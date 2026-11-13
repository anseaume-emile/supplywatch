"""Nexar API plumbing: OAuth tokens and GraphQL calls.

Two token types:
* supply  - client_credentials, scope ``supply.domain`` (Octopart data). Fully headless.
* design  - authorization code + PKCE, scope ``design.domain user.access offline_access``
            (Altium 365 data). An admin signs in once from the Admin page; the refresh
            token is stored (encrypted when SECRET_KEY is set) and rotated on every refresh.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode

import httpx

from .. import config, db
from ..crypto import decrypt, encrypt

AUTHORIZE_URL = "https://identity.nexar.com/connect/authorize"
TOKEN_URL = "https://identity.nexar.com/connect/token"
GRAPHQL_URL = "https://api.nexar.com/graphql"
DESIGN_SCOPES = ["openid", "profile", "email", "user.access", "design.domain", "offline_access"]
TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class NexarError(RuntimeError):
    pass


class NexarNotConnected(NexarError):
    pass


# --------------------------------------------------------------------------- tokens
def _store_token(key: str, tok: dict):
    tok = dict(tok)
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 120
    blob = encrypt(json.dumps(tok))
    with db.session() as s:
        db.kv_set(s, key, {"blob": blob})


def _load_token(key: str) -> dict | None:
    with db.session() as s:
        v = db.kv_get(s, key)
    if not v or "blob" not in v:
        return None
    try:
        return json.loads(decrypt(v["blob"]))
    except Exception:  # key changed or corrupt
        return None


def supply_token(client: httpx.Client | None = None) -> str:
    cfg = config.settings
    if not (cfg.supply_id and cfg.supply_secret):
        raise NexarNotConnected("NEXAR_CLIENT_ID / NEXAR_CLIENT_SECRET are not set.")
    tok = _load_token("nexar_supply_token")
    if tok and tok.get("expires_at", 0) > time.time():
        return tok["access_token"]
    c = client or httpx.Client(timeout=TIMEOUT)
    r = c.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": cfg.supply_id,
            "client_secret": cfg.supply_secret,
            "scope": "supply.domain",
        },
    )
    if r.status_code != 200:
        raise NexarError(f"Nexar supply token request failed ({r.status_code}): {r.text[:300]}")
    tok = r.json()
    _store_token("nexar_supply_token", tok)
    return tok["access_token"]


def design_login_url() -> str:
    cfg = config.settings
    if not (cfg.nexar_client_id and cfg.nexar_client_secret):
        raise NexarNotConnected("NEXAR_CLIENT_ID / NEXAR_CLIENT_SECRET are not set.")
    verifier = re.sub("[^a-zA-Z0-9]+", "", base64.urlsafe_b64encode(os.urandom(40)).decode())
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    with db.session() as s:
        db.kv_set(s, "nexar_pkce", {"state": state, "verifier": encrypt(verifier), "at": time.time()})
    q = {
        "response_type": "code",
        "client_id": cfg.nexar_client_id,
        "redirect_uri": cfg.nexar_redirect_uri,
        "scope": " ".join(DESIGN_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(q)}"


def design_callback(code: str, state: str, client: httpx.Client | None = None) -> dict:
    cfg = config.settings
    with db.session() as s:
        p = db.kv_get(s, "nexar_pkce") or {}
        db.kv_set(s, "nexar_pkce", {})
    if not p or p.get("state") != state or time.time() - p.get("at", 0) > 900:
        raise NexarError("The Altium 365 sign-in expired or did not start here. Start it again from Admin.")
    c = client or httpx.Client(timeout=TIMEOUT)
    r = c.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": cfg.nexar_client_id,
            "client_secret": cfg.nexar_client_secret,
            "redirect_uri": cfg.nexar_redirect_uri,
            "code": code,
            "code_verifier": decrypt(p["verifier"]),
        },
    )
    if r.status_code != 200:
        raise NexarError(f"Nexar token exchange failed ({r.status_code}): {r.text[:300]}")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise NexarError("Nexar returned no refresh token. Enable offline access for the app in the Nexar portal.")
    _store_token("nexar_design_token", tok)
    return tok


def design_disconnect():
    with db.session() as s:
        db.kv_set(s, "nexar_design_token", {})


def design_connected() -> bool:
    t = _load_token("nexar_design_token")
    return bool(t and t.get("refresh_token"))


def design_token(client: httpx.Client | None = None) -> str:
    cfg = config.settings
    tok = _load_token("nexar_design_token")
    if not tok or not tok.get("refresh_token"):
        raise NexarNotConnected("Altium 365 is not connected. An admin must sign in from the Admin page.")
    if tok.get("expires_at", 0) > time.time():
        return tok["access_token"]
    c = client or httpx.Client(timeout=TIMEOUT)
    r = c.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": cfg.nexar_client_id,
            "client_secret": cfg.nexar_client_secret,
        },
    )
    if r.status_code != 200:
        raise NexarNotConnected(f"Altium 365 sign-in has expired ({r.status_code}). An admin must sign in again.")
    new = r.json()
    new.setdefault("refresh_token", tok["refresh_token"])  # keep old one if not rotated
    _store_token("nexar_design_token", new)
    return new["access_token"]


# --------------------------------------------------------------------------- graphql
def graphql(
    query: str, variables: dict | None, token: str, url: str = GRAPHQL_URL, client: httpx.Client | None = None
) -> tuple[dict, list]:
    """Returns (data, errors). Raises only on transport / HTTP failure."""
    c = client or httpx.Client(timeout=TIMEOUT)
    headers = {"Authorization": f"Bearer {token}", "token": token, "Content-Type": "application/json"}
    for attempt in range(4):
        r = c.post(url, json={"query": query, "variables": variables or {}}, headers=headers)
        if r.status_code in (429, 502, 503, 504) and attempt < 3:
            time.sleep(2**attempt * 2)
            continue
        break
    if r.status_code != 200:
        raise NexarError(f"Nexar GraphQL HTTP {r.status_code}: {r.text[:300]}")
    body = r.json()
    return body.get("data") or {}, body.get("errors") or []


def error_text(errors: list) -> str:
    return "; ".join(e.get("message", str(e)) for e in errors)[:500]


# --------------------------------------------------------------------------- schema-drift helpers
_BAD_FIELD = re.compile(r'Cannot query field "(\w+)"')
_UNAUTH = re.compile(r"Unauthorized to access '(\w+)'")


def _camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(w.capitalize() for w in rest)


def rejected_fields(errors: list) -> set[str]:
    """Fields the API refused: unknown in the schema, or not included in the Nexar plan."""
    out = set()
    for e in errors:
        msg = e.get("message", "")
        for m in _BAD_FIELD.finditer(msg):
            out.add(m.group(1))
        for m in _UNAUTH.finditer(msg):
            out.add(_camel(m.group(1)))
    return out


def strip_field(query: str, name: str) -> str:
    """Removes every selection of `name` (with its arguments and nested selection set)."""
    out, i = [], 0
    rx = re.compile(rf"(?<![\w$]){re.escape(name)}(?!\w)")
    while True:
        m = rx.search(query, i)
        if not m:
            out.append(query[i:])
            return "".join(out)
        # skip if this is an argument name (followed by ':')
        j = m.end()
        k = j
        while k < len(query) and query[k] in " \t\r\n":
            k += 1
        if k < len(query) and query[k] == ":":
            out.append(query[i:j])
            i = j
            continue
        out.append(query[i : m.start()])
        if k < len(query) and query[k] == "(":
            depth = 0
            while k < len(query):
                depth += query[k] == "("
                depth -= query[k] == ")"
                k += 1
                if depth == 0:
                    break
            while k < len(query) and query[k] in " \t\r\n":
                k += 1
        if k < len(query) and query[k] == "{":
            depth = 0
            while k < len(query):
                depth += query[k] == "{"
                depth -= query[k] == "}"
                k += 1
                if depth == 0:
                    break
            j = k
        i = j if j > m.end() else (k if k > m.end() and query[m.end() : k].strip().startswith("(") else m.end())
