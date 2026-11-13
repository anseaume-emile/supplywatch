"""Octopart data through Nexar supMultiMatch: sellers, stock, prices, and (plan permitting) lead time and lifecycle."""

from __future__ import annotations

import datetime as dt

import httpx

from .. import compliance, config, db
from ..db import norm_mpn
from . import nexar

# Fields that need a Nexar plan add-on are only requested when enabled in config.
_LEAD = "estimatedFactoryLeadDays"
_SPECS = "specs { attribute { shortname name } displayValue }"
_DATASHEET = "bestDatasheet { url }"


def build_query() -> str:
    cfg = config.settings
    extra = []
    if cfg.nexar_lead_time:
        extra.append(_LEAD)
    if cfg.nexar_specs:
        extra += [_SPECS, _DATASHEET]
    auth = "true" if cfg.nexar_authorized_only else "false"
    offer_lead = "factoryLeadDays" if cfg.nexar_lead_time else ""
    return f"""
query($queries: [SupPartMatchQuery!]!, $country: String!, $currency: String!) {{
  supMultiMatch(queries: $queries, country: $country, currency: $currency) {{
    hits
    reference
    parts {{
      mpn
      manufacturer {{ name }}
      shortDescription
      octopartUrl
      totalAvail
      {" ".join(extra)}
      sellers(authorizedOnly: {auth}) {{
        country
        isAuthorized
        company {{ name }}
        offers {{
          sku inventoryLevel moq packaging updated clickUrl {offer_lead}
          prices {{ quantity price currency convertedPrice convertedCurrency }}
        }}
      }}
    }}
  }}
}}"""


_STOCK_CODES = {-1: "not stocked", -2: "in stock, qty unknown", -3: "no quantity given", -4: "quote required"}


def _denied() -> set[str]:
    with db.session() as s:
        return set(db.kv_get(s, "nexar_rejected_fields", []) or [])


def _remember_denied(fields: set[str]):
    with db.session() as s:
        cur = set(db.kv_get(s, "nexar_rejected_fields", []) or [])
        db.kv_set(s, "nexar_rejected_fields", sorted(cur | fields))


def _pick(parts: list[dict], mpn: str, mfr: str) -> dict | None:
    if not parts:
        return None
    key = norm_mpn(mpn)
    exact = [p for p in parts if norm_mpn(p.get("mpn", "")) == key] or parts
    if mfr:
        m = mfr.lower().split()[0]
        for p in exact:
            if m in ((p.get("manufacturer") or {}).get("name") or "").lower():
                return p
    return exact[0]


def _parse_dt(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def normalise(part: dict) -> dict:
    cfg = config.settings
    offers = []
    for sl in part.get("sellers") or []:
        seller = ((sl.get("company") or {}).get("name")) or "?"
        for o in sl.get("offers") or []:
            inv = o.get("inventoryLevel")
            note = ""
            if isinstance(inv, int) and inv < 0:
                note, inv = _STOCK_CODES.get(inv, "unknown"), None
            prices = []
            for p in o.get("prices") or []:
                price, cur = p.get("price"), p.get("currency")
                if (
                    p.get("convertedPrice") is not None
                    and (p.get("convertedCurrency") or "").upper() == cfg.currency.upper()
                ):
                    price, cur = p["convertedPrice"], p["convertedCurrency"]
                prices.append({"qty": p.get("quantity"), "price": price, "currency": cur})
            prices.sort(key=lambda x: x["qty"] or 0)
            offers.append(
                {
                    "source": "nexar",
                    "seller": seller,
                    "authorized": sl.get("isAuthorized"),
                    "seller_country": sl.get("country") or "",
                    "sku": o.get("sku") or "",
                    "stock": inv,
                    "stock_note": note,
                    "moq": o.get("moq"),
                    "packaging": o.get("packaging") or "",
                    "lead_days": o.get("factoryLeadDays"),
                    "prices": prices,
                    "url": o.get("clickUrl") or "",
                    "updated": _parse_dt(o.get("updated")),
                }
            )
    specs = {
        ((s.get("attribute") or {}).get("shortname") or ""): s.get("displayValue") for s in part.get("specs") or []
    }
    return {
        "mpn": part.get("mpn") or "",
        "manufacturer": (part.get("manufacturer") or {}).get("name") or "",
        "description": part.get("shortDescription") or "",
        "octopart_url": part.get("octopartUrl") or "",
        "total_avail": part.get("totalAvail"),
        "lead_days": part.get("estimatedFactoryLeadDays"),
        "lifecycle": specs.get("lifecyclestatus") or "",
        "rohs": specs.get("rohs") or "",
        "reach": compliance.find({k: v for k, v in specs.items() if v}, "reach"),
        "coo": specs.get("country_of_origin") or specs.get("countryoforigin") or "",
        "datasheet": ((part.get("bestDatasheet") or {}).get("url")) or "",
        "offers": offers,
    }


def lookup(items: list[tuple[str, str]], log: list, client: httpx.Client | None = None) -> dict[str, dict | None]:
    """items: [(mpn, manufacturer)]. Returns {norm_mpn: normalised part or None}. Honours the monthly budget."""
    cfg = config.settings
    c = client or httpx.Client(timeout=nexar.TIMEOUT)
    token = nexar.supply_token(c)
    out: dict[str, dict | None] = {}
    query = build_query()
    for f in _denied():
        query = nexar.strip_field(query, f)
    i = 0
    while i < len(items):
        with db.session() as s:
            used = db.usage_this_month(s).parts
        room = cfg.nexar_monthly_part_budget - used
        if room <= 0:
            log.append(
                f"nexar: monthly budget of {cfg.nexar_monthly_part_budget} matched parts reached; "
                f"{len(items) - i} MPN(s) left for next month"
            )
            break
        batch = items[i : i + min(50, room)]
        queries = [{"mpn": m, "limit": 1, "reference": str(i + k)} for k, (m, _) in enumerate(batch)]
        variables = {"queries": queries, "country": cfg.country, "currency": cfg.currency}
        for _ in range(6):
            data, errors = nexar.graphql(query, variables, token, client=c)
            bad = nexar.rejected_fields(errors)
            if (
                not bad
                and errors
                and not data.get("supMultiMatch")
                and "reference" in nexar.error_text(errors)
                and "reference" in variables["queries"][0]
            ):
                log.append("nexar: 'reference' not accepted in match queries; using positional results")
                variables["queries"] = [{k: v for k, v in q.items() if k != "reference"} for q in variables["queries"]]
                query = nexar.strip_field(query, "reference")
                continue
            if not bad:
                break
            log.append("nexar: plan/schema refused " + ", ".join(sorted(bad)) + "; retrying without")
            _remember_denied(bad)
            for f in bad:
                query = nexar.strip_field(query, f)
        if errors and not data.get("supMultiMatch"):
            raise nexar.NexarError(nexar.error_text(errors))
        results = data.get("supMultiMatch") or []
        returned = 0
        for k, (mpn, mfr) in enumerate(batch):
            res = next((r for r in results if str(r.get("reference")) == str(i + k)), None)
            if res is None and k < len(results) and "reference" not in results[k]:
                res = results[k]
            parts = (res or {}).get("parts") or []
            returned += len(parts)
            p = _pick(parts, mpn, mfr)
            out[norm_mpn(mpn)] = normalise(p) if p else None
        with db.session() as s:
            db.usage_this_month(s).parts += returned
        log.append(
            f"nexar: matched {sum(1 for k, _ in batch if out.get(norm_mpn(k)))}/{len(batch)} MPNs "
            f"({returned} parts counted against quota)"
        )
        i += len(batch)
    return out
