"""Where an offer ships from. APIs rarely say, so the best available answer is used and labelled:
api (warehouse reported by the source) > typical (the distributor's main distribution centre) > seller country."""

from __future__ import annotations

import re

from . import config
from .countries import country_name

# Main distribution centres of distributors that ship (almost) everything from one place.
_TYPICAL = [
    (("digikey", "digi-key"), "Thief River Falls MN, United States"),
    (("mouser",), "Mansfield TX, United States"),
    (("newark",), "Gaffney SC, United States"),
    (("tme", "transfer multisort"), "Łódź, Poland"),
    (("lcsc",), "Shenzhen, China"),
]

# Farnell / element14 warehouse and region codes (stock.breakdown / regionalBreakdown).
_FARNELL_CODES = {
    "UK": "Leeds, United Kingdom",
    "GB": "Leeds, United Kingdom",
    "BE": "Liège, Belgium",
    "LIEGE": "Liège, Belgium",
    "EU": "Liège, Belgium",
    "US": "Gaffney SC, United States",
    "SG": "Singapore",
    "APAC": "Singapore",
}


def farnell_code(code: str) -> str:
    c = (code or "").strip().upper()
    if not c:
        return ""
    if c in _FARNELL_CODES:
        return _FARNELL_CODES[c]
    base = c.rstrip("0123456789")
    if base in _FARNELL_CODES:
        return _FARNELL_CODES[base]
    name = country_name(base[:2]) if len(base) >= 2 else ""
    return name if name and name != base[:2] else code


def _farnell_store() -> str:
    store = (config.settings.farnell_store or "").lower()
    if store.startswith(("uk.", "cpc.")):
        return "Leeds, United Kingdom"
    if "newark" in store:
        return "Gaffney SC, United States"
    if "farnell" in store:
        return "Liège, Belgium"
    return ""


def typical(seller: str) -> str:
    s = (seller or "").lower()
    if "farnell" in s or "element14" in s:
        return _farnell_store()
    for names, place in _TYPICAL:
        if any(n in s for n in names):
            return place
    return ""


def locate(o) -> tuple[str, str]:
    """(location, basis) for an Offer row. basis: api | typical | seller_country | ''."""
    if o.warehouse:
        return o.warehouse, "api"
    t = typical(o.seller)
    if t:
        return t, "typical"
    if o.seller_country:
        return country_name(o.seller_country), "seller_country"
    return "", ""


# ------------------------------------------------------------------------------------------ regions
REGIONS = ["Americas", "Europe", "Asia-Pacific", "Rest of world", "Unlocated"]
_REGION_OF = {
    "Americas": [
        "United States",
        "Canada",
        "Mexico",
        "Brazil",
        "Argentina",
        "Chile",
        "Colombia",
        "Costa Rica",
        "Puerto Rico",
        "Peru",
        "Uruguay",
    ],
    "Europe": [
        "Austria",
        "Belgium",
        "Bulgaria",
        "Croatia",
        "Cyprus",
        "Czechia",
        "Czech Republic",
        "Denmark",
        "Estonia",
        "Finland",
        "France",
        "Germany",
        "Greece",
        "Hungary",
        "Iceland",
        "Ireland",
        "Italy",
        "Latvia",
        "Liechtenstein",
        "Lithuania",
        "Luxembourg",
        "Malta",
        "Netherlands",
        "Norway",
        "Poland",
        "Portugal",
        "Romania",
        "Serbia",
        "Slovakia",
        "Slovenia",
        "Spain",
        "Sweden",
        "Switzerland",
        "United Kingdom",
        "Ukraine",
    ],
    "Asia-Pacific": [
        "China",
        "Taiwan",
        "Taiwan, Province of China",
        "Hong Kong",
        "Japan",
        "Korea, Republic of",
        "Singapore",
        "Malaysia",
        "Thailand",
        "Viet Nam",
        "Vietnam",
        "Philippines",
        "India",
        "Indonesia",
        "Australia",
        "New Zealand",
    ],
}
_BY_COUNTRY = {c.lower(): r for r, cs in _REGION_OF.items() for c in cs}


def region(location: str) -> str:
    """Region of a location string such as 'Thief River Falls MN, United States' or 'Japan'."""
    loc = (location or "").strip().lower()
    if not loc:
        return "Unlocated"
    for name in sorted(_BY_COUNTRY, key=len, reverse=True):
        if loc == name or loc.endswith(", " + name):
            return _BY_COUNTRY[name]
    return "Rest of world"


_QTY = re.compile(r"^(.*?)(?: \(([\d,]+)\))?$")


def stock_by_region(o) -> dict[str, int]:
    """An offer's stock spread over regions. Uses per-warehouse quantities when the source reported them."""
    if not o.stock:
        return {}
    loc, _ = locate(o)
    parts = [_QTY.match(p.strip()).groups() for p in loc.split(" · ")] if loc else [("", None)]
    if len(parts) > 1 and all(q for _, q in parts):
        out: dict[str, int] = {}
        for place, q in parts:
            out[region(place)] = out.get(region(place), 0) + int(q.replace(",", ""))
        return out
    return {region(parts[0][0]): o.stock}
