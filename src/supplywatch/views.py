"""Read models for the UI and exports: per-part demand across projects, risk flags."""

from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import select

from . import compliance, config, db
from .db import CooOverride, Part, Project, StockSnapshot, norm_mpn, utcnow
from .warehouses import locate

BAD_LIFECYCLE = re.compile(r"eol|end of life|obsolete|nrnd|not recommended|discontinu|last time|ltb", re.I)

FLAGS = {
    "no_mpn": ("No MPN", "critical", "BOM line has no manufacturer part number."),
    "not_found": ("Not found", "critical", "No supplier returned this MPN."),
    "lifecycle": ("Lifecycle", "critical", "EOL, NRND, obsolete or discontinued."),
    "shortage": ("Shortage", "critical", "Authorised stock is below the quantity needed for the planned builds."),
    "low_stock": ("Low stock", "warning", "Authorised stock covers less than the configured multiple of demand."),
    "long_lead": ("Long lead", "warning", "Factory lead time is at or above the threshold."),
    "not_rohs": ("Not RoHS", "critical", "A supplier lists this part as not RoHS compliant."),
    "reach_svhc": (
        "REACH SVHC",
        "warning",
        "Contains a REACH substance of very high concern above 0.1% w/w; it must be declared to customers.",
    ),
    "single_source": ("Single source", "warning", "Only one approved MPN for this BOM line."),
    "no_coo": ("No COO", "info", "Country of origin unknown. Enter it on the part page."),
    "stale": ("Stale", "info", "Supply data is older than twice the refresh interval."),
    "unchecked": ("Not checked", "info", "No supply data yet. Run a supply refresh."),
}


def _iso(v: dt.datetime | None) -> str | None:
    return v.isoformat() + "Z" if v else None


def best_price(offers, qty: int) -> dict | None:
    best = None
    for o in offers:
        if o.authorized is False or not o.prices:
            continue
        if o.stock is not None and o.stock < qty:
            continue
        if o.moq and o.moq > qty:
            continue
        tiers = [p for p in o.prices if (p.get("qty") or 0) <= max(qty, 1) and p.get("price") is not None]
        if not tiers:
            continue
        t = max(tiers, key=lambda p: p.get("qty") or 0)
        if best is None or t["price"] < best["price"]:
            best = {"price": t["price"], "currency": t.get("currency") or "", "seller": o.seller, "sku": o.sku}
    return best


def offer_dict(o) -> dict:
    warehouse, basis = locate(o)
    return {
        "source": o.source,
        "seller": o.seller,
        "authorized": o.authorized,
        "sku": o.sku,
        "stock": o.stock,
        "stock_note": o.stock_note,
        "on_order": o.on_order,
        "moq": o.moq,
        "packaging": o.packaging,
        "lead_days": o.lead_days,
        "prices": o.prices,
        "url": o.url,
        "seller_country": o.seller_country,
        "warehouse": warehouse,
        "warehouse_basis": basis,
        "updated": _iso(o.updated),
    }


def build(s) -> dict:
    """Everything the UI needs, computed in one pass."""
    cfg = config.settings
    now = utcnow()
    stale_cut = now - dt.timedelta(hours=cfg.supply_max_age_hours * 2)
    parts = {p.key: p for p in s.scalars(select(Part)).all()}
    overrides = {o.key: o for o in s.scalars(select(CooOverride)).all()}
    projects = sorted(
        s.scalars(select(Project)).all(),
        key=lambda p: (p.name.lower(), p.variant != "[No Variations]", p.variant.lower()),
    )

    usage: dict[str, dict] = {}
    proj_out = []
    for p in projects:
        lines_out = []
        pflags: dict[str, int] = {}
        for ln in p.lines:
            choices = ln.part_choices or []
            lf = []
            if not choices:
                lf.append("no_mpn")
            elif len(choices) == 1:
                lf.append("single_source")
            primary = norm_mpn(choices[0]["mpn"]) if choices else ""
            for i, ch in enumerate(choices):
                k = norm_mpn(ch["mpn"])
                u = usage.setdefault(
                    k,
                    {
                        "mpn": ch["mpn"],
                        "mfr": ch.get("mfr", ""),
                        "projects": {},
                        "per_board": 0,
                        "required": 0,
                        "alternate_for": set(),
                        "alternates": set(),
                        "lines": set(),
                        "single_source": False,
                    },
                )
                if p.active:
                    if i == 0:
                        u["projects"][p.id] = u["projects"].get(p.id, 0) + ln.quantity
                        u["required"] += ln.quantity * p.build_qty
                        u["per_board"] += ln.quantity
                        u["lines"].add(ln.component)
                        u["alternates"].update(norm_mpn(c["mpn"]) for c in choices[1:])
                        if len(choices) == 1:
                            u["single_source"] = True
                    else:
                        u["alternate_for"].add(choices[0]["mpn"])
            pp = parts.get(primary)
            if pp is not None:
                lf += _part_flags(pp, ln.quantity * p.build_qty, stale_cut, overrides.get(primary))
            for f in lf:
                pflags[f] = pflags.get(f, 0) + 1
            lines_out.append(
                {
                    "component": ln.component,
                    "description": ln.description,
                    "designators": ln.designators,
                    "quantity": ln.quantity,
                    "choices": choices,
                    "primary": primary,
                    "need": ln.quantity * p.build_qty,
                    "flags": sorted(set(lf)),
                    "footprint": ln.footprint,
                    "mpn_source": ln.mpn_source,
                }
            )
        proj_out.append(
            {
                "id": p.id,
                "name": p.name,
                "variant": p.variant,
                "source": p.source,
                "description": p.description,
                "build_qty": p.build_qty,
                "active": p.active,
                "synced_at": _iso(p.synced_at),
                "line_count": len(p.lines),
                "revision": p.revision,
                "location": p.location,
                "placements": sum(ln.quantity for ln in p.lines),
                "flags": pflags,
                "lines": lines_out,
            }
        )

    parts_out = []
    for k, u in usage.items():
        if not u["projects"] and not u["alternate_for"]:
            continue
        pp = parts.get(k)
        req = u["required"]
        flags = []
        if pp is None:
            flags.append("unchecked")
        else:
            flags += _part_flags(pp, req, stale_cut, overrides.get(k))
        if u["single_source"]:
            flags.append("single_source")
        parts_out.append(
            {
                "key": k,
                "mpn": (pp.mpn if pp else u["mpn"]),
                "manufacturer": (pp.manufacturer if pp and pp.manufacturer else u["mfr"]),
                "description": pp.description if pp else "",
                "used_in": {str(pid): q for pid, q in u["projects"].items()},
                "per_board": u["per_board"],
                "required": req,
                "alternate_for": sorted(u["alternate_for"]),
                "alternates": sorted(u["alternates"] - {k}),
                "is_alternate_only": not u["projects"],
                "lines": sorted(x for x in u["lines"] if x),
                "stock": pp.total_stock if pp else None,
                "lead_days": pp.lead_days if pp else None,
                "lead_source": pp.lead_source if pp else "",
                "lifecycle": pp.lifecycle if pp else "",
                "coo": pp.coo if pp else (overrides[k].coo if k in overrides else ""),
                "coo_source": pp.coo_source if pp else ("manual" if k in overrides else ""),
                "hts": pp.hts if pp else "",
                "eccn": pp.eccn if pp else "",
                "rohs": pp.rohs if pp else "",
                "rohs_source": pp.rohs_source if pp else "",
                "reach": pp.reach if pp else "",
                "reach_source": pp.reach_source if pp else "",
                "suppliers": len({o.seller for o in pp.offers if (o.stock or 0) > 0}) if pp else 0,
                "offers_count": len(pp.offers) if pp else 0,
                "best_price": best_price(pp.offers, max(req, 1)) if pp else None,
                "refreshed_at": _iso(pp.refreshed_at) if pp else None,
                "flags": sorted(set(flags), key=list(FLAGS).index),
            }
        )
    parts_out.sort(key=lambda x: (_severity(x["flags"]), x["mpn"]))
    return {
        "projects": proj_out,
        "parts": parts_out,
        "flags": {k: {"label": v[0], "level": v[1], "help": v[2]} for k, v in FLAGS.items()},
        "thresholds": {
            "long_lead_days": cfg.long_lead_days,
            "low_stock_multiple": cfg.low_stock_multiple,
            "max_age_hours": cfg.supply_max_age_hours,
        },
        "currency": cfg.currency,
    }


def _severity(flags: list[str]) -> int:
    lv = [FLAGS[f][1] for f in flags]
    return 0 if "critical" in lv else 1 if "warning" in lv else 2


def _part_flags(pp: Part, required: int, stale_cut: dt.datetime, override) -> list[str]:
    cfg = config.settings
    f = []
    if pp.not_found:
        f.append("not_found")
    if pp.lifecycle and BAD_LIFECYCLE.search(pp.lifecycle):
        f.append("lifecycle")
    if pp.total_stock is not None and required:
        if pp.total_stock < required:
            f.append("shortage")
        elif pp.total_stock < required * cfg.low_stock_multiple:
            f.append("low_stock")
    if pp.lead_days and pp.lead_days >= cfg.long_lead_days:
        f.append("long_lead")
    if pp.rohs == compliance.NOT_COMPLIANT:
        f.append("not_rohs")
    if pp.reach == compliance.SVHC:
        f.append("reach_svhc")
    if not (pp.coo or override):
        f.append("no_coo")
    if pp.refreshed_at and pp.refreshed_at < stale_cut:
        f.append("stale")
    return f


def part_detail(s, key: str) -> dict | None:
    key = norm_mpn(key)
    pp = s.scalar(select(Part).where(Part.key == key))
    data = build(s)
    row = next((p for p in data["parts"] if p["key"] == key), None)
    if not pp and not row:
        return None
    ov = s.get(CooOverride, key)
    offers = sorted(pp.offers, key=lambda o: (-(o.stock or 0), o.seller)) if pp else []
    return {
        "part": row or {"key": key, "mpn": pp.mpn, "flags": []},
        "alternates": _related(data["parts"], row) if row else [],
        "offers": [offer_dict(o) for o in offers],
        "stock_history": stock_history(s, pp.id) if pp else [],
        "sources": db.kv_get(s, f"part:{key}", {}) or {},
        "datasheet": pp.datasheet if pp else "",
        "octopart_url": pp.octopart_url if pp else "",
        "coo_override": {"coo": ov.coo, "note": ov.note, "by": ov.by, "at": _iso(ov.at)} if ov else None,
        "projects": {
            str(p["id"]): {"name": p["name"], "variant": p["variant"], "build_qty": p["build_qty"]}
            for p in data["projects"]
        },
    }


def stock_history(s, part_id: int, days: int = 365) -> list[dict]:
    rows = s.scalars(
        select(StockSnapshot)
        .where(StockSnapshot.part_id == part_id, StockSnapshot.at >= utcnow() - dt.timedelta(days=days))
        .order_by(StockSnapshot.at)
    ).all()
    return [
        {"at": _iso(r.at), "region": r.region, "trusted": r.trusted, "stock": r.stock, "sellers": r.sellers}
        for r in rows
    ]


def _related(parts: list[dict], row: dict) -> list[dict]:
    """Parts interchangeable with `row` on some BOM line: its own alternates, or the primaries it stands in for
    and their other alternates. `need` is the primary's demand, so stock can be judged against it."""
    by_key = {p["key"]: p for p in parts}
    out: dict[str, dict] = {}

    def add(k: str, relation: str, need: int):
        if k == row["key"] or k not in by_key:
            return
        cur = out.get(k)
        if cur is None:
            out[k] = {**by_key[k], "relation": relation, "need": need}
        else:
            cur["need"] = max(cur["need"], need)
            if relation == "primary":
                cur["relation"] = relation

    for k in row.get("alternates", []):
        add(k, "alternate", row["required"])
    for mpn in row.get("alternate_for", []):
        prim = by_key.get(norm_mpn(mpn))
        if prim:
            add(prim["key"], "primary", prim["required"])
            for k in prim["alternates"]:
                add(k, "sibling", prim["required"])
    order = {"primary": 0, "alternate": 1, "sibling": 2}
    return sorted(out.values(), key=lambda x: (order[x["relation"]], _severity(x["flags"]), x["mpn"]))
