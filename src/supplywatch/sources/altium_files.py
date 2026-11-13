"""Reads BOMs straight from Altium project files (no Altium licence, no Altium 365).

* ``.PrjPcb``  INI file: schematic documents, variants (not-fitted parts), BomDoc.
* ``.SchDoc``  OLE compound file; the ``FileHeader`` stream holds length-prefixed
                ``|KEY=VALUE|`` records. Components are RECORD=1, their designator RECORD=34,
                parameters RECORD=41, footprints RECORD=45 (under RECORD=44).
* ``.BomDoc``  ActiveBOM text file: part choices for managed parts (used when the schematic
                has no MPN parameters, e.g. Altium 365 library parts).
"""

from __future__ import annotations

import configparser
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

import olefile

from .upload import _col_role

# COMPONENTKIND: 0 Standard, 1 Mechanical, 2 Graphical, 3 Net tie (in BOM), 4 Net tie, 5 Standard (no BOM), 6 Jumper
BOM_KINDS = {0, 1, 3, 6}
_REPEAT = re.compile(r"repeat\s*\(", re.I)


def _decode(b: bytes) -> str:
    return b.rstrip(b"\0").decode("cp1252", errors="replace")


def read_records(path: Path) -> list[dict]:
    """Returns the text records after the header (index = OWNERINDEX). Binary records become {}."""
    with olefile.OleFileIO(str(path)) as ole:
        data = ole.openstream("FileHeader").read()
    out, i, first = [], 0, True
    while i + 4 <= len(data):
        n = struct.unpack("<I", data[i : i + 4])[0]
        binary, n = n >> 24, n & 0xFFFFFF
        payload = data[i + 4 : i + 4 + n]
        i += 4 + n
        rec: dict = {}
        if not binary:
            txt = _decode(payload)
            for part in txt.split("|"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    rec[k.strip().upper()] = v
            for k in [k for k in rec if k.startswith("%UTF8%")]:
                try:
                    rec[k[6:]] = rec[k].encode("cp1252", errors="replace").decode("utf-8")
                except UnicodeDecodeError:
                    pass
        if first:
            first = False
            continue
        out.append(rec)
    return out


@dataclass
class Component:
    designator: str
    unique_id: str
    kind: int
    lib_ref: str
    design_item_id: str
    description: str
    comment: str = ""
    footprint: str = ""
    params: dict = field(default_factory=dict)
    sheet: str = ""


def components_in_sheet(path: Path, log: list | None = None) -> list[Component]:
    recs = read_records(path)
    comps: dict[int, Component] = {}
    current_part: dict[int, str] = {}
    for idx, r in enumerate(recs):
        if r.get("RECORD") == "1":
            try:
                # newer Altium versions store kinds the old field can't express in COMPONENTKINDVERSIONn
                kind = int(
                    r.get("COMPONENTKINDVERSION3") or r.get("COMPONENTKINDVERSION2") or r.get("COMPONENTKIND") or 0
                )
            except ValueError:
                kind = 0
            comps[idx] = Component(
                designator="",
                unique_id=r.get("UNIQUEID", ""),
                kind=kind,
                lib_ref=r.get("LIBREFERENCE", ""),
                design_item_id=r.get("DESIGNITEMID", ""),
                description=r.get("COMPONENTDESCRIPTION", ""),
                sheet=path.name,
            )
            current_part[idx] = r.get("CURRENTPARTID", "")
        elif r.get("RECORD") == "32" and log is not None and _REPEAT.search(r.get("TEXT", "")):
            log.append(
                f"warning: {path.name}: multi-channel sheet '{r.get('TEXT')}' - channel designators are "
                "assigned at compile time and are not expanded; quantities under it count once"
            )
    impl_owner: dict[int, int] = {}
    for idx, r in enumerate(recs):
        owner = r.get("OWNERINDEX")
        if owner is None:
            continue
        try:
            owner = int(owner)
        except ValueError:
            continue
        typ = r.get("RECORD")
        if typ == "44" and owner in comps:
            impl_owner[idx] = owner
            continue
        if typ == "45" and owner in impl_owner:
            if r.get("MODELTYPE", "").upper() == "PCBLIB" and r.get("ISCURRENT", "").upper() == "T":
                comps[impl_owner[owner]].footprint = r.get("MODELNAME", "")
            continue
        if owner not in comps:
            continue
        opid = r.get("OWNERPARTID", "-1")
        if opid not in ("-1", "", current_part.get(owner, "")):
            continue
        c = comps[owner]
        if typ == "34":
            c.designator = r.get("TEXT", "").strip()
        elif typ == "41":
            name = r.get("NAME", "").strip()
            if name:
                c.params[name] = r.get("TEXT", "")
    # resolve "=OtherParam" references
    for c in comps.values():
        for k, v in list(c.params.items()):
            if v.startswith("=") and v[1:] in c.params:
                c.params[k] = c.params[v[1:]]
        c.comment = c.params.get("Comment", "")
        if c.comment.startswith("=") and c.comment[1:] in c.params:
            c.comment = c.params[c.comment[1:]]
    return list(comps.values())


def choices_from_params(params: dict) -> list[dict]:
    """Manufacturer/MPN pairs from parameters, e.g. '1st source - MPN' or 'Manufacturer Part Number 1'."""
    mpns, mfrs = {}, {}
    for k, v in params.items():
        v = (v or "").strip()
        if not v or v in ("-", "N/A", "n/a", "NA", "*"):
            continue
        role, n = _col_role(k)
        if role == "mpn":
            mpns.setdefault(n, v)
        elif role == "mfr":
            mfrs.setdefault(n, v)
    return [{"mfr": mfrs.get(n, ""), "mpn": mpns[n]} for n in sorted(mpns)]


def parse_bomdoc(path: Path) -> dict[str, list[dict]]:
    """DESIGNITEMID -> part choices (ActiveBOM's chosen solution first)."""
    items: dict[str, dict] = {}
    cur = None
    for line in path.read_bytes().decode("cp1252", errors="replace").splitlines():
        if not line.startswith("|RECORD="):
            continue
        d = {}
        for part in line.split("|")[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                d[k] = v
        rec = d.get("RECORD")
        if rec == "CatalogItem":
            cur = items.setdefault(d.get("DESIGNITEMID", ""), {"choices": [], "solution": None})
        elif rec == "PartChoice" and cur is not None:
            ch = {"mfr": d.get("MANUFACTURER", ""), "mpn": d.get("MANUFACTURERPARTNO", "").strip()}
            if ch["mpn"] and ch not in cur["choices"]:
                cur["choices"].append(ch)
        elif rec == "Solution" and cur is not None and cur["solution"] is None and d.get("MANUFACTURERPARTNO"):
            cur["solution"] = {"mfr": d.get("MANUFACTURER", ""), "mpn": d["MANUFACTURERPARTNO"].strip()}
    out = {}
    for k, v in items.items():
        ch = v["choices"]
        if v["solution"]:
            ch = [v["solution"]] + [c for c in ch if c["mpn"] != v["solution"]["mpn"]]
        out[k] = ch
    return out


@dataclass
class ProjectFile:
    path: Path
    name: str
    schematics: list[Path]
    bomdocs: list[Path]
    variants: list[dict]  # {"name", "not_fitted": set(designators)}


def read_prjpcb(path: Path) -> ProjectFile:
    cp = configparser.ConfigParser(strict=False, interpolation=None, allow_no_value=True, delimiters=("=",))
    cp.optionxform = str  # ty: ignore[invalid-assignment]  # keep key case
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    cp.read_string(text)
    base = path.parent
    sch, bom, variants = [], [], []
    for sec in cp.sections():
        if re.fullmatch(r"Document\d+", sec):
            rel = cp[sec].get("DocumentPath", "")
            p = base.joinpath(*PureWindowsPath(rel).parts)
            if rel.lower().endswith(".schdoc"):
                sch.append(p)
            elif rel.lower().endswith(".bomdoc"):
                bom.append(p)
        elif re.fullmatch(r"ProjectVariant\d+", sec):
            s = cp[sec]
            nf, alt = set(), {}
            for k, v in s.items():
                if re.fullmatch(r"Variation\d+", k):
                    f = dict(x.split("=", 1) for x in v.split("|") if "=" in x)
                    if f.get("Kind") == "1":
                        nf.add(f.get("Designator", ""))
                    elif f.get("Kind") == "2" and f.get("AlternatePart"):
                        alt[f.get("Designator", "")] = f["AlternatePart"]
            variants.append({"name": s.get("Description", sec), "not_fitted": nf, "alternate": alt})
    return ProjectFile(path=path, name=path.stem, schematics=sch, bomdocs=bom, variants=variants)


def _natkey(d: str):
    m = re.match(r"([A-Za-z_]+)(\d+)(.*)", d or "")
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (d, 0, "")


def project_boms(prj: Path, log: list) -> list[tuple[str, list[dict]]]:
    """Returns [(variant name, BOM lines)]; the first entry is the base design without variations."""
    pf = read_prjpcb(prj)
    bom_choices: dict[str, list[dict]] = {}
    for b in pf.bomdocs:
        if b.exists():
            bom_choices.update(parse_bomdoc(b))
    comps: dict[str, Component] = {}
    for sch in pf.schematics:
        if not sch.exists():
            log.append(f"warning: {pf.name}: schematic {sch.name} listed in project but missing")
            continue
        for c in components_in_sheet(sch, log):
            if c.kind not in BOM_KINDS or not c.designator or c.designator.endswith("?"):
                if c.kind in BOM_KINDS and c.designator.endswith("?"):
                    log.append(f"warning: {pf.name}: unannotated component {c.designator} ({c.lib_ref}) skipped")
                continue
            comps.setdefault(c.designator, c)  # multi-part symbols share a designator

    def lines_for(not_fitted: set[str]) -> list[dict]:
        groups: dict[str, dict] = {}
        for des, c in comps.items():
            if des in not_fitted:
                continue
            choices = choices_from_params(c.params)
            source = "schematic"
            if not choices and c.design_item_id in bom_choices:
                choices, source = bom_choices[c.design_item_id], "activebom"
            key = c.design_item_id or c.lib_ref + "|" + c.comment + "|" + (choices[0]["mpn"] if choices else "")
            g = groups.setdefault(
                key,
                {
                    "component": c.design_item_id or c.lib_ref,
                    "description": c.params.get("Description") or c.description,
                    "designators": [],
                    "quantity": 0,
                    "part_choices": choices,
                    "footprint": c.footprint,
                    "comment": c.comment,
                    "mpn_source": source,
                },
            )
            g["designators"].append(des)
            g["quantity"] += 1
        out = list(groups.values())
        for g in out:
            g["designators"].sort(key=_natkey)
        out.sort(key=lambda g: _natkey(g["designators"][0]))
        return out

    result = [("[No Variations]", lines_for(set()))]
    for v in pf.variants:
        result.append((v["name"], lines_for(v["not_fitted"])))
    return result
