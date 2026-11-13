"""RoHS and REACH statuses: distributor values ('ROHS3 Compliant', 'YES', 'REACH Affected', ...) to one label."""

from __future__ import annotations

import re

COMPLIANT = "Compliant"
EXEMPT = "Compliant (exemption)"
NOT_COMPLIANT = "Not compliant"
SVHC = "Contains SVHC"  # REACH: substance of very high concern above 0.1% w/w; must be declared, not banned

_UNKNOWN = re.compile(
    r"^(|-|n/?a|unknown|vendor undefined|not available|no data|tba|tbd|not applicable|"
    r"(rohs|reach) not applicable)$"
)
_NOT = re.compile(r"\b(not|non)\b[\s-]*(rohs\s*)?compliant|non-?compliant|^(no|n|false)$")
_YES = re.compile(
    r"compliant|^(yes|y|true)$|rohs\s*[23]?$|lead[\s-]*free|pb[\s-]*free|unaffected|no svhc|svhc[\s-]*free"
)


def normalise(kind: str, raw) -> str:
    """kind: 'rohs' or 'reach'. Returns one of the labels above, '' when unknown, or the raw text if unrecognised."""
    v = str(raw or "").strip()
    s = v.lower()
    if _UNKNOWN.match(s):
        return ""
    if kind == "reach" and (re.search(r"(?<!un)affected", s) or re.search(r"contains svhc|svhc (above|present)", s)):
        return SVHC
    if "exempt" in s:
        return EXEMPT
    if _NOT.search(s):
        return NOT_COMPLIANT
    if _YES.search(s):
        return COMPLIANT
    return v


def find(obj, word: str) -> str:
    """First value of a key containing `word` ('rohs' / 'reach'), searched depth-first; also matches
    name/value pairs such as {'ComplianceName': 'REACH', 'ComplianceValue': '...'} or {'Key': .., 'Value': ..}.
    A yes/no under an 'SVHC' key is turned into 'Contains SVHC' / 'No SVHC'."""

    def val(key: str, v) -> str:
        v = str(v).strip()
        if "svhc" in key.lower() and v.lower() in ("yes", "y", "true", "no", "n", "false"):
            return "Contains SVHC" if v.lower() in ("yes", "y", "true") else "No SVHC"
        return v

    if isinstance(obj, dict):
        for k, v in obj.items():
            if word in str(k).lower() and isinstance(v, (str, bool)) and str(v).strip():
                return val(str(k), v)
        name = next(
            (obj[k] for k in ("ComplianceName", "Key", "Name", "name", "key") if isinstance(obj.get(k), str)), ""
        )
        value = next(
            (obj[k] for k in ("ComplianceValue", "Value", "value", "displayValue") if obj.get(k) not in (None, "")),
            None,
        )
        if word in name.lower() and value is not None:
            return val(name, value)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = find(v, word)
                if r:
                    return r
    elif isinstance(obj, list):
        for v in obj:
            r = find(v, word)
            if r:
                return r
    return ""
