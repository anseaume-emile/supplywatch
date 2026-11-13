"""Fallback importer for projects that are not in Altium 365: an Altium BOM export (CSV).

Recognises the usual Altium report columns (Designator, Quantity, Manufacturer 1,
Manufacturer Part Number 1, ...) and CDDS-style "1st source - MPN" parameters.
"""

from __future__ import annotations

import csv
import io
import re

from sqlalchemy import select

from .. import db
from ..db import Project
from .altium365 import _natkey, replace_lines

_ORD = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _col_role(h: str):
    """Returns (role, index) for a header; index orders alternate sources."""
    k = re.sub(r"\s+", " ", h.strip().lower())
    m = re.match(r"(1st|2nd|3rd|4th|5th)\s*s\w*\s*-?\s*(.*)", k)
    if m:
        n, rest = _ORD[m.group(1)], m.group(2)
        if rest in ("mpn", "manufacturer part number", "part number"):
            return ("mpn", n)
        if rest.startswith("manufacturer"):
            return ("mfr", n)
        return (None, 0)
    m = re.match(r"(manufacturer part number|manufacturer part|mpn|mfr part number|mfr\.? ?pn)\s*(\d*)$", k)
    if m:
        return ("mpn", int(m.group(2) or 1))
    m = re.match(r"(manufacturer|mfr|manufacturer name)\s*(\d*)$", k)
    if m:
        return ("mfr", int(m.group(2) or 1))
    if k in ("designator", "designators", "reference", "refdes"):
        return ("des", 0)
    if k in ("quantity", "qty", "count"):
        return ("qty", 0)
    if k in ("description",):
        return ("desc", 0)
    if k in ("name", "library reference", "libref", "design item id", "revision id", "comment", "part number", "cmp"):
        # Altium ActiveBOM exports put the managed item ID (e.g. CMP-001-00001-5) in "Revision ID"
        return (
            "comp",
            {
                "design item id": 0,
                "revision id": 0,
                "cmp": 0,
                "library reference": 1,
                "libref": 1,
                "name": 2,
                "part number": 3,
                "comment": 4,
            }.get(k, 5),
        )
    return (None, 0)


def parse_csv(raw: bytes) -> list[dict]:
    text = _decode(raw)
    sample = text[:4000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    hi = next((i for i, r in enumerate(rows[:40]) if any(_col_role(c)[0] == "des" for c in r)), None)
    if hi is None:
        raise ValueError("No 'Designator' column found. Export the BOM from Altium with a Designator column.")
    roles = [_col_role(c) for c in rows[hi]]
    comp_cols = sorted([(r[1], i) for i, r in enumerate(roles) if r[0] == "comp"])
    lines = []
    for r in rows[hi + 1 :]:
        if not any(x.strip() for x in r):
            continue
        get = lambda i, r=r: r[i].strip() if i < len(r) else ""  # noqa: E731
        des, qty, desc, comp = [], None, "", ""
        mpns, mfrs = {}, {}
        for i, (role, n) in enumerate(roles):
            v = get(i)
            if not v or v in ("-", "N/A", "n/a"):
                continue
            if role == "des":
                des = [d for d in re.split(r"[,;\s]+", v) if d]
            elif role == "qty":
                try:
                    qty = int(float(v.replace(",", ".")))
                except ValueError:
                    pass
            elif role == "desc":
                desc = v
            elif role == "mpn":
                mpns.setdefault(n, v)
            elif role == "mfr":
                mfrs.setdefault(n, v)
        for _, i in comp_cols:
            if get(i):
                comp = get(i)
                break
        choices = [{"mfr": mfrs.get(n, ""), "mpn": mpns[n]} for n in sorted(mpns)]
        if not des and qty in (None, 0):
            continue
        lines.append(
            {
                "component": comp,
                "description": desc,
                "designators": sorted(des, key=_natkey),
                "quantity": qty if qty is not None else len(des),
                "part_choices": choices,
            }
        )
    return lines


def import_csv(name: str, raw: bytes, variant: str = "") -> dict:
    lines = parse_csv(raw)
    ext = f"upload:{name.strip().lower()}::{variant.strip().lower()}"
    with db.session() as s:
        row = s.scalar(select(Project).where(Project.external_id == ext))
        if not row:
            row = Project(source="upload", external_id=ext, name=name.strip(), variant=variant.strip(), active=True)
            s.add(row)
        replace_lines(s, row, lines)
    return {"lines": len(lines), "with_mpn": sum(1 for ln in lines if ln["part_choices"])}
