"""Imports project BOMs from an Altium 365 workspace through the Nexar design API."""

from __future__ import annotations

import re

import httpx
from sqlalchemy import select

from .. import config, db
from ..db import BomLine, Project, utcnow
from . import nexar

Q_WORKSPACES = "query { desWorkspaces { id name url } }"
Q_LOCATION = """query($url: String!) { desWorkspaceByUrl(workspaceUrl: $url) { location { name apiServiceUrl } } }"""
Q_PROJECTS = """query($id: ID!) { desWorkspaceById(id: $id) { projects { id name description } } }"""
Q_BOM = """
query($id: ID!) {
  desProjectById(id: $id) {
    id name description
    design { workInProgress { variants { name bom { bomItems {
      quantity
      bomItemInstances { designator }
      component { name description manufacturerParts { companyName partNumber } }
    } } } } }
  }
}"""


def _query_tolerant(query: str, variables: dict, token: str, url: str, client: httpx.Client, log: list) -> dict:
    """Runs a query; if the schema rejects a field, drops that field and retries (schema drift guard)."""
    for _ in range(5):
        data, errors = nexar.graphql(query, variables, token, url, client)
        bad = nexar.rejected_fields(errors)
        if not bad:
            if errors and not data:
                raise nexar.NexarError(nexar.error_text(errors))
            if errors:
                log.append(f"warning: {nexar.error_text(errors)}")
            return data
        for f in set(bad):
            log.append(f"schema: field '{f}' not available, retrying without it")
            query = nexar.strip_field(query, f)
    raise nexar.NexarError("Could not build a query the Nexar schema accepts.")


def _line_key(item: dict) -> str:
    comp = item.get("component") or {}
    return comp.get("name") or ""


def parse_variant_bom(bom_items: list[dict]) -> list[dict]:
    lines = []
    for it in bom_items or []:
        comp = it.get("component") or {}
        des = [i.get("designator") for i in (it.get("bomItemInstances") or []) if i.get("designator")]
        qty = it.get("quantity")
        try:
            qty = int(float(qty)) if qty is not None else len(des)
        except (TypeError, ValueError):
            qty = len(des)
        choices = []
        for mp in comp.get("manufacturerParts") or []:
            if mp and mp.get("partNumber"):
                choices.append({"mfr": mp.get("companyName") or "", "mpn": mp["partNumber"].strip()})
        if qty == 0 and not des:
            continue
        lines.append(
            {
                "component": comp.get("name") or "",
                "description": comp.get("description") or "",
                "designators": sorted(des, key=_natkey),
                "quantity": qty,
                "part_choices": choices,
            }
        )
    return lines


def _natkey(d: str):
    m = re.match(r"([A-Za-z_]+)(\d+)(.*)", d or "")
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (d, 0, "")


def replace_lines(s, project: Project, lines: list[dict]):
    project.lines.clear()
    s.flush()
    for ln in lines:
        project.lines.append(BomLine(**ln))
    project.synced_at = utcnow()


def sync(log: list, client: httpx.Client | None = None) -> dict:
    cfg = config.settings
    c = client or httpx.Client(timeout=nexar.TIMEOUT)
    token = nexar.design_token(c)

    data, errors = nexar.graphql(Q_WORKSPACES, {}, token, client=c)
    wss = data.get("desWorkspaces") or []
    if not wss:
        raise nexar.NexarError("The signed-in Altium account has no Altium 365 workspaces. " + nexar.error_text(errors))
    ws = next(
        (w for w in wss if cfg.a365_workspace_url and w.get("url", "").rstrip("/") == cfg.a365_workspace_url), None
    )
    if ws is None:
        if cfg.a365_workspace_url:
            raise nexar.NexarError(
                f"Workspace {cfg.a365_workspace_url} not found. Available: " + ", ".join(w.get("url", "") for w in wss)
            )
        ws = wss[0]
    log.append(f"workspace: {ws.get('name')} ({ws.get('url')})")

    api_url = nexar.GRAPHQL_URL
    loc, _ = nexar.graphql(Q_LOCATION, {"url": ws["url"]}, token, client=c)
    svc = (((loc.get("desWorkspaceByUrl") or {}).get("location") or {}).get("apiServiceUrl") or "").strip()
    if svc:
        api_url = svc if svc.endswith("/graphql") else svc.rstrip("/") + "/graphql"
        log.append(f"regional endpoint: {api_url}")

    pdata = _query_tolerant(Q_PROJECTS, {"id": ws["id"]}, token, api_url, c, log)
    projects = (pdata.get("desWorkspaceById") or {}).get("projects") or []
    if cfg.a365_project_filter:
        rx = re.compile(cfg.a365_project_filter, re.I)
        projects = [p for p in projects if rx.search(p.get("name") or "")]
    log.append(f"projects in scope: {len(projects)}")

    seen, failed, n_lines = set(), set(), 0
    for p in projects:
        try:
            bd = _query_tolerant(Q_BOM, {"id": p["id"]}, token, api_url, c, log)
        except nexar.NexarError as e:
            log.append(f"error: {p.get('name')}: {e}")
            failed.add(f"a365:{p['id']}::")
            continue
        proj = bd.get("desProjectById") or {}
        variants = (((proj.get("design") or {}).get("workInProgress") or {}).get("variants")) or []
        if not variants:
            log.append(f"skip: {p.get('name')} has no design data")
            continue
        with db.session() as s:
            for vi, v in enumerate(variants):
                vname = v.get("name") or "[No Variations]"
                ext = f"a365:{p['id']}::{vname}"
                seen.add(ext)
                row = s.scalar(select(Project).where(Project.external_id == ext))
                if not row:
                    row = Project(
                        source="a365", external_id=ext, name=p.get("name") or "", variant=vname, active=(vi == 0)
                    )
                    s.add(row)
                row.name = p.get("name") or row.name
                row.description = p.get("description") or ""
                lines = parse_variant_bom(((v.get("bom") or {}).get("bomItems")) or [])
                replace_lines(s, row, lines)
                n_lines += len(lines)
        log.append(f"synced: {p.get('name')} ({len(variants)} variant(s))")

    with db.session() as s:
        stale = s.scalars(select(Project).where(Project.source == "a365")).all()
        removed = 0
        for row in stale:
            if row.external_id in seen or any(row.external_id.startswith(f) for f in failed):
                continue
            if row.active:
                row.active = False
                removed += 1
        if removed:
            log.append(f"deactivated {removed} project variant(s) no longer in the workspace")
    return {"projects": len(projects), "lines": n_lines, "failed": len(failed)}
