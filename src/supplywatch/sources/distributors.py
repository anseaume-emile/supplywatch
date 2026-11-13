"""Direct distributor lookups: DigiKey (Product Information v4), Mouser (Search API v1), Farnell (element14).

They add direct stock, manufacturer lead times, lifecycle and trade classification (HTS / ECCN).
Farnell publishes country of origin; DigiKey and Mouser don't document one, but if a COO-like
field appears in a response it is picked up.
"""

from __future__ import annotations

import re
import time

import httpx

from .. import compliance, config
from ..db import norm_mpn

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _num(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"\d[\d',.\s]*", str(v))
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(0))
    return int(digits) if digits else None


def _price(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\d+(?:[.,]\d+)?", str(v or "").replace("'", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def find_coo(obj) -> str:
    """Searches any nested dict/list for a country-of-origin style key."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kk = re.sub(r"[^a-z]", "", str(k).lower())
            if (
                kk in ("countryoforigin", "coo", "origincountry", "countryoforigincode")
                and isinstance(v, str)
                and v.strip()
            ):
                return v.strip()
            if (
                kk in ("compliancename", "name")
                and isinstance(v, str)
                and "country" in v.lower()
                and "origin" in v.lower()
            ):
                val = obj.get("ComplianceValue") or obj.get("Value") or obj.get("value")
                if isinstance(val, str) and val.strip():
                    return val.strip()
        for v in obj.values():
            r = find_coo(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_coo(v)
            if r:
                return r
    return ""


# ------------------------------------------------------------------------------------------ DigiKey
class DigiKey:
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    BASE = "https://api.digikey.com/products/v4"

    def __init__(self, client: httpx.Client | None = None):
        self.c = client or httpx.Client(timeout=TIMEOUT)
        self._tok, self._exp = None, 0.0

    @staticmethod
    def enabled() -> bool:
        cfg = config.settings
        return bool(cfg.digikey_client_id and cfg.digikey_client_secret)

    def _token(self) -> str:
        if self._tok and self._exp > time.time():
            return self._tok
        cfg = config.settings
        r = self.c.post(
            self.TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": cfg.digikey_client_id,
                "client_secret": cfg.digikey_client_secret,
            },
        )
        r.raise_for_status()
        j = r.json()
        self._tok, self._exp = j["access_token"], time.time() + int(j.get("expires_in", 600)) - 60
        return self._tok

    def _headers(self) -> dict:
        cfg = config.settings
        return {
            "Authorization": f"Bearer {self._token()}",
            "X-DIGIKEY-Client-Id": cfg.digikey_client_id,
            "X-DIGIKEY-Locale-Site": cfg.digikey_site,
            "X-DIGIKEY-Locale-Language": cfg.digikey_language,
            "X-DIGIKEY-Locale-Currency": cfg.currency,
            "Accept": "application/json",
        }

    def _get(self, method: str, url: str, **kw) -> httpx.Response:
        # Up to three tries; the last response is returned whatever its status.
        for _ in range(2):
            r = self.c.request(method, url, headers=self._headers(), **kw)
            if r.status_code != 429:
                return r
            time.sleep(int(r.headers.get("Retry-After", "10")))
        return self.c.request(method, url, headers=self._headers(), **kw)

    def lookup(self, mpn: str) -> dict | None:
        r = self._get("GET", f"{self.BASE}/search/{_q(mpn)}/productdetails")
        prod = None
        if r.status_code == 200:
            prod = (r.json() or {}).get("Product")
        if prod is None or norm_mpn(prod.get("ManufacturerProductNumber", "")) != norm_mpn(mpn):
            r = self._get("POST", f"{self.BASE}/search/keyword", json={"Keywords": mpn, "Limit": 10, "Offset": 0})
            if r.status_code != 200:
                if r.status_code in (401, 403):
                    raise RuntimeError(f"DigiKey refused the request ({r.status_code}): {r.text[:200]}")
                return None
            prods = (r.json() or {}).get("Products") or []
            prod = next((p for p in prods if norm_mpn(p.get("ManufacturerProductNumber", "")) == norm_mpn(mpn)), None)
        return self.normalise(prod) if prod else None

    @staticmethod
    def normalise(p: dict) -> dict:
        cls = p.get("Classifications") or {}
        weeks = _num(p.get("ManufacturerLeadWeeks"))
        offers = []
        for v in p.get("ProductVariations") or []:
            prices = [
                {
                    "qty": b.get("BreakQuantity"),
                    "price": _price(b.get("UnitPrice")),
                    "currency": config.settings.currency,
                }
                for b in v.get("StandardPricing") or []
            ]
            offers.append(
                {
                    "source": "digikey",
                    "seller": "DigiKey",
                    "authorized": True,
                    "sku": v.get("DigiKeyProductNumber", ""),
                    "stock": _num(v.get("QuantityAvailableforPackageType")),
                    "moq": _num(v.get("MinimumOrderQuantity")),
                    "packaging": ((v.get("PackageType") or {}).get("Name")) or "",
                    "lead_days": weeks * 7 if weeks else None,
                    "prices": prices,
                    "url": p.get("ProductUrl") or "",
                }
            )
        if not offers:
            offers.append(
                {
                    "source": "digikey",
                    "seller": "DigiKey",
                    "authorized": True,
                    "sku": "",
                    "stock": _num(p.get("QuantityAvailable")),
                    "lead_days": weeks * 7 if weeks else None,
                    "prices": [],
                    "url": p.get("ProductUrl") or "",
                }
            )
        return {
            "mpn": p.get("ManufacturerProductNumber") or "",
            "manufacturer": (p.get("Manufacturer") or {}).get("Name") or "",
            "description": ((p.get("Description") or {}).get("ProductDescription")) or "",
            "lead_days": weeks * 7 if weeks else None,
            "lifecycle": ((p.get("ProductStatus") or {}).get("Status")) or "",
            "hts": cls.get("HtsusCode") or "",
            "eccn": cls.get("ExportControlClassNumber") or "",
            "rohs": cls.get("RohsStatus") or "",
            "reach": cls.get("ReachStatus") or "",
            "coo": find_coo(p),
            "datasheet": p.get("DatasheetUrl") or "",
            "offers": offers,
        }


def _q(mpn: str) -> str:
    from urllib.parse import quote

    return quote(mpn, safe="")


# ------------------------------------------------------------------------------------------ Mouser
class Mouser:
    URL = "https://api.mouser.com/api/v1/search/partnumber"
    MIN_INTERVAL = 2.1  # Mouser allows ~30 calls/minute

    def __init__(self, client: httpx.Client | None = None, sleep=time.sleep):
        self.c = client or httpx.Client(timeout=TIMEOUT)
        self._last = 0.0
        self._sleep = sleep

    @staticmethod
    def enabled() -> bool:
        return bool(config.settings.mouser_api_key)

    def lookup_many(self, mpns: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(mpns), 10):
            chunk = mpns[i : i + 10]
            wait = self.MIN_INTERVAL - (time.time() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.time()
            r = self.c.post(
                self.URL,
                params={"apiKey": config.settings.mouser_api_key},
                json={"SearchByPartRequest": {"mouserPartNumber": "|".join(chunk), "partSearchOptions": "Exact"}},
            )
            if r.status_code != 200:
                raise RuntimeError(f"Mouser HTTP {r.status_code}: {r.text[:200]}")
            j = r.json() or {}
            errs = j.get("Errors") or []
            if errs and not (j.get("SearchResults") or {}).get("Parts"):
                raise RuntimeError("Mouser: " + "; ".join(e.get("Message", str(e)) for e in errs))
            for p in (j.get("SearchResults") or {}).get("Parts") or []:
                k = norm_mpn(p.get("ManufacturerPartNumber", ""))
                if k in {norm_mpn(m) for m in chunk} and k not in out:
                    out[k] = self.normalise(p)
        return out

    @staticmethod
    def normalise(p: dict) -> dict:
        comp = {
            (c.get("ComplianceName") or "").upper(): c.get("ComplianceValue") or ""
            for c in p.get("ProductCompliance") or []
        }
        lead = p.get("LeadTime") or ""
        lead_days = None
        m = re.search(r"(\d+)\s*(day|week)", lead, re.I)
        if m:
            lead_days = int(m.group(1)) * (7 if m.group(2).lower().startswith("week") else 1)
        on_order = sum(_num(x.get("Quantity")) or 0 for x in p.get("AvailabilityOnOrder") or []) or None
        prices = [
            {"qty": b.get("Quantity"), "price": _price(b.get("Price")), "currency": b.get("Currency") or ""}
            for b in p.get("PriceBreaks") or []
        ]
        stock = _num(p.get("AvailabilityInStock"))
        if stock is None:
            stock = _num(p.get("Availability")) if "stock" in (p.get("Availability") or "").lower() else 0
        return {
            "mpn": p.get("ManufacturerPartNumber") or "",
            "manufacturer": p.get("Manufacturer") or "",
            "description": p.get("Description") or "",
            "lead_days": lead_days,
            "lifecycle": p.get("LifecycleStatus")
            or ("Discontinued" if str(p.get("IsDiscontinued")).lower() == "true" else ""),
            "hts": comp.get("USHTS") or comp.get("TARIC") or comp.get("CNHTS") or "",
            "eccn": comp.get("ECCN") or "",
            "rohs": p.get("ROHSStatus") or "",
            "reach": compliance.find(p.get("ProductCompliance") or [], "reach"),
            "coo": find_coo(p),
            "datasheet": p.get("DataSheetUrl") or "",
            "offers": [
                {
                    "source": "mouser",
                    "seller": "Mouser",
                    "authorized": True,
                    "sku": p.get("MouserPartNumber") or "",
                    "stock": stock,
                    "on_order": on_order,
                    "moq": _num(p.get("Min")),
                    "packaging": "",
                    "lead_days": lead_days,
                    "prices": prices,
                    "url": p.get("ProductDetailUrl") or "",
                }
            ],
        }


# ------------------------------------------------------------------------------------------ Farnell / element14
class Farnell:
    """element14 Product Search API (REST). Free key at partner.element14.com.
    Provides stock, lead time (``stock.leastLeadTime``, days), price breaks, MOQ and country of origin."""

    URL = "https://api.element14.com/catalog/products"
    MIN_INTERVAL = 0.6  # ~2 calls/second
    _RELEASE = {"1": "Announced", "2": "Active", "4": "Active", "6": "EOL", "7": "Discontinued"}
    _STATUS = {
        -1: "awaiting delivery",
        -2: "short lead time",
        -3: "no longer manufactured",
        -4: "not stocked",
        -7: "on demand",
    }

    def __init__(self, client: httpx.Client | None = None, sleep=time.sleep):
        self.c = client or httpx.Client(timeout=TIMEOUT)
        self._last = 0.0
        self._sleep = sleep

    @staticmethod
    def enabled() -> bool:
        return bool(config.settings.farnell_api_key)

    def lookup(self, mpn: str) -> dict | None:
        wait = self.MIN_INTERVAL - (time.time() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.time()
        cfg = config.settings
        r = self.c.get(
            self.URL,
            params={
                "term": f"manuPartNum:{mpn}",
                "storeInfo.id": cfg.farnell_store,
                "resultsSettings.offset": 0,
                "resultsSettings.numberOfResults": 10,
                "resultsSettings.responseGroup": "large",
                "callInfo.responseDataFormat": "json",
                "callInfo.apiKey": cfg.farnell_api_key,
                "versionNumber": "1.4",
            },
        )
        if r.status_code in (401, 403):
            raise RuntimeError(f"Farnell refused the request ({r.status_code}): {r.text[:200]}")
        if r.status_code != 200:
            return None
        j = r.json() or {}
        ret = j.get("manufacturerPartNumberSearchReturn") or next((v for v in j.values() if isinstance(v, dict)), {})
        prods = [
            p
            for p in ret.get("products") or []
            if norm_mpn(p.get("translatedManufacturerPartNumber", "")) == norm_mpn(mpn)
        ]
        return self.normalise(prods) if prods else None

    @staticmethod
    def warehouses(st: dict) -> str:
        """'Liège, Belgium (1,200) · Leeds, United Kingdom (300)' from stock.breakdown / regionalBreakdown."""
        from ..warehouses import farnell_code

        out: dict[str, int | None] = {}
        for b in st.get("breakdown") or st.get("regionalBreakdown") or []:
            place = farnell_code(str(b.get("warehouse") or b.get("region") or ""))
            qty = _num(b.get("inv", b.get("level")))
            if place and qty != 0:
                out[place] = (out.get(place) or 0) + qty if qty is not None else out.get(place)
        return " · ".join(f"{k} ({v:,})" if v else k for k, v in out.items())

    @classmethod
    def normalise(cls, prods: list[dict]) -> dict:
        cur = config.settings.currency
        offers, lead_all = [], []
        for p in prods:
            st = p.get("stock") or {}
            lead = _num(st.get("leastLeadTime"))
            if lead:
                lead_all.append(lead)
            status = st.get("status")
            prices = [
                {"qty": x.get("from"), "price": _price(x.get("cost")), "currency": cur} for x in p.get("prices") or []
            ]
            offers.append(
                {
                    "source": "farnell",
                    "seller": "Farnell",
                    "authorized": True,
                    "sku": p.get("sku") or "",
                    "stock": _num(st.get("level")),
                    "stock_note": cls._STATUS.get(status, "") if isinstance(status, int) else "",
                    "moq": _num(p.get("translatedMinimumOrderQuality")),
                    "packaging": p.get("packSize")
                    and f"{p.get('packSize')} {p.get('unitOfMeasure') or ''}".strip()
                    or "",
                    "lead_days": lead,
                    "prices": prices,
                    "url": p.get("productURL") or "",
                    "warehouse": cls.warehouses(st),
                }
            )
        p0 = prods[0]
        ds = next((d.get("url") for d in p0.get("datasheets") or [] if d.get("url")), "")
        return {
            "mpn": p0.get("translatedManufacturerPartNumber") or "",
            "manufacturer": p0.get("brandName") or p0.get("vendorName") or "",
            "description": p0.get("displayName") or "",
            "lead_days": min(lead_all) if lead_all else None,
            "lifecycle": cls._RELEASE.get(str(p0.get("releaseStatusCode") or ""), ""),
            "coo": (p0.get("countryOfOrigin") or "").strip(),
            "rohs": p0.get("rohsStatusCode") or "",
            "reach": compliance.find(p0, "reach"),
            "hts": "",
            "eccn": "",
            "datasheet": ds,
            "offers": offers,
        }
