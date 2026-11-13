"""MCP server at /mcp: lets AI assistants (Claude, Cursor, ...) read supply data and make the same changes as the UI.

Streamable HTTP, stateless, JSON responses, so it runs in the app's single worker next to the web UI.
Every request needs `Authorization: Bearer <MCP_TOKEN>`; with no MCP_TOKEN set the endpoint is off (404).
"""

from __future__ import annotations

import functools
import hmac
import threading
from contextlib import asynccontextmanager

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy import select

from . import config, db, jobs, views
from .db import Project, Run, norm_mpn

WHO = "mcp"  # recorded as the user on runs and COO entries

mcp = FastMCP(
    "supplywatch",
    instructions=(
        "Procurement data for the CDDS Altium projects: per-part stock across authorised distributors, lead time, "
        "lifecycle, country of origin, and shortages against each project's build quantity. Parts are keyed by MPN. "
        "Call overview first; list_parts filters by issue flag, project or text."
    ),
)


def tool(fn):
    """Registers a sync tool and runs it in a worker thread, so DB work doesn't block the event loop."""

    @functools.wraps(fn)
    async def run(*a, **kw):
        return await anyio.to_thread.run_sync(functools.partial(fn, *a, **kw))

    return mcp.tool()(run)


def _project(p: dict) -> dict:
    return {k: v for k, v in p.items() if k != "lines"}


def _start(fn, *a):
    """Like main._bg: one job at a time, in a thread."""
    if jobs._lock.locked():
        raise ValueError("A sync is already running. Wait for it to finish (see sync_status).")

    def work():
        try:
            fn(*a)
        except jobs.Busy:
            pass

    threading.Thread(target=work, daemon=True).start()


# ------------------------------------------------------------------------------------------ read
@tool
def overview() -> dict:
    """Totals: parts per issue flag (with the flag's meaning), active projects with build quantities, thresholds."""
    with db.session() as s:
        d = views.build(s)
    counts: dict[str, int] = {}
    for p in d["parts"]:
        for f in p["flags"]:
            counts[f] = counts.get(f, 0) + 1
    return {
        "parts": len(d["parts"]),
        "issues": {f: {**d["flags"][f], "parts": n} for f, n in counts.items()},
        "projects": [_project(p) for p in d["projects"] if p["active"]],
        "thresholds": d["thresholds"],
        "currency": d["currency"],
    }


@tool
def list_parts(
    flag: str = "", project_id: int | None = None, query: str = "", include_alternates: bool = True, limit: int = 100
) -> dict:
    """Parts across active projects, worst issues first.

    flag: only parts with this issue, e.g. shortage, low_stock, lifecycle, long_lead, single_source, no_coo,
    not_found, stale, no_mpn, unchecked. project_id: only parts used in that project. query: text match on MPN,
    manufacturer or description. Each row has required qty, stock, lead_days, lifecycle, coo, best_price and flags.
    """
    q = query.strip().lower()
    with db.session() as s:
        d = views.build(s)
    rows = [
        p
        for p in d["parts"]
        if (not flag or flag in p["flags"])
        and (project_id is None or str(project_id) in p["used_in"])
        and (include_alternates or not p["is_alternate_only"])
        and (not q or q in f"{p['mpn']} {p['manufacturer']} {p['description']}".lower())
    ]
    return {"total": len(rows), "parts": rows[: max(1, min(limit, 1000))]}


@tool
def get_part(mpn: str) -> dict:
    """One part: every supplier offer (stock, MOQ, lead, price breaks, link), alternates, per-source values,
    COO override, and the last 90 days of stock history."""
    with db.session() as s:
        d = views.part_detail(s, mpn)
    if not d:
        raise ValueError(f"No part {mpn!r}. Use list_parts to find MPNs.")
    d.pop("projects", None)
    d["stock_history"] = d["stock_history"][-90:]
    return d


@tool
def list_projects() -> dict:
    """All projects and variants: id, name, variant, build_qty, active, revision, issue counts."""
    with db.session() as s:
        return {"projects": [_project(p) for p in views.build(s)["projects"]]}


@tool
def get_project_bom(project_id: int) -> dict:
    """A project's BOM lines: designators, quantity, part choices (primary first), need at build qty, flags."""
    with db.session() as s:
        p = next((p for p in views.build(s)["projects"] if p["id"] == project_id), None)
    if not p:
        raise ValueError(f"No project {project_id}. Use list_projects.")
    return p


@tool
def sync_status(runs: int = 10) -> dict:
    """Whether a sync is running, and the most recent BOM syncs and supply refreshes with their result."""
    with db.session() as s:
        rs = s.scalars(select(Run).order_by(Run.id.desc()).limit(max(1, min(runs, 50)))).all()
        return {
            "running": jobs._lock.locked(),
            "runs": [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "status": r.status,
                    "summary": r.summary,
                    "by": r.triggered_by,
                    "started": views._iso(r.started),
                    "finished": views._iso(r.finished),
                }
                for r in rs
            ],
        }


# ------------------------------------------------------------------------------------------ write
@tool
def set_build_qty(project_id: int, build_qty: int) -> dict:
    """Sets how many boards of a project are planned. Drives 'required' quantities and shortages."""
    if not 0 <= build_qty <= 1_000_000:
        raise ValueError("build_qty must be between 0 and 1,000,000.")
    with db.session() as s:
        p = s.get(Project, project_id)
        if not p:
            raise ValueError(f"No project {project_id}.")
        p.build_qty = build_qty
        return {"ok": True, "project": p.name, "variant": p.variant, "build_qty": build_qty}


@tool
def set_project_active(project_id: int, active: bool) -> dict:
    """Includes or excludes a project/variant from demand. Only include the variants you actually build."""
    with db.session() as s:
        p = s.get(Project, project_id)
        if not p:
            raise ValueError(f"No project {project_id}.")
        p.active = active
        return {"ok": True, "project": p.name, "variant": p.variant, "active": active}


@tool
def set_coo(mpn: str, coo: str, note: str = "") -> dict:
    """Records a part's country of origin by hand (overrides API values and survives refreshes).
    An empty coo removes the manual entry. note: the evidence, e.g. 'CoC from supplier, 2026-10'."""
    jobs.set_coo(mpn, coo[:100], note[:1000], WHO)
    return {"ok": True, "mpn": norm_mpn(mpn), "coo": coo.strip()}


@tool
def refresh_part(mpn: str) -> dict:
    """Fetches fresh supply data for one part in the background. Check the result with get_part shortly after."""
    _start(jobs.supply_refresh, WHO, True, [mpn])
    return {"ok": True, "started": True}


@tool
def run_bom_sync() -> dict:
    """Re-reads BOMs from the git repositories / project folders, then refreshes supply. Runs in the background;
    follow it with sync_status."""
    if not jobs.bom_sources():
        raise ValueError("No BOM source configured. Set GIT_REPOS or PROJECT_DIRS.")

    def sync_then_refresh(who):
        jobs.bom_sync(who)
        jobs.supply_refresh(who)

    _start(sync_then_refresh, WHO)
    return {"ok": True, "started": True}


@tool
def run_supply_refresh(force: bool = False) -> dict:
    """Refreshes supply data for all parts in the background. Without force, recently refreshed parts are skipped
    (distributor APIs have daily limits)."""
    _start(jobs.supply_refresh, WHO, force)
    return {"ok": True, "started": True}


# ------------------------------------------------------------------------------------------ HTTP endpoint
class Endpoint:
    """ASGI app for /mcp: checks the bearer token, then hands the request to the session manager.

    A new session manager per app lifespan, because one can only be run once (matters for tests and reloads).
    DNS-rebinding (Host header) checks are off: a rebinding page can't add the Authorization header, and the
    server is reached by IP or hostname from other machines.
    """

    manager: StreamableHTTPSessionManager | None = None

    async def __call__(self, scope, receive, send):
        token = config.settings.mcp_token
        if not token or self.manager is None:
            return await _reply(send, 404, b'{"detail":"MCP is off. Set MCP_TOKEN to enable it."}')
        auth = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        if not hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
            return await _reply(
                send,
                401,
                b'{"detail":"Missing or wrong Authorization: Bearer <MCP_TOKEN>."}',
                [(b"www-authenticate", b"Bearer")],
            )
        await self.manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(self):
        self.manager = StreamableHTTPSessionManager(app=mcp._mcp_server, json_response=True, stateless=True)
        try:
            async with self.manager.run():
                yield
        finally:
            self.manager = None


async def _reply(send, status: int, body: bytes, headers: list | None = None):
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), *(headers or [])],
        }
    )
    await send({"type": "http.response.body", "body": body})


endpoint = Endpoint()
