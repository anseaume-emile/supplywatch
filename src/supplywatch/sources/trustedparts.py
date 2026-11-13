"""TrustedParts.com Inventory API v2 (ECIA): authorised-distributor stock and pricing across many distributors.

Free, on request (trustedparts.com → My Account → API Keys). 50 part numbers per request.
Soft limits (real-time data): 50 parts / 10 s, 2,000 / hour, 15,000 / day; above them the API
serves cached data. Terms: internal purchasing use; cache for at most one week.

Response fields confirmed from the docs and the Part-DB client: PartResults[].PartNumber,
Manufacturer, Distributors[].Name, DistributorResults[].DistributorPartNumber, Stock.QuantityOnHand,
Pricing.Prices[].{Quantity,Amount}, Links[].{Url,Type}. Lead time / MOQ / packaging names are
not published, so they are found by key name; a sample response is stored for inspection.
"""

from __future__ import annotations

import re
import time

import httpx

from .. import compliance, config, db
from ..countries import country_name
from ..db import norm_mpn
from .distributors import _num, find_coo

URL = "https://api.trustedparts.com/v2/search"
TIMEOUT = httpx.Timeout(45.0, connect=10.0)
BATCH = 50


def enabled() -> bool:
    c = config.settings
    return bool(c.trustedparts_company_id and c.trustedparts_api_key)


def _find(obj, *patterns: str):
    """First scalar value whose key matches one of the regex patterns (case-insensitive), searched depth-first."""
    rx = [re.compile(p, re.I) for p in patterns]
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(v, (dict, list)) and v not in (None, "") and any(r.search(k) for r in rx):
                return v
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = _find(v, *patterns)
                if r not in (None, ""):
                    return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, *patterns)
            if r not in (None, ""):
                return r
    return None


def _lead_days(dr: dict) -> int | None:
    v = _find(dr, r"leadtime", r"lead_time", r"^lead$")
    if v is None:
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        unit = str(_find(dr, r"leadtime.*unit", r"lead.*uom") or "").lower()
        return n * 7 if unit.startswith("w") else n
    m = re.search(r"(\d+)\s*(d|day|w|wk|week)", str(v), re.I)
    if m:
        return int(m.group(1)) * (7 if m.group(2).lower().startswith("w") else 1)
    n = _num(v)
    return n


def _warehouse(dr: dict) -> str:
    v = _find(dr.get("Stock") or {}, r"warehouse", r"ships?from", r"location", r"region") or _find(
        dr, r"warehouse", r"ships?from", r"stocklocation"
    )
    return country_name(str(v)) if v not in (None, "") else ""


def normalise(pr: dict) -> dict:
    offers = []
    for dist in pr.get("Distributors") or []:
        seller = dist.get("Name") or "?"
        for dr in dist.get("DistributorResults") or [dist]:
            stock = (dr.get("Stock") or {}).get("QuantityOnHand")
            pricing = dr.get("Pricing") or {}
            cur = pricing.get("CurrencyCode") or config.settings.currency
            prices = sorted(
                (
                    {"qty": p.get("Quantity"), "price": p.get("Amount"), "currency": cur}
                    for p in pricing.get("Prices") or []
                    if p.get("Amount") is not None
                ),
                key=lambda x: x["qty"] or 0,
            )
            links = dr.get("Links") or []
            url = next((ln.get("Url") for ln in links if (ln.get("Type") or "").lower() not in ("datasheet",)), "")
            offers.append(
                {
                    "source": "trustedparts",
                    "seller": seller,
                    "authorized": True,
                    "sku": dr.get("DistributorPartNumber") or "",
                    "stock": _num(stock),
                    "moq": _num(_find(dr, r"^min(imum)?(order)?(quantity|qty)$", r"^moq$")),
                    "packaging": str(_find(dr, r"^packag") or ""),
                    "lead_days": _lead_days(dr),
                    "prices": prices,
                    "url": url or "",
                    "warehouse": _warehouse(dr),
                }
            )
    datasheet = ""
    for dist in pr.get("Distributors") or []:
        for dr in dist.get("DistributorResults") or []:
            for ln in dr.get("Links") or []:
                if (ln.get("Type") or "").lower() == "datasheet" and not datasheet:
                    datasheet = ln.get("Url") or ""
    specs = {s.get("Key", ""): s.get("Value", "") for s in pr.get("Specifications") or []}
    lifecycle = next((v for k, v in specs.items() if "lifecycle" in k.lower() or "part status" in k.lower()), "")
    return {
        "mpn": pr.get("PartNumber") or "",
        "manufacturer": pr.get("Manufacturer") or "",
        "description": next(
            (
                dr.get("Description")
                for dist in pr.get("Distributors") or []
                for dr in dist.get("DistributorResults") or []
                if dr.get("Description")
            ),
            "",
        ),
        "lifecycle": lifecycle,
        "lead_days": None,
        "coo": find_coo(pr),
        "datasheet": datasheet,
        "rohs": compliance.find(pr.get("Specifications") or [], "rohs") or compliance.find(pr, "rohs"),
        "reach": compliance.find(pr.get("Specifications") or [], "reach") or compliance.find(pr, "reach"),
        "offers": offers,
    }


def lookup_many(
    items: list[tuple[str, str]], log: list, client: httpx.Client | None = None, sleep=time.sleep
) -> dict[str, dict | None]:
    cfg = config.settings
    c = client or httpx.Client(timeout=TIMEOUT)
    out: dict[str, dict | None] = {}
    for i in range(0, len(items), BATCH):
        if i:
            sleep(10.5)  # stay inside the 50-parts-per-10-seconds soft limit (real-time data)
        batch = items[i : i + BATCH]
        body = {
            "CompanyId": cfg.trustedparts_company_id,
            "ApiKey": cfg.trustedparts_api_key,
            "Queries": [{"SearchToken": m} for m, _ in batch],
            "CountryCode": cfg.country,
            "CurrencyCode": cfg.currency,
            "LanguageCode": "en",
            "InStockOnly": False,
            "ExactMatch": True,
            "IsCrawler": False,
            "UseCachedData": False,
            "UserAgent": "SupplyWatch/1.0 (internal procurement)",
        }
        r = c.post(URL, json=body, headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise RuntimeError(f"TrustedParts HTTP {r.status_code}: {r.text[:200]}")
        j = r.json() or {}
        if j.get("ErrorMessage"):
            raise RuntimeError(f"TrustedParts: {j['ErrorMessage']}")
        results = j.get("PartResults") or []
        if i == 0 and results:
            with db.session() as s:
                db.kv_set(s, "trustedparts_sample", results[0])
        by_key: dict[str, list[dict]] = {}
        for pr in results:
            by_key.setdefault(norm_mpn(pr.get("PartNumber", "")), []).append(pr)
        for mpn, mfr in batch:
            cands = by_key.get(norm_mpn(mpn)) or []
            pick = None
            if cands and mfr:
                first = mfr.lower().split()[0]
                pick = next((p for p in cands if first in (p.get("Manufacturer") or "").lower()), None)
            pick = pick or (cands[0] if cands else None)
            out[norm_mpn(mpn)] = normalise(pick) if pick else None
        log.append(f"trustedparts: matched {sum(1 for m, _ in batch if out.get(norm_mpn(m)))}/{len(batch)}")
    return out
