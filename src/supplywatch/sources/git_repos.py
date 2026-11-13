"""BOM sync from git repositories (and/or mounted folders) holding Altium projects.

Each repository is cloned or fast-forwarded into ``/data/repos``; every ``.PrjPcb`` whose file
name matches PROJECT_FILTER is parsed with :mod:`altium_files`. Each project variant becomes one
Project row; the base design (no variations) is active by default.

The same project can turn up more than once (a folder holding two copies, or a project moving from a
git remote to a local folder). Only the most recently saved copy is synced, and a newly seen copy takes
over the active flag and build quantity of the old one, which is then deactivated, so demand is never
counted twice.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy import select

from .. import config, db
from ..db import BomLine, Project, utcnow
from . import altium_files

GIT_TIMEOUT = 300


def _scrub(text: str) -> str:
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text or "")


def _auth_url(url: str) -> str:
    tok = config.settings.git_token
    if not tok or not url.startswith("https://"):
        return url
    p = urlsplit(url)
    user = config.settings.git_username or "x-access-token"
    return urlunsplit(
        (
            p.scheme,
            f"{quote(user, safe='')}:{quote(tok, safe='')}@{p.hostname}" + (f":{p.port}" if p.port else ""),
            p.path,
            p.query,
            p.fragment,
        )
    )


def _git(args: list[str], cwd: Path | None = None) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if config.settings.git_ssh_key:
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {config.settings.git_ssh_key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        )
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(_scrub(f"git {args[0]} failed: {r.stderr.strip() or r.stdout.strip()}"))
    return r.stdout.strip()


def parse_repo_spec(spec: str) -> tuple[str, str]:
    """'url' or 'url#branch'."""
    url, _, branch = spec.strip().partition("#")
    return url.strip(), branch.strip()


def checkout(spec: str, log: list) -> tuple[Path, str]:
    url, branch = parse_repo_spec(spec)
    root = Path(config.settings.repos_dir)
    root.mkdir(parents=True, exist_ok=True)
    # Last path segment only; local Windows remotes (file://C:\...) use backslashes.
    last = re.split(r"[/\\]", url.rstrip("/\\"))[-1]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", last.removesuffix(".git"))
    dest = root / f"{name}-{hashlib.sha1(spec.encode()).hexdigest()[:8]}"
    if not (dest / ".git").exists():
        args = ["clone", "--depth", "1"] + (["--branch", branch] if branch else []) + [_auth_url(url), str(dest)]
        _git(args)
        log.append(f"cloned {_scrub(url)}{'#' + branch if branch else ''}")
    else:
        _git(["remote", "set-url", "origin", _auth_url(url)], dest)
        _git(["fetch", "--depth", "1", "origin", branch or "HEAD"], dest)  # HEAD = remote default branch
        _git(["reset", "--hard", "FETCH_HEAD"], dest)
        _git(["clean", "-fdx"], dest)
    rev = _git(["log", "-1", "--format=%h %cs"], dest)
    return dest, rev


def find_projects(root: Path) -> list[Path]:
    rx = re.compile(config.settings.project_filter, re.I) if config.settings.project_filter else None
    out = []
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() != ".prjpcb" or not p.is_file() or ".git" in p.parts:
            continue
        if rx and not rx.search(p.name):
            continue
        out.append(p)
    return out


def saved_at(prj: Path) -> float:
    """Last save of a project: newest .PrjPcb / .SchDoc / .PcbDoc / .BomDoc in its folder."""
    files = [prj] + [
        p
        for p in prj.parent.rglob("*")
        if p.suffix.lower() in (".schdoc", ".pcbdoc", ".bomdoc") and ".git" not in p.parts
    ]
    return max((p.stat().st_mtime for p in files if p.is_file()), default=0.0)


def _upsert(
    s,
    ext: str,
    name: str,
    variant: str,
    active_default: bool,
    lines: list[dict],
    rev: str,
    where: str,
    log: list | None = None,
):
    row = s.scalar(select(Project).where(Project.external_id == ext))
    if not row:
        row = Project(source="git", external_id=ext, name=name, variant=variant, active=active_default)
        # the same project synced before from elsewhere: keep its settings, retire the old copy
        here = ext.rsplit("::", 1)[0] + "::"  # every variant of this project at this location
        known = [
            p
            for p in s.scalars(select(Project).where(Project.name == name)).all()
            if not p.external_id.startswith(here)
        ]
        olds = [p for p in known if p.variant == variant]
        if known and not olds:
            row.active = False  # a variant the old copies didn't have: keep the user's choice of which one counts
        if olds:
            prev = max(olds, key=lambda p: (p.active, p.synced_at or dt.datetime.min))
            row.active, row.build_qty = prev.active, prev.build_qty
            for o in olds:
                o.active = False
            if log is not None:
                log.append(
                    f"{name} [{variant}]: now synced from {where}; settings taken over from the "
                    f"{prev.source} copy, which is deactivated"
                )
        s.add(row)
    row.name, row.variant, row.revision, row.location = name, variant, rev, where
    row.lines.clear()
    s.flush()
    for ln in lines:
        row.lines.append(
            BomLine(
                component=ln["component"],
                description=ln["description"],
                designators=ln["designators"],
                quantity=ln["quantity"],
                part_choices=ln["part_choices"],
                footprint=ln.get("footprint", ""),
                comment=ln.get("comment", ""),
                mpn_source=ln.get("mpn_source", ""),
            )
        )
    row.synced_at = utcnow()


def sync(log: list) -> dict:
    cfg = config.settings
    sources: list[tuple[str, Path, str]] = []  # (label, root, revision)
    failed_roots: set[str] = set()
    for spec in cfg.git_repos:
        try:
            dest, rev = checkout(spec, log)
            sources.append((_scrub(parse_repo_spec(spec)[0]), dest, rev))
        except Exception as e:  # noqa: BLE001
            log.append(f"error: {_scrub(spec)}: {e}")
            failed_roots.add(_scrub(parse_repo_spec(spec)[0]))
    for d in cfg.project_dirs:
        p = Path(d)
        if p.is_dir():
            sources.append((str(p), p, ""))  # revision = last save, per project
        else:
            log.append(f"error: folder {d} not found in the container")
            failed_roots.add(str(p))
    if not sources and not failed_roots:
        raise RuntimeError("No GIT_REPOS or PROJECT_DIRS configured.")

    found: dict[str, list[tuple[float, str, Path, str, Path]]] = {}  # project name -> copies
    for label, root, rev in sources:
        prjs = find_projects(root)
        if not prjs:
            log.append(f"warning: {label}: no .PrjPcb matching PROJECT_FILTER '{cfg.project_filter}'")
        for prj in prjs:
            found.setdefault(prj.stem.lower(), []).append((saved_at(prj), label, root, rev, prj))
    todo = []
    for copies in found.values():
        copies.sort(key=lambda c: c[0], reverse=True)
        todo.append(copies[0])
        for _, label, root, _, prj in copies[1:]:
            log.append(
                f"skipped {label}/{prj.relative_to(root).as_posix()}: older copy of {prj.stem} "
                f"(using {copies[0][1]}/{copies[0][4].relative_to(copies[0][2]).as_posix()})"
            )

    seen, n_proj, n_lines = set(), 0, 0
    for mtime, label, root, rev, prj in sorted(todo, key=lambda c: (c[1], str(c[4]))):
        rev = rev or "saved " + dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        rel = prj.relative_to(root).as_posix()
        try:
            variants = altium_files.project_boms(prj, log)
        except Exception as e:  # noqa: BLE001
            log.append(f"error: {label}/{rel}: {type(e).__name__}: {e}")
            failed_roots.add(label)
            continue
        name = prj.stem
        with db.session() as s:
            for i, (vname, lines) in enumerate(variants):
                ext = f"git:{label}:{rel}::{vname}"
                seen.add(ext)
                _upsert(s, ext, name, vname, i == 0, lines, rev, f"{label}/{rel}", log)
                n_lines += len(lines)
        n_proj += 1
        no_mpn = sum(1 for ln in variants[0][1] if not ln["part_choices"])
        log.append(
            f"synced: {name} @ {rev} - {len(variants[0][1])} lines, {len(variants) - 1} variant(s)"
            + (f", {no_mpn} line(s) without MPN" if no_mpn else "")
        )

    with db.session() as s:
        n_off = 0
        for row in s.scalars(select(Project).where(Project.source == "git")).all():
            if row.external_id in seen or any(row.external_id.startswith(f"git:{f}:") for f in failed_roots):
                continue
            if row.active:
                row.active = False
                n_off += 1
        if n_off:
            log.append(f"excluded {n_off} project variant(s) no longer found")
    return {"projects": n_proj, "lines": n_lines, "failed": len(failed_roots)}
