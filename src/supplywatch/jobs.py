"""Background jobs: BOM sync (git / folders, optional Altium 365) and supply refresh from the configured sources."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select

from . import compliance, config, db
from .countries import country_name
from .db import CooOverride, Offer, Part, Project, Run, StockSnapshot, norm_mpn, utcnow
from .sources import altium365, git_repos, nexar, nexar_supply, trustedparts
from .sources.distributors import DigiKey, Farnell, Mouser
from .warehouses import stock_by_region

log = logging.getLogger("supplywatch.jobs")
_lock = threading.Lock()

# Direct distributor data replaces that distributor's listing inside an aggregator (no double counting).
DIRECT_NAMES = {
    "digikey": ("digikey", "digi-key"),
    "mouser": ("mouser",),
    "farnell": ("farnell", "element14", "newark"),
}
AGGREGATORS = ("trustedparts", "nexar")
# Which source wins for each field (first with a value).
PRIORITY = {
    "lifecycle": ["digikey", "mouser", "farnell", "nexar", "trustedparts"],
    "lead_days": ["digikey", "mouser", "farnell", "nexar", "trustedparts"],
    "coo": ["farnell", "digikey", "mouser", "trustedparts", "nexar"],
    "rohs": ["digikey", "mouser", "farnell", "nexar", "trustedparts"],
    "reach": ["digikey", "mouser", "farnell", "nexar", "trustedparts"],
}


class Busy(RuntimeError):
    pass


def _run(kind: str, who: str, fn):
    if not _lock.acquire(blocking=False):
        raise Busy("Another sync is already running. Try again when it finishes.")
    with db.session() as s:
        run = Run(kind=kind, triggered_by=who)
        s.add(run)
        s.flush()
        run_id = run.id
    lines: list[str] = []
    status, summary = "ok", ""
    try:
        summary = fn(lines) or ""
        if any(ln.startswith("error") for ln in lines):
            status = "partial"
    except nexar.NexarNotConnected as e:
        status, summary = "error", str(e)
    except Exception as e:  # noqa: BLE001
        status, summary = "error", f"{type(e).__name__}: {e}"
        lines.append(traceback.format_exc(limit=3))
        log.exception("%s failed", kind)
    finally:
        with db.session() as s:
            run = s.get(Run, run_id)
            run.finished, run.status, run.summary, run.log = utcnow(), status, summary, lines[-400:]
        _lock.release()
    return run_id


# ------------------------------------------------------------------------------------ BOM sync
def bom_sources() -> list[str]:
    cfg = config.settings
    out = []
    if cfg.git_repos or cfg.project_dirs:
        out.append("git")
    if cfg.nexar_client_id and nexar.design_connected():
        out.append("a365")
    return out


def bom_sync(who: str = "schedule") -> int:
    def work(lines):
        srcs = bom_sources()
        if not srcs:
            raise RuntimeError("No BOM source configured. Set GIT_REPOS or PROJECT_DIRS.")
        parts = []
        for src in srcs:
            try:
                r = git_repos.sync(lines) if src == "git" else altium365.sync(lines)
            except Exception as e:  # noqa: BLE001
                lines.append(f"error: {src}: {e}")
                continue
            msg = f"{r['projects']} project(s), {r['lines']} BOM lines"
            if r.get("failed"):
                msg += f", {r['failed']} source(s) failed"
            parts.append(("Git: " if src == "git" else "Altium 365: ") + msg)
        if not parts:
            raise RuntimeError("; ".join(ln for ln in lines if ln.startswith("error")) or "BOM sync failed")
        return " · ".join(parts)

    return _run("bom_sync", who, work)


# ------------------------------------------------------------------------------------ supply sources
@dataclass
class Source:
    key: str
    label: str
    enabled: Callable[[], bool]
    lookup: Callable[[list[tuple[str, str]], list], dict]  # [(mpn, mfr)], log -> {norm_mpn: info|None}


def _digikey_many(items, lines):
    dk, out, n = DigiKey(), {}, 0
    for m, _ in items:
        v = dk.lookup(m)  # RuntimeError on auth problems stops the source
        out[norm_mpn(m)] = v
        n += bool(v)
    lines.append(f"digikey: found {n}/{len(items)}")
    return out


def _mouser_many(items, lines):
    r = Mouser().lookup_many([m for m, _ in items])
    lines.append(f"mouser: found {len(r)}/{len(items)}")
    return {norm_mpn(m): r.get(norm_mpn(m)) for m, _ in items}


def _farnell_many(items, lines):
    f, out, n = Farnell(), {}, 0
    for m, _ in items:
        v = f.lookup(m)
        out[norm_mpn(m)] = v
        n += bool(v)
    lines.append(f"farnell: found {n}/{len(items)}")
    return out


SOURCES = [
    Source("trustedparts", "TrustedParts", trustedparts.enabled, trustedparts.lookup_many),
    Source("digikey", "DigiKey", DigiKey.enabled, _digikey_many),
    Source("mouser", "Mouser", Mouser.enabled, _mouser_many),
    Source("farnell", "Farnell", Farnell.enabled, _farnell_many),
    Source("nexar", "Octopart (Nexar)", lambda: config.settings.nexar_supply_enabled, nexar_supply.lookup),
]


def wanted_parts(s) -> list[tuple[str, str, bool]]:
    """(mpn, manufacturer, preferred) for every part choice on active projects; preferred first."""
    cfg = config.settings
    seen: dict[str, tuple[str, str, bool]] = {}
    for p in s.scalars(select(Project).where(Project.active.is_(True))).all():
        for ln in p.lines:
            for i, ch in enumerate(ln.part_choices or []):
                if i > 0 and not cfg.include_alternates:
                    break
                k = norm_mpn(ch.get("mpn", ""))
                if not k:
                    continue
                if k not in seen or (i == 0 and not seen[k][2]):
                    seen[k] = (ch["mpn"], ch.get("mfr", ""), i == 0)
    return sorted(seen.values(), key=lambda x: (not x[2], x[0]))


def _merge(part: Part, source: str, info: dict | None, now: dt.datetime):
    """Replaces this source's offers on the part. info=None means no result; the old offers stay."""
    if info is None:
        return
    part.offers = [o for o in part.offers if o.source != source]
    for o in info.get("offers", []):
        part.offers.append(
            Offer(
                source=source,
                seller=o.get("seller") or source,
                authorized=o.get("authorized"),
                sku=o.get("sku") or "",
                stock=o.get("stock"),
                stock_note=o.get("stock_note") or "",
                on_order=o.get("on_order"),
                moq=o.get("moq"),
                packaging=o.get("packaging") or "",
                lead_days=o.get("lead_days"),
                prices=o.get("prices") or [],
                url=o.get("url") or "",
                seller_country=o.get("seller_country") or "",
                warehouse=o.get("warehouse") or "",
                updated=o.get("updated") or now,
            )
        )
    for f in ("manufacturer", "description", "datasheet", "octopart_url", "hts", "eccn"):
        if info.get(f) and not getattr(part, f):
            setattr(part, f, info[f])


def _dedupe(part: Part):
    for src, names in DIRECT_NAMES.items():
        if any(o.source == src for o in part.offers):
            part.offers = [
                o
                for o in part.offers
                if not (o.source in AGGREGATORS and any(n in (o.seller or "").lower() for n in names))
            ]
    # the two aggregators overlap too: keep TrustedParts' copy of a distributor if both list it
    tp = {(o.seller or "").lower() for o in part.offers if o.source == "trustedparts"}
    part.offers = [o for o in part.offers if not (o.source == "nexar" and (o.seller or "").lower() in tp)]


def _aggregate(part: Part, infos: dict[str, dict | None], override: CooOverride | None):
    part.lifecycle = part.lifecycle_source = ""
    part.lead_days, part.lead_source = None, ""
    part.coo = part.coo_source = ""
    part.rohs = part.rohs_source = part.reach = part.reach_source = ""
    for field_, attr_src in (
        ("lifecycle", "lifecycle_source"),
        ("lead_days", "lead_source"),
        ("coo", "coo_source"),
        ("rohs", "rohs_source"),
        ("reach", "reach_source"),
    ):
        for src in PRIORITY[field_]:
            v = (infos.get(src) or {}).get(field_)
            if field_ in ("rohs", "reach"):
                v = compliance.normalise(field_, v)
            if v:
                setattr(part, field_, int(v) if field_ == "lead_days" else (country_name(v) if field_ == "coo" else v))
                setattr(part, attr_src, src)
                break
    if part.lead_days is None:
        leads = sorted((o.lead_days, o.source) for o in part.offers if o.lead_days)
        if leads:
            part.lead_days, part.lead_source = leads[0][0], f"{leads[0][1]} offer"
    if override:
        part.coo, part.coo_source = override.coo, "manual"
    stocks = [o.stock for o in part.offers if o.stock is not None and o.authorized is not False]
    part.total_stock = sum(stocks) if stocks else (0 if part.offers else None)


def _snapshot(s, part: Part, now: dt.datetime):
    """Records stock per region for the stock-history chart. Missing (region, trusted) rows mean zero."""
    totals: dict[tuple[str, bool], list[int]] = {}
    for o in part.offers:
        for reg, qty in stock_by_region(o).items():
            t = totals.setdefault((reg, o.authorized is True), [0, 0])
            t[0] += qty
            t[1] += 1
    for (reg, trusted), (qty, n) in totals.items():
        s.add(StockSnapshot(part_id=part.id, at=now, region=reg, trusted=trusted, stock=qty, sellers=n))
    if not totals:  # nothing in stock anywhere: still mark that the part was checked at this time
        s.add(StockSnapshot(part_id=part.id, at=now, region="Unlocated", trusted=True, stock=0, sellers=0))


def _slim(info: dict) -> dict:
    return {k: info.get(k) for k in ("lifecycle", "lead_days", "coo", "hts", "eccn", "rohs", "reach")}


def supply_refresh(who: str = "schedule", force: bool = False, only: list[str] | None = None) -> int:
    def work(lines):
        cfg = config.settings
        now = utcnow()
        cutoff = now - dt.timedelta(hours=cfg.supply_max_age_hours)
        with db.session() as s:
            wanted = wanted_parts(s)
            if only:
                keys = {norm_mpn(k) for k in only}
                wanted = [w for w in wanted if norm_mpn(w[0]) in keys] or [(k, "", True) for k in only]
            existing = {p.key: p for p in s.scalars(select(Part)).all()}
            todo = [
                w
                for w in wanted
                if force
                or only
                or norm_mpn(w[0]) not in existing
                or existing[norm_mpn(w[0])].refreshed_at is None
                or existing[norm_mpn(w[0])].refreshed_at < cutoff
            ]
        lines.append(f"{len(wanted)} MPN(s) in scope, {len(todo)} due for refresh")
        if not todo:
            return "Nothing due for refresh"

        items = [(m, mf) for m, mf, _ in todo]
        results: dict[str, dict[str, dict | None]] = {norm_mpn(m): {} for m, _ in items}
        ok = []
        for src in SOURCES:
            if not src.enabled():
                continue
            try:
                for k, v in src.lookup(items, lines).items():
                    results.setdefault(k, {})[src.key] = v
                ok.append(src.label)
            except Exception as e:  # noqa: BLE001
                lines.append(f"error: {src.key}: {e}")
        if not ok:
            raise RuntimeError("No supply source is configured or reachable. Set API keys in .env.")

        with db.session() as s:
            overrides = {o.key: o for o in s.scalars(select(CooOverride)).all()}
            for mpn, mfr in items:
                k = norm_mpn(mpn)
                infos = results.get(k, {})
                part = s.scalar(select(Part).where(Part.key == k))
                if not part:
                    part = Part(key=k, mpn=mpn, manufacturer=mfr)
                    s.add(part)
                    s.flush()
                for src, info in infos.items():
                    _merge(part, src, info, now)
                _dedupe(part)
                # keep each source's last summary so a failed source doesn't wipe lifecycle / lead / COO
                merged = {
                    **(db.kv_get(s, f"part:{k}", {}) or {}),
                    **{src: _slim(i) for src, i in infos.items() if i is not None},
                }
                db.kv_set(s, f"part:{k}", merged)
                _aggregate(part, merged, overrides.get(k))
                if infos:
                    _snapshot(s, part, now)
                part.not_found = not any(infos.values()) and not part.offers
                part.refreshed_at = now
        return f"Refreshed {len(items)} part(s) from {', '.join(ok)}"

    return _run("supply_refresh", who, work)


def set_coo(key: str, coo: str, note: str, who: str):
    key = norm_mpn(key)
    with db.session() as s:
        o = s.get(CooOverride, key)
        part = s.scalar(select(Part).where(Part.key == key))
        if not coo.strip():
            if o:
                s.delete(o)
            if part:
                _aggregate(part, db.kv_get(s, f"part:{key}", {}) or {}, None)
            return
        if not o:
            o = CooOverride(key=key, coo=coo.strip(), note=note, by=who)
            s.add(o)
        else:
            o.coo, o.note, o.by, o.at = coo.strip(), note, who, utcnow()
        if part:
            part.coo, part.coo_source = o.coo, "manual"
