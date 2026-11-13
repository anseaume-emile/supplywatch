"""Normalises country-of-origin values ('CN', 'CHN', 'China') to one display name."""

from __future__ import annotations

import pycountry

_ALIASES = {
    "USA": "United States",
    "US": "United States",
    "UK": "United Kingdom",
    "KOREA": "Korea, Republic of",
    "SOUTH KOREA": "Korea, Republic of",
    "TAIWAN": "Taiwan",
    "TW": "Taiwan",
    "TWN": "Taiwan",
}


def country_name(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    up = v.upper()
    if up in _ALIASES:
        return _ALIASES[up]
    try:
        if len(v) == 2:
            return pycountry.countries.get(alpha_2=up).name
        if len(v) == 3:
            return pycountry.countries.get(alpha_3=up).name
    except (AttributeError, KeyError, LookupError):
        pass
    return v
