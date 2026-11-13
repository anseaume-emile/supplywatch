"""Optional Nexar path: Altium 365 BOMs + Octopart data, with DigiKey and Mouser (all mocked)."""

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import respx

A365_BOM = {
    "desProjectById": {
        "id": "P1",
        "name": "PRJ-00002_PowerBoard",
        "description": "Power board",
        "design": {
            "workInProgress": {
                "variants": [
                    {
                        "name": "[No Variations]",
                        "bom": {
                            "bomItems": [
                                {
                                    "quantity": 2,
                                    "bomItemInstances": [{"designator": "C24"}, {"designator": "C1"}],
                                    "component": {
                                        "name": "CMP-001-00001-5",
                                        "description": "47 uF 100V electrolytic",
                                        "manufacturerParts": [
                                            {"companyName": "Würth Elektronik", "partNumber": "865060857005"},
                                            {"companyName": "Nichicon", "partNumber": "UUX2A470MNL1GS"},
                                        ],
                                    },
                                },
                                {
                                    "quantity": 1,
                                    "bomItemInstances": [{"designator": "U1"}],
                                    "component": {
                                        "name": "CMP-00000-2",
                                        "description": "LM5148 buck controller",
                                        "manufacturerParts": [
                                            {"companyName": "Texas Instruments", "partNumber": "LM5148RGYR"}
                                        ],
                                    },
                                },
                                {
                                    "quantity": 1,
                                    "bomItemInstances": [{"designator": "X1"}],
                                    "component": {
                                        "name": "CMP-002-00002-2",
                                        "description": "XT30 connector",
                                        "manufacturerParts": [],
                                    },
                                },
                            ]
                        },
                    },
                    {"name": "without sensing", "bom": {"bomItems": []}},
                ]
            }
        },
    }
}


def nexar_part(mpn, mfr, stock, lead=None, sellers=None):
    return {
        "mpn": mpn,
        "manufacturer": {"name": mfr},
        "shortDescription": f"{mpn} desc",
        "octopartUrl": f"https://octopart.com/{mpn}",
        "totalAvail": stock,
        "sellers": sellers
        or [
            {
                "country": "US",
                "isAuthorized": True,
                "company": {"name": "Digi-Key"},
                "offers": [
                    {
                        "sku": "DK-" + mpn,
                        "inventoryLevel": stock,
                        "moq": 1,
                        "packaging": "Cut Tape",
                        "updated": "2026-09-28T10:00:00Z",
                        "clickUrl": "https://dk",
                        "factoryLeadDays": lead,
                        "prices": [
                            {
                                "quantity": 1,
                                "price": 1.0,
                                "currency": "USD",
                                "convertedPrice": 0.9,
                                "convertedCurrency": "CHF",
                            }
                        ],
                    }
                ],
            },
            {
                "country": "DE",
                "isAuthorized": True,
                "company": {"name": "Farnell"},
                "offers": [
                    {
                        "sku": "F-" + mpn,
                        "inventoryLevel": -2,
                        "moq": 10,
                        "packaging": "Reel",
                        "updated": "2026-09-28T10:00:00Z",
                        "clickUrl": "https://farnell",
                        "factoryLeadDays": lead,
                        "prices": [
                            {
                                "quantity": 10,
                                "price": 0.8,
                                "currency": "EUR",
                                "convertedPrice": 0.75,
                                "convertedCurrency": "CHF",
                            }
                        ],
                    }
                ],
            },
        ],
    }


NEXAR_DB = {
    "865060857005": nexar_part("865060857005", "Würth Elektronik", 4500),
    "UUX2A470MNL1GS": nexar_part("UUX2A470MNL1GS", "Nichicon", 12),
    "LM5148RGYR": nexar_part("LM5148RGYR", "Texas Instruments", 8, lead=126),
}


def install_mocks(router, calls, refuse_lead=False):
    def token(request):
        body = parse_qs(request.content.decode())
        calls.append(("token", body["grant_type"][0]))
        if body["grant_type"][0] == "client_credentials":
            return httpx.Response(200, json={"access_token": "supply-tok", "expires_in": 86400})
        if body["grant_type"][0] == "authorization_code":
            assert body["code_verifier"][0]
            return httpx.Response(200, json={"access_token": "des-1", "refresh_token": "r1", "expires_in": 1})
        if body["grant_type"][0] == "refresh_token":
            return httpx.Response(200, json={"access_token": "des-2", "refresh_token": "r2", "expires_in": 86400})
        return httpx.Response(400)

    def gql(request):
        q = json.loads(request.content)
        query, v = q["query"], q["variables"]
        if "desWorkspaces" in query:
            return httpx.Response(
                200,
                json={"data": {"desWorkspaces": [{"id": "W1", "name": "CDDS", "url": "https://cdds.365.altium.com"}]}},
            )
        if "desWorkspaceByUrl" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "desWorkspaceByUrl": {
                            "location": {"name": "EU", "apiServiceUrl": "https://api.nexar.com/graphql"}
                        }
                    }
                },
            )
        if "desWorkspaceById" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "desWorkspaceById": {
                            "projects": [{"id": "P1", "name": "PRJ-00002_PowerBoard", "description": ""}]
                        }
                    }
                },
            )
        if "desProjectById" in query:
            if "companyName" in query:
                return (
                    httpx.Response(
                        200,
                        json={
                            "errors": [{"message": 'Cannot query field "companyName" on type "DesManufacturerPart".'}]
                        },
                    )
                    if calls.count("no-company")
                    else httpx.Response(200, json={"data": A365_BOM})
                )
            return httpx.Response(200, json={"data": A365_BOM})
        if "supMultiMatch" in query:
            calls.append("supMultiMatch")
            if refuse_lead and "factoryLeadDays" in query:
                return httpx.Response(
                    200,
                    json={
                        "errors": [
                            {
                                "message": "Unauthorized to access 'factory_lead_days'. Please upgrade your Nexar subscription to include 'LEAD_TIME'"
                            }
                        ]
                    },
                )
            res = []
            for item in v["queries"]:
                p = NEXAR_DB.get(item["mpn"])
                res.append({"hits": 1 if p else 0, "reference": item.get("reference"), "parts": [p] if p else []})
            return httpx.Response(200, json={"data": {"supMultiMatch": res}})
        return httpx.Response(400, json={"errors": [{"message": "unexpected"}]})

    router.post("https://identity.nexar.com/connect/token").mock(side_effect=token)
    router.post("https://api.nexar.com/graphql").mock(side_effect=gql)
    router.post("https://api.digikey.com/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "dk", "expires_in": 600})
    )

    def dk_details(request):
        mpn = request.url.path.split("/")[-2]
        if mpn == "LM5148RGYR":
            return httpx.Response(
                200,
                json={
                    "Product": {
                        "ManufacturerProductNumber": "LM5148RGYR",
                        "Manufacturer": {"Name": "Texas Instruments"},
                        "Description": {"ProductDescription": "IC REG CTRLR BUCK"},
                        "QuantityAvailable": 20,
                        "ManufacturerLeadWeeks": "18",
                        "ProductStatus": {"Status": "Active"},
                        "ProductUrl": "https://digikey/lm",
                        "Classifications": {
                            "RohsStatus": "ROHS3 Compliant",
                            "ExportControlClassNumber": "EAR99",
                            "HtsusCode": "8542.39.0001",
                        },
                        "ProductVariations": [
                            {
                                "DigiKeyProductNumber": "296-LM5148RGYRCT-ND",
                                "PackageType": {"Name": "Cut Tape"},
                                "QuantityAvailableforPackageType": 20,
                                "MinimumOrderQuantity": 1,
                                "StandardPricing": [
                                    {"BreakQuantity": 1, "UnitPrice": 4.12},
                                    {"BreakQuantity": 10, "UnitPrice": 3.5},
                                ],
                            }
                        ],
                    }
                },
            )
        return httpx.Response(404, json={})

    router.get(url__regex=r"https://api\.digikey\.com/products/v4/search/.*/productdetails").mock(
        side_effect=dk_details
    )
    router.post("https://api.digikey.com/products/v4/search/keyword").mock(
        return_value=httpx.Response(200, json={"Products": []})
    )

    def mouser(request):
        body = json.loads(request.content)
        mpns = body["SearchByPartRequest"]["mouserPartNumber"].split("|")
        parts = []
        if "865060857005" in mpns:
            parts.append(
                {
                    "ManufacturerPartNumber": "865060857005",
                    "Manufacturer": "Wurth Elektronik",
                    "Description": "Aluminum Electrolytic",
                    "Availability": "3000 In Stock",
                    "AvailabilityInStock": "3000",
                    "AvailabilityOnOrder": [{"Quantity": 5000, "Date": "2026-11-01"}],
                    "LeadTime": "8 Weeks",
                    "LifecycleStatus": None,
                    "Min": "1",
                    "MouserPartNumber": "710-865060857005",
                    "PriceBreaks": [{"Quantity": 1, "Price": "0,48 CHF", "Currency": "CHF"}],
                    "ProductCompliance": [
                        {"ComplianceName": "USHTS", "ComplianceValue": "8532220085"},
                        {"ComplianceName": "ECCN", "ComplianceValue": "EAR99"},
                    ],
                    "ProductDetailUrl": "https://mouser/865060857005",
                }
            )
        return httpx.Response(200, json={"Errors": [], "SearchResults": {"NumberOfResult": len(parts), "Parts": parts}})

    router.post(url__startswith="https://api.mouser.com/api/v1/search/partnumber").mock(side_effect=mouser)


def client(env):
    from fastapi.testclient import TestClient

    from supplywatch import main

    c = TestClient(main.app)
    c.__enter__()
    return c


ADMIN = {"X-Forwarded-Email": "admin@cdds.ai", "X-Requested-With": "supplywatch"}
USER = {"X-Forwarded-Email": "buyer@cdds.ai", "X-Requested-With": "supplywatch"}


def wait_idle():
    from supplywatch import jobs

    for _ in range(100):
        if not jobs._lock.locked():
            return
        time.sleep(0.05)


def test_auth_and_csrf(env):
    c = client(env)
    assert c.get("/api/data").status_code == 401
    assert c.get("/api/data", headers={"X-Forwarded-Email": "buyer@cdds.ai"}).status_code == 200
    r = c.post("/api/runs/supply-refresh", headers={"X-Forwarded-Email": "admin@cdds.ai"})
    assert r.status_code == 403 and "X-Requested-With" in r.text
    assert c.post("/api/runs/supply-refresh", headers=USER).status_code == 403  # not admin
    assert c.get("/api/me", headers=ADMIN).json()["admin"] is True
    assert "Content-Security-Policy" in c.get("/healthz").headers


def test_full_flow(env):
    calls = []
    c = client(env)
    with respx.mock(assert_all_mocked=True) as router:
        install_mocks(router, calls)
        # --- Altium 365 sign-in (PKCE) ---
        r = c.get("/admin/nexar/login", headers=ADMIN, follow_redirects=False)
        assert r.status_code == 307
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert "offline_access" in q["scope"][0] and q["code_challenge_method"] == ["S256"]
        assert q["redirect_uri"] == ["https://parts.example.com/admin/nexar/callback"]
        r = c.get(f"/admin/nexar/callback?code=abc&state={q['state'][0]}", headers=ADMIN, follow_redirects=False)
        assert r.status_code == 307
        wait_idle()
        st = c.get("/api/status", headers=USER).json()
        assert st["bom_sources"]["altium365"]["connected"]
        bom_run = [x for x in st["runs"] if x["kind"] == "bom_sync"][0]
        assert bom_run["status"] == "ok", bom_run
        assert ("token", "refresh_token") in calls  # short-lived access token got refreshed

        d = c.get("/api/data", headers=USER).json()
        base = [p for p in d["projects"] if p["variant"] == "[No Variations]"][0]
        other = [p for p in d["projects"] if p["variant"] == "without sensing"][0]
        assert base["active"] and not other["active"]
        assert base["line_count"] == 3
        c1 = [ln for ln in base["lines"] if ln["component"] == "CMP-001-00001-5"][0]
        assert c1["designators"] == ["C1", "C24"] and c1["choices"][0]["mfr"] == "Würth Elektronik"
        assert "no_mpn" in [ln for ln in base["lines"] if ln["component"] == "CMP-002-00002-2"][0]["flags"]

        # --- build qty 100 -> LM5148 needs 100 ---
        assert c.patch(f"/api/projects/{base['id']}", json={"build_qty": 100}, headers=USER).status_code == 200
        assert c.patch(f"/api/projects/{base['id']}", json={"active": False}, headers=USER).status_code == 403

        # --- supply refresh ---
        assert c.post("/api/runs/supply-refresh", headers=ADMIN).status_code == 200
        wait_idle()
        st = c.get("/api/status", headers=USER).json()
        run = [x for x in st["runs"] if x["kind"] == "supply_refresh"][0]
        assert run["status"] == "ok", c.get(f"/api/runs/{run['id']}", headers=USER).json()
        assert st["nexar_usage"]["parts"] == 3

        d = c.get("/api/data", headers=USER).json()
        parts = {p["mpn"]: p for p in d["parts"]}
        lm = parts["LM5148RGYR"]
        assert lm["required"] == 100
        # Digi-Key listing inside Octopart replaced by direct DigiKey data (20), Farnell has unknown qty
        assert lm["stock"] == 20
        assert "shortage" in lm["flags"]
        assert lm["lead_days"] == 18 * 7 and lm["lead_source"] == "digikey"  # Standard plan: no Nexar lead
        assert lm["lifecycle"] == "Active" and lm["hts"] == "8542.39.0001" and lm["eccn"] == "EAR99"
        assert "no_coo" in lm["flags"]
        wurth = parts["865060857005"]
        assert wurth["stock"] == 4500 + 3000  # Octopart Digi-Key listing kept (no direct DigiKey hit) + Mouser direct
        det = c.get("/api/parts/865060857005", headers=USER).json()
        sellers = sorted({o["seller"] for o in det["offers"]})
        assert "Mouser" in sellers and "Farnell" in sellers
        mo = [o for o in det["offers"] if o["seller"] == "Mouser"][0]
        assert mo["stock"] == 3000 and mo["on_order"] == 5000 and mo["lead_days"] == 56
        far = [o for o in det["offers"] if o["seller"] == "Farnell"][0]
        assert far["stock"] is None and far["stock_note"] == "in stock, qty unknown"
        assert far["prices"][0]["currency"] == "CHF"
        nich = parts["UUX2A470MNL1GS"]
        assert nich["is_alternate_only"] and nich["alternate_for"] == ["865060857005"]
        assert wurth["alternates"] == [nich["key"]]
        alts = det["alternates"]
        assert [(a["mpn"], a["relation"], a["need"]) for a in alts] == [
            ("UUX2A470MNL1GS", "alternate", wurth["required"])
        ]
        back = c.get("/api/parts/UUX2A470MNL1GS", headers=USER).json()["alternates"]
        assert [(a["mpn"], a["relation"]) for a in back] == [("865060857005", "primary")]

        # --- COO override survives refresh ---
        assert (
            c.put("/api/parts/LM5148RGYR/coo", json={"coo": "Malaysia", "note": "TI CoC"}, headers=USER).status_code
            == 200
        )
        n_before = len([x for x in calls if x == "supMultiMatch"])
        c.post("/api/runs/supply-refresh?force=true", headers=ADMIN)
        wait_idle()
        assert len([x for x in calls if x == "supMultiMatch"]) == n_before + 1
        d = c.get("/api/data", headers=USER).json()
        lm = {p["mpn"]: p for p in d["parts"]}["LM5148RGYR"]
        assert lm["coo"] == "Malaysia" and lm["coo_source"] == "manual" and "no_coo" not in lm["flags"]

        # --- due-only refresh skips fresh parts (quota protection) ---
        c.post("/api/runs/supply-refresh", headers=ADMIN)
        wait_idle()
        st = c.get("/api/status", headers=USER).json()
        assert st["runs"][0]["summary"] == "Nothing due for refresh"

        # --- exports ---
        csv = c.get("/api/export/parts.csv", headers=USER)
        assert csv.status_code == 200 and "LM5148RGYR" in csv.text and "Malaysia" in csv.text
        pcsv = c.get(f"/api/export/projects/{base['id']}.csv", headers=USER)
        assert "Need for 100" in pcsv.text


def test_plan_refusal_and_budget(env, monkeypatch):
    monkeypatch.setenv("NEXAR_LEAD_TIME", "true")
    monkeypatch.setenv("NEXAR_MONTHLY_PART_BUDGET", "2")
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "")
    monkeypatch.setenv("MOUSER_API_KEY", "")
    from supplywatch import config, jobs

    config.reload()
    from supplywatch.sources import upload

    upload.import_csv(
        "Test board",
        (
            "Designator,Quantity,Manufacturer 1,Manufacturer Part Number 1\n"
            '"C1, C24",2,Würth,865060857005\n"U1",1,TI,LM5148RGYR\n"U2",1,Nichicon,UUX2A470MNL1GS\n'
        ).encode("cp1252"),
    )
    calls = []
    with respx.mock(assert_all_called=False) as router:
        install_mocks(router, calls, refuse_lead=True)
        jobs.supply_refresh("test")
    from supplywatch import db
    from supplywatch.db import Run

    with db.session() as s:
        run = s.query(Run).order_by(Run.id.desc()).first()
        assert run.status == "ok", run.log
        assert any("refused" in ln for ln in run.log)
        assert any("budget" in ln for ln in run.log)
        assert db.kv_get(s, "nexar_rejected_fields") == ["factoryLeadDays"]
        assert db.usage_this_month(s).parts == 2


def test_upload_formats(env):
    from supplywatch.sources.upload import parse_csv

    cdds = (
        "Comment;Designator;Quantity;1st source - Manufacturer;1st source - MPN;2nd source - Manufacturer;2nd source - MPN;Library Reference\n"
        "10k;R10 R11;2;Stackpole;RMCF0402FT10K0;YAGEO;RC0402FR-0710KL;CMP-009-00014-1\n"
    )
    ln = parse_csv(cdds.encode())[0]
    assert ln["component"] == "CMP-009-00014-1" and ln["designators"] == ["R10", "R11"]
    assert [c["mpn"] for c in ln["part_choices"]] == ["RMCF0402FT10K0", "RC0402FR-0710KL"]


def test_strip_field():
    from supplywatch.sources.nexar import rejected_fields, strip_field

    q = "part { mpn specs { attribute { shortname } displayValue } sellers(authorizedOnly: true) { offers { factoryLeadDays sku } } }"
    assert "specs" not in strip_field(q, "specs") and "displayValue" not in strip_field(q, "specs")
    assert "factoryLeadDays" not in strip_field(q, "factoryLeadDays") and "sku" in strip_field(q, "factoryLeadDays")
    assert rejected_fields([{"message": "Unauthorized to access 'best_datasheet'. Please upgrade"}]) == {
        "bestDatasheet"
    }
