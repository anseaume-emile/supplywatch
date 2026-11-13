"""MCP endpoint: token check, tool listing, read and write tools against an uploaded BOM."""

import json

import pytest

TOKEN = "t0ken"
AUTH = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}
BOM = (
    "Comment;Designator;Quantity;1st source - Manufacturer;1st source - MPN;Library Reference\n"
    "10k;R10 R11;2;Stackpole;RMCF0402FT10K0;CMP-009-00014-1\n"
)


@pytest.fixture()
def c(env, monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", TOKEN)
    from fastapi.testclient import TestClient

    from supplywatch import config, main

    config.reload()
    with TestClient(main.app) as c:
        yield c


def rpc(c, method, params=None, headers=AUTH):
    return c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def call(c, name, **args):
    r = rpc(c, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    return res, None if res.get("isError") else json.loads(res["content"][0]["text"])


def test_token_required(c, monkeypatch):
    assert rpc(c, "tools/list", headers={}).status_code == 401
    assert rpc(c, "tools/list", headers={**AUTH, "Authorization": "Bearer nope"}).status_code == 401
    from supplywatch import config

    monkeypatch.setenv("MCP_TOKEN", "")
    config.reload()
    assert rpc(c, "tools/list").status_code == 404


def test_tools(c):
    names = {t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]}
    assert {
        "overview",
        "list_parts",
        "get_part",
        "get_project_bom",
        "set_build_qty",
        "set_coo",
        "run_supply_refresh",
    } <= names

    from supplywatch.sources import upload

    upload.import_csv("PRJ-TEST", BOM.encode(), "")
    (pid,) = [p["id"] for p in call(c, "list_projects")[1]["projects"] if p["name"] == "PRJ-TEST"]

    _, r = call(c, "set_build_qty", project_id=pid, build_qty=50)
    assert r["build_qty"] == 50
    _, parts = call(c, "list_parts", project_id=pid)
    (row,) = parts["parts"]
    assert row["mpn"] == "RMCF0402FT10K0" and row["required"] == 100 and "single_source" in row["flags"]
    assert call(c, "list_parts", query="nothing-like-this")[1]["total"] == 0

    assert call(c, "set_coo", mpn="RMCF0402FT10K0", coo="Taiwan", note="CoC")[1]["ok"]
    assert call(c, "get_part", mpn="rmcf0402ft10k0")[1]["coo_override"]["coo"] == "Taiwan"
    assert call(c, "get_project_bom", project_id=pid)[1]["lines"][0]["designators"] == ["R10", "R11"]

    res, _ = call(c, "get_part", mpn="NOPE-123")
    assert res["isError"]

    _, r = call(c, "set_project_active", project_id=pid, active=False)
    assert call(c, "list_parts", project_id=pid)[1]["total"] == 0
    assert "running" in call(c, "sync_status")[1]


def test_web_csrf_still_enforced(c):
    assert c.post("/api/runs/supply-refresh", headers={"X-Forwarded-Email": "admin@cdds.ai"}).status_code == 403
