"""HTTP API and web UI."""

from __future__ import annotations

import csv
import io
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import config, db, jobs, mcp_server, views
from .db import Project, Run
from .sources import nexar, upload

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("supplywatch")
STATIC = Path(__file__).parent / "static"

scheduler: BackgroundScheduler | None = None


@asynccontextmanager
async def lifespan(_app):
    _startup()
    async with mcp_server.endpoint.lifespan():
        yield
    _shutdown()


app = FastAPI(title="Supply Watch", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
# The MCP endpoint is a raw ASGI app; Starlette routes it as one, which its request-handler type hint does not cover.
app.add_route(
    "/mcp",
    mcp_server.endpoint,  # ty: ignore[invalid-argument-type]
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    # State-changing API calls must carry a custom header. Browsers won't send it cross-site
    # without a CORS preflight (which this app never grants), so other sites can't trigger actions.
    # /mcp is exempt: it needs a bearer token instead, which browsers never send on their own.
    if (
        request.method not in ("GET", "HEAD", "OPTIONS")
        and request.url.path != "/mcp"
        and request.headers.get("X-Requested-With") != "supplywatch"
    ):
        return JSONResponse({"detail": "Missing X-Requested-With: supplywatch header."}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        # Revalidate (cheap 304 via ETag) so browsers pick up a new app.js/styles.css right after a deploy.
        resp.headers.setdefault("Cache-Control", "no-cache")
    if not request.url.path.startswith("/api/docs"):
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'",
        )
    return resp


# ------------------------------------------------------------------------------------------ auth
class User(BaseModel):
    email: str
    admin: bool


def current_user(request: Request) -> User:
    cfg = config.settings
    if cfg.auth_mode == "none":
        return User(email="anonymous", admin=True)
    email = (request.headers.get(cfg.auth_user_header) or "").strip()
    if not email:
        raise HTTPException(401, f"Missing {cfg.auth_user_header} header. This app must run behind the SSO proxy.")
    groups = {g.strip() for g in (request.headers.get(cfg.auth_groups_header) or "").split(",") if g.strip()}
    admin = email.lower() in cfg.admin_users or (bool(cfg.admin_group) and cfg.admin_group in groups)
    return User(email=email, admin=admin)


def admin_user(u: User = Depends(current_user)) -> User:
    if not u.admin:
        raise HTTPException(403, "Only admins can do this. Ask an admin to add you to ADMIN_USERS or ADMIN_GROUP.")
    return u


# ------------------------------------------------------------------------------------------ lifecycle
def _startup():
    global scheduler
    db.init()
    cfg = config.settings
    if cfg.auth_mode == "none":
        log.warning("AUTH_MODE=none: no login, every visitor is an admin. Keep the port on the internal network.")
    if not cfg.secret_key:
        log.warning("SECRET_KEY is not set: OAuth tokens are stored unencrypted in the database.")
    if cfg.mcp_token:
        log.info("MCP server on at %s/mcp (bearer token required).", cfg.base_url)
    scheduler = BackgroundScheduler(timezone=cfg.timezone)
    for cron, fn, name in (
        (cfg.bom_sync_cron, _sched_bom, "bom_sync"),
        (cfg.supply_refresh_cron, _sched_supply, "supply"),
    ):
        if cron and cron.lower() != "off":
            scheduler.add_job(
                fn,
                CronTrigger.from_crontab(cron, timezone=cfg.timezone),
                id=name,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )
    scheduler.start()


def _shutdown():
    if scheduler:
        scheduler.shutdown(wait=False)


def _sched_bom():
    try:
        if jobs.bom_sources():
            jobs.bom_sync("schedule")
            jobs.supply_refresh("after BOM sync")  # picks up new MPNs; fresh parts are skipped
    except jobs.Busy:
        log.info("bom sync skipped: busy")


def _sched_supply():
    try:
        jobs.supply_refresh("schedule")
    except jobs.Busy:
        log.info("supply refresh skipped: busy")


def _bg(fn, *a, **kw):
    """Runs a job in a thread; raises Busy synchronously if one is already running."""
    if jobs._lock.locked():
        raise HTTPException(409, "A sync is already running. Wait for it to finish.")
    threading.Thread(target=lambda: _safe(fn, *a, **kw), daemon=True).start()


def _sync_then_refresh(who: str):
    jobs.bom_sync(who)
    jobs.supply_refresh(who)


def _safe(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except jobs.Busy:
        pass


# ------------------------------------------------------------------------------------------ pages
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(u: User = Depends(current_user)):
    return FileResponse(STATIC / "index.html")


# ------------------------------------------------------------------------------------------ API
@app.get("/api/me")
def me(u: User = Depends(current_user)):
    return u


@app.get("/api/data")
def data(u: User = Depends(current_user)):
    with db.session() as s:
        return views.build(s)


@app.get("/api/parts/{key}")
def part(key: str, u: User = Depends(current_user)):
    with db.session() as s:
        d = views.part_detail(s, key)
    if not d:
        raise HTTPException(404, "Part not found")
    return d


class CooIn(BaseModel):
    coo: str = Field("", max_length=100)
    note: str = Field("", max_length=1000)


@app.put("/api/parts/{key}/coo")
def put_coo(key: str, body: CooIn, u: User = Depends(current_user)):
    jobs.set_coo(key, body.coo, body.note, u.email)
    return {"ok": True}


@app.post("/api/parts/{key}/refresh")
def refresh_part(key: str, u: User = Depends(current_user)):
    _bg(jobs.supply_refresh, u.email, True, [key])
    return {"ok": True}


class ProjectIn(BaseModel):
    build_qty: int | None = Field(None, ge=0, le=1_000_000)
    active: bool | None = None


@app.patch("/api/projects/{pid}")
def patch_project(pid: int, body: ProjectIn, u: User = Depends(current_user)):
    with db.session() as s:
        p = s.get(Project, pid)
        if not p:
            raise HTTPException(404, "Project not found")
        if body.build_qty is not None:
            p.build_qty = body.build_qty
        if body.active is not None:
            if not u.admin:
                raise HTTPException(403, "Only admins can include or exclude projects.")
            p.active = body.active
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def delete_project(pid: int, u: User = Depends(admin_user)):
    with db.session() as s:
        p = s.get(Project, pid)
        if not p:
            raise HTTPException(404, "Project not found")
        if p.source != "upload":
            raise HTTPException(400, "Altium 365 projects come back on the next sync. Exclude them instead.")
        s.delete(p)
    return {"ok": True}


@app.post("/api/upload")
async def upload_bom(
    name: str = Form(...), variant: str = Form(""), file: UploadFile = File(...), u: User = Depends(admin_user)
):
    raw = await file.read()
    if len(raw) > 10_000_000:
        raise HTTPException(413, "File is larger than 10 MB.")
    try:
        r = upload.import_csv(name, raw, variant)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return r


@app.get("/api/status")
def status(u: User = Depends(current_user)):
    cfg = config.settings
    with db.session() as s:
        usage = db.usage_this_month(s)
        runs = s.scalars(select(Run).order_by(Run.id.desc()).limit(20)).all()
        denied = db.kv_get(s, "nexar_rejected_fields", []) or []
        out = {
            "user": u.model_dump(),
            "running": jobs._lock.locked(),
            "bom_sources": {
                "git": {
                    "configured": bool(cfg.git_repos or cfg.project_dirs),
                    "repos": len(cfg.git_repos),
                    "folders": len(cfg.project_dirs),
                    "filter": cfg.project_filter,
                },
                "altium365": {
                    "configured": bool(cfg.nexar_client_id),
                    "connected": nexar.design_connected(),
                    "workspace": cfg.a365_workspace_url,
                    "redirect_uri": cfg.nexar_redirect_uri,
                },
            },
            "supply_sources": [
                {"key": src.key, "label": src.label, "configured": bool(src.enabled())} for src in jobs.SOURCES
            ],
            "nexar_supply": {
                "enabled": cfg.nexar_supply_enabled,
                "lead_time": cfg.nexar_lead_time,
                "specs": cfg.nexar_specs,
                "refused_fields": denied,
            },
            "nexar_usage": {"month": usage.month, "parts": usage.parts, "budget": cfg.nexar_monthly_part_budget},
            "schedule": {
                "bom_sync": cfg.bom_sync_cron,
                "supply_refresh": cfg.supply_refresh_cron,
                "next": {
                    j.id: j.next_run_time.isoformat()
                    for j in (scheduler.get_jobs() if scheduler else [])
                    if j.next_run_time
                },
            },
            "runs": [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "started": views._iso(r.started),
                    "finished": views._iso(r.finished),
                    "status": r.status,
                    "summary": r.summary,
                    "by": r.triggered_by,
                }
                for r in runs
            ],
        }
    return out


@app.get("/api/runs/{rid}")
def run_detail(rid: int, u: User = Depends(current_user)):
    with db.session() as s:
        r = s.get(Run, rid)
        if not r:
            raise HTTPException(404, "Run not found")
        return {
            "id": r.id,
            "kind": r.kind,
            "status": r.status,
            "summary": r.summary,
            "log": r.log,
            "started": views._iso(r.started),
            "finished": views._iso(r.finished),
            "by": r.triggered_by,
        }


@app.post("/api/runs/bom-sync")
def run_bom(u: User = Depends(admin_user)):
    if not jobs.bom_sources():
        raise HTTPException(400, "No BOM source configured. Set GIT_REPOS or PROJECT_DIRS.")
    _bg(_sync_then_refresh, u.email)
    return {"ok": True}


@app.post("/api/runs/supply-refresh")
def run_supply(force: bool = False, u: User = Depends(admin_user)):
    _bg(jobs.supply_refresh, u.email, force)
    return {"ok": True}


# ------------------------------------------------------------------------------------------ Altium 365 sign-in
@app.get("/admin/nexar/login")
def nexar_login(u: User = Depends(admin_user)):
    try:
        return RedirectResponse(nexar.design_login_url())
    except nexar.NexarError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/admin/nexar/callback")
def nexar_callback(code: str = "", state: str = "", error: str = "", u: User = Depends(admin_user)):
    if error:
        return RedirectResponse(f"/#admin?error={error}")
    try:
        nexar.design_callback(code, state)
    except nexar.NexarError as e:
        return HTMLResponse(f"<p>Altium 365 sign-in failed: {e}</p><p><a href='/#admin'>Back</a></p>", status_code=400)
    _bg(jobs.bom_sync, u.email)
    return RedirectResponse("/#admin")


@app.post("/admin/nexar/disconnect")
def nexar_disconnect(u: User = Depends(admin_user)):
    nexar.design_disconnect()
    return {"ok": True}


# ------------------------------------------------------------------------------------------ exports
def _csv(rows: list[list], filename: str) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    for r in rows:
        w.writerow(r)
    data = "﻿" + buf.getvalue()  # BOM so Excel opens UTF-8 correctly
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export/parts.csv")
def export_parts(u: User = Depends(current_user)):
    with db.session() as s:
        d = views.build(s)
    names = {str(p["id"]): f"{p['name']} [{p['variant']}]" for p in d["projects"]}
    rows = [
        [
            "MPN",
            "Manufacturer",
            "Description",
            "Projects",
            "Per board (all projects)",
            "Required",
            "Authorised stock",
            "Suppliers with stock",
            "Lead time (days)",
            "Lead source",
            "Lifecycle",
            "Country of origin",
            "COO source",
            "HTS",
            "ECCN",
            "RoHS",
            "REACH",
            "Best unit price",
            "Currency",
            "Best seller",
            "Issues",
            "Alternate for",
            "Refreshed",
        ]
    ]
    for p in d["parts"]:
        bp = p["best_price"] or {}
        rows.append(
            [
                p["mpn"],
                p["manufacturer"],
                p["description"],
                ", ".join(names.get(k, k) for k in p["used_in"]),
                p["per_board"],
                p["required"],
                p["stock"],
                p["suppliers"],
                p["lead_days"],
                p["lead_source"],
                p["lifecycle"],
                p["coo"],
                p["coo_source"],
                p["hts"],
                p["eccn"],
                p["rohs"],
                p["reach"],
                bp.get("price"),
                bp.get("currency"),
                bp.get("seller"),
                ", ".join(d["flags"][f]["label"] for f in p["flags"]),
                ", ".join(p["alternate_for"]),
                p["refreshed_at"],
            ]
        )
    return _csv(rows, "supply-watch-parts.csv")


@app.get("/api/export/projects/{pid}.csv")
def export_project(pid: int, u: User = Depends(current_user)):
    with db.session() as s:
        d = views.build(s)
    p = next((x for x in d["projects"] if x["id"] == pid), None)
    if not p:
        raise HTTPException(404, "Project not found")
    parts = {x["key"]: x for x in d["parts"]}
    rows = [
        [
            "Component",
            "Description",
            "Designators",
            "Qty/board",
            f"Need for {p['build_qty']}",
            "MPN",
            "Manufacturer",
            "Alternates",
            "Authorised stock",
            "Lead time (days)",
            "Lifecycle",
            "COO",
            "Issues",
        ]
    ]
    for ln in p["lines"]:
        pp = parts.get(ln["primary"], {})
        ch = ln["choices"]
        rows.append(
            [
                ln["component"],
                ln["description"],
                " ".join(ln["designators"]),
                ln["quantity"],
                ln["need"],
                ch[0]["mpn"] if ch else "",
                ch[0].get("mfr", "") if ch else "",
                ", ".join(c["mpn"] for c in ch[1:]),
                pp.get("stock"),
                pp.get("lead_days"),
                pp.get("lifecycle"),
                pp.get("coo"),
                ", ".join(d["flags"][f]["label"] for f in ln["flags"]),
            ]
        )
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in p["name"])
    return _csv(rows, f"{safe}.csv")


@app.exception_handler(jobs.Busy)
def _busy(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=409)
