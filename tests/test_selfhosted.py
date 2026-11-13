"""Default path: BOMs from a git repo of Altium files; supply from TrustedParts, DigiKey and Farnell (mocked)."""

import json
import subprocess
import time
from pathlib import Path

import httpx
import respx

PRJPCB = """﻿[Design]
Version=1.0

[Document1]
DocumentPath=Main.SchDoc

[Document2]
DocumentPath=Sub\\Power.SchDoc

[Document3]
DocumentPath=Board.BomDoc

[ProjectVariant1]
Description=Lite
VariationCount=1
Variation1=Designator=U2|UniqueId=\\BBBBBBBB|Kind=1|AlternatePart=
ParamVariationCount=0
"""

BOMDOC = (
    "|RECORD=BOM|VERSION=6|DATE=01.09.2026\r\n"
    "|RECORD=CatalogItem|DESIGNITEMID=CMP-MCU-1|DESCRIPTION=MCU\r\n"
    "|RECORD=PartChoice|MANUFACTURER=STMicroelectronics|MANUFACTURERPARTNO=STM32G431KBT6|SUPPLIER=DigiKey\r\n"
    "|RECORD=PartChoice|MANUFACTURER=STMicroelectronics|MANUFACTURERPARTNO=STM32G431KBT6|SUPPLIER=Mouser\r\n"
    "|RECORD=Solution|MANUFACTURER=STMicroelectronics|MANUFACTURERPARTNO=STM32G431KBT6\r\n"
)


def comp(idx, des, kind=None, **params):
    """Synthetic SchDoc records for one component starting at record index idx."""
    r1 = {
        "RECORD": "1",
        "LIBREFERENCE": params.pop("lib", "RES"),
        "DESIGNITEMID": params.pop("item", ""),
        "COMPONENTDESCRIPTION": params.pop("desc", ""),
        "CURRENTPARTID": params.pop("part", "1"),
        "UNIQUEID": params.pop("uid", "AAAAAAAA"),
    }
    if kind is not None:
        r1["COMPONENTKINDVERSION2"] = str(kind)
    recs = [r1, {"RECORD": "34", "OWNERINDEX": str(idx), "NAME": "Designator", "TEXT": des}]
    fp = params.pop("fp", "")
    for k, v in params.items():
        recs.append({"RECORD": "41", "OWNERINDEX": str(idx), "NAME": k.replace("_", " "), "TEXT": v})
    if fp:
        recs.append({"RECORD": "44", "OWNERINDEX": str(idx)})
        recs.append(
            {
                "RECORD": "45",
                "OWNERINDEX": str(idx + len(recs) - 1),
                "MODELNAME": fp,
                "MODELTYPE": "PCBLIB",
                "ISCURRENT": "T",
            }
        )
    return recs


def sheet(*components):
    out = []
    for c in components:
        base = len(out)
        recs = c(base)
        out += recs
    return out


MAIN = sheet(
    lambda i: comp(
        i,
        "C1",
        item="CMP-001-1",
        desc="100 nF 0402",
        Comment="=Value",
        Value="100nF",
        **{
            "1st source - Manufacturer": "Murata",
            "1st source - MPN": "GCM155R71C104KA55D",
            "2nd source - Manufacturer": "Samsung",
            "2nd source - MPN": "CL05B104KO5NNNC",
        },
        fp="C0402",
    ),
    lambda i: comp(
        i,
        "C2",
        item="CMP-001-1",
        desc="100 nF 0402",
        Comment="100nF",
        **{
            "1st source - Manufacturer": "Murata",
            "1st source - MPN": "GCM155R71C104KA55D",
            "2nd source - Manufacturer": "Samsung",
            "2nd source - MPN": "CL05B104KO5NNNC",
        },
    ),
    lambda i: comp(i, "U1", item="CMP-MCU-1", lib="MCU", desc="Cortex-M4", Comment="STM32G431", part="1"),
    lambda i: comp(i, "U1", item="CMP-MCU-1", lib="MCU", desc="Cortex-M4", Comment="STM32G431", part="2"),  # 2nd part
    lambda i: comp(i, "TP1", kind=5, item="CMP-TP", lib="TST", desc="Test pad"),
    lambda i: comp(i, "R?", item="CMP-009", lib="RES", desc="unannotated"),
)
POWER = sheet(
    lambda i: comp(
        i,
        "U2",
        item="CMP-REG-1",
        lib="REG",
        desc="LDO 3V3",
        uid="BBBBBBBB",
        **{"Manufacturer 1": "Texas Instruments", "Manufacturer Part Number 1": "TLV73333PDBVR"},
    ),
)
SHEETS = {"Main.SchDoc": MAIN, "Power.SchDoc": POWER}


def make_project(root: Path):
    (root / "PRJ-00099_Test").mkdir(parents=True)
    d = root / "PRJ-00099_Test"
    (d / "PRJ-00099_Test_Rev0.1.PrjPcb").write_bytes(PRJPCB.encode("utf-8"))
    (d / "Main.SchDoc").write_bytes(b"x")
    (d / "Sub").mkdir()
    (d / "Sub" / "Power.SchDoc").write_bytes(b"x")
    (d / "Board.BomDoc").write_bytes(BOMDOC.encode("cp1252"))
    (d / "Other.PrjPcb").write_text("[Design]\n")  # filtered out by ^PRJ-
    return d


def fake_records(monkeypatch):
    from supplywatch.sources import altium_files

    monkeypatch.setattr(altium_files, "read_records", lambda p: [dict(r) for r in SHEETS[Path(p).name]])


def test_parse_project(env, tmp_path, monkeypatch):
    from supplywatch.sources import altium_files

    fake_records(monkeypatch)
    d = make_project(tmp_path)
    log = []
    res = altium_files.project_boms(d / "PRJ-00099_Test_Rev0.1.PrjPcb", log)
    names = [n for n, _ in res]
    assert names == ["[No Variations]", "Lite"]
    base = {tuple(ln["designators"]): ln for ln in res[0][1]}
    assert set(base) == {("C1", "C2"), ("U1",), ("U2",)}  # TP1 (no BOM) and R? (unannotated) left out
    c = base[("C1", "C2")]
    assert c["quantity"] == 2 and c["component"] == "CMP-001-1" and c["footprint"] == "C0402"
    assert [x["mpn"] for x in c["part_choices"]] == ["GCM155R71C104KA55D", "CL05B104KO5NNNC"]
    assert c["comment"] == "100nF" and c["mpn_source"] == "schematic"
    u1 = base[("U1",)]
    assert u1["quantity"] == 1  # multi-part symbol counted once
    assert (
        u1["part_choices"] == [{"mfr": "STMicroelectronics", "mpn": "STM32G431KBT6"}]
        and u1["mpn_source"] == "activebom"
    )
    assert base[("U2",)]["part_choices"][0]["mpn"] == "TLV73333PDBVR"
    lite = {d for ln in res[1][1] for d in ln["designators"]}
    assert "U2" not in lite and "U1" in lite
    assert any("unannotated" in ln for ln in log)


def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True
    )


def wait_idle():
    from supplywatch import jobs

    for _ in range(200):
        if not jobs._lock.locked():
            return
        time.sleep(0.05)


def wait_for_finished(kind, timeout=20):
    from supplywatch import db
    from supplywatch.db import Run

    end = time.time() + timeout
    while time.time() < end:
        with db.session() as s:
            r = s.query(Run).filter(Run.kind == kind).order_by(Run.id.desc()).first()
            if r and r.finished:
                return
        time.sleep(0.05)
    raise AssertionError(f"{kind} did not finish")


def install_supply_mocks(router):
    def tp(request):
        body = json.loads(request.content)
        assert body["CompanyId"] == "cid-tp" and body["ExactMatch"] is True and len(body["Queries"]) <= 50
        res = []
        for q in body["Queries"]:
            m = q["SearchToken"]
            if m == "GCM155R71C104KA55D":
                res.append(
                    {
                        "PartNumber": m,
                        "Manufacturer": "Murata Electronics",
                        "Specifications": [],
                        "Distributors": [
                            {
                                "Name": "DigiKey",
                                "DistributorResults": [
                                    {
                                        "DistributorPartNumber": "490-X",
                                        "Stock": {"QuantityOnHand": 999999},
                                        "Pricing": {"CurrencyCode": "CHF", "Prices": [{"Quantity": 1, "Amount": 0.1}]},
                                        "Links": [],
                                    }
                                ],
                            },
                            {
                                "Name": "Arrow Electronics",
                                "DistributorResults": [
                                    {
                                        "DistributorPartNumber": "A-1",
                                        "Description": "CAP CER 0.1UF 16V",
                                        "Stock": {"QuantityOnHand": 80000},
                                        "LeadTime": "15 weeks",
                                        "Pricing": {
                                            "CurrencyCode": "CHF",
                                            "Prices": [
                                                {"Quantity": 10000, "Amount": 0.004},
                                                {"Quantity": 1, "Amount": 0.05},
                                            ],
                                        },
                                        "Links": [
                                            {"Type": "Datasheet", "Url": "https://ds"},
                                            {"Type": "Buy", "Url": "https://arrow/buy"},
                                        ],
                                    }
                                ],
                            },
                        ],
                    }
                )
            if m == "TLV73333PDBVR":
                res.append(
                    {
                        "PartNumber": m,
                        "Manufacturer": "Texas Instruments",
                        "Distributors": [
                            {
                                "Name": "Avnet",
                                "DistributorResults": [
                                    {
                                        "DistributorPartNumber": "AV-1",
                                        "Stock": {"QuantityOnHand": 50},
                                        "Pricing": {"CurrencyCode": "CHF", "Prices": [{"Quantity": 1, "Amount": 0.3}]},
                                    }
                                ],
                            }
                        ],
                    }
                )
        return httpx.Response(200, json={"ErrorMessage": None, "PartResults": res})

    router.post("https://api.trustedparts.com/v2/search").mock(side_effect=tp)

    router.post("https://api.digikey.com/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "dk", "expires_in": 600})
    )

    def dk(request):
        mpn = request.url.path.split("/")[-2]
        if mpn == "GCM155R71C104KA55D":
            return httpx.Response(
                200,
                json={
                    "Product": {
                        "ManufacturerProductNumber": mpn,
                        "Manufacturer": {"Name": "Murata"},
                        "ManufacturerLeadWeeks": "12",
                        "ProductStatus": {"Status": "Active"},
                        "Classifications": {
                            "HtsusCode": "8532.24.0020",
                            "ExportControlClassNumber": "EAR99",
                            "RohsStatus": "ROHS3 Compliant",
                            "ReachStatus": "REACH Affected",
                        },
                        "ProductVariations": [
                            {
                                "DigiKeyProductNumber": "490-4759-1-ND",
                                "QuantityAvailableforPackageType": 250000,
                                "MinimumOrderQuantity": 1,
                                "PackageType": {"Name": "Cut Tape"},
                                "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 0.09}],
                            }
                        ],
                    }
                },
            )
        return httpx.Response(404)

    router.get(url__regex=r"https://api\.digikey\.com/products/v4/search/.*/productdetails").mock(side_effect=dk)
    router.post("https://api.digikey.com/products/v4/search/keyword").mock(
        return_value=httpx.Response(200, json={"Products": []})
    )

    def farnell(request):
        term = request.url.params["term"]
        assert request.url.params["storeInfo.id"] == "ch.farnell.com"
        mpn = term.split(":", 1)[1]
        if mpn == "TLV73333PDBVR":
            return httpx.Response(
                200,
                json={
                    "manufacturerPartNumberSearchReturn": {
                        "numberOfResults": 1,
                        "products": [
                            {
                                "sku": "2361243",
                                "displayName": "LDO 3.3V 300mA SOT-23-5",
                                "translatedManufacturerPartNumber": "TLV73333PDBVR",
                                "brandName": "TEXAS INSTRUMENTS",
                                "translatedMinimumOrderQuality": 5,
                                "countryOfOrigin": "CN",
                                "releaseStatusCode": 6,
                                "rohsStatusCode": "YES",
                                "packSize": 1,
                                "unitOfMeasure": "EACH",
                                "stock": {"level": 1200, "leastLeadTime": 98, "status": 1},
                                "prices": [{"from": 5, "to": 99, "cost": 0.41}, {"from": 100, "to": 999, "cost": 0.28}],
                                "datasheets": [{"url": "https://ti/ds"}],
                            }
                        ],
                    }
                },
            )
        return httpx.Response(200, json={"manufacturerPartNumberSearchReturn": {"numberOfResults": 0}})

    router.get(url__startswith="https://api.element14.com/catalog/products").mock(side_effect=farnell)


def test_git_sync_and_supply(env, tmp_path, monkeypatch):
    fake_records(monkeypatch)
    repo = tmp_path / "origin"
    repo.mkdir()
    make_project(repo)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    monkeypatch.setenv("GIT_REPOS", f"file://{repo}")
    monkeypatch.setenv("TRUSTEDPARTS_COMPANY_ID", "cid-tp")
    monkeypatch.setenv("TRUSTEDPARTS_API_KEY", "k")
    monkeypatch.setenv("FARNELL_API_KEY", "fk")
    monkeypatch.setenv("MOUSER_API_KEY", "")
    monkeypatch.setenv("NEXAR_SUPPLY", "false")
    monkeypatch.setenv("NEXAR_CLIENT_ID", "")
    from supplywatch import config, jobs

    config.reload()
    from supplywatch.sources import trustedparts

    monkeypatch.setattr(trustedparts.time, "sleep", lambda s: None)

    from fastapi.testclient import TestClient

    from supplywatch import main

    c = TestClient(main.app)
    c.__enter__()
    H = {"X-Forwarded-Email": "admin@cdds.ai", "X-Requested-With": "supplywatch"}

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.route(url__startswith="file://").pass_through()
        install_supply_mocks(router)
        assert c.post("/api/runs/bom-sync", headers=H).status_code == 200
        wait_for_finished("supply_refresh")
        st = c.get("/api/status", headers=H).json()
        runs = {r["kind"]: r for r in st["runs"]}
        assert runs["bom_sync"]["status"] == "ok", c.get(f"/api/runs/{runs['bom_sync']['id']}", headers=H).json()
        assert runs["supply_refresh"]["status"] == "ok", c.get(
            f"/api/runs/{runs['supply_refresh']['id']}", headers=H
        ).json()
        assert st["bom_sources"]["git"]["configured"]

        d = c.get("/api/data", headers=H).json()
        projs = {p["variant"]: p for p in d["projects"]}
        base = projs["[No Variations]"]
        assert base["name"] == "PRJ-00099_Test_Rev0.1" and base["source"] == "git" and base["active"]
        assert base["revision"] and "PRJ-00099_Test/PRJ-00099_Test_Rev0.1.PrjPcb" in base["location"]
        assert not projs["Lite"]["active"]
        parts = {p["mpn"]: p for p in d["parts"]}

        cap = parts["GCM155R71C104KA55D"]
        det = c.get("/api/parts/GCM155R71C104KA55D", headers=H).json()
        sellers = {(o["seller"], o["source"]) for o in det["offers"]}
        # DigiKey's listing inside TrustedParts replaced by direct DigiKey data; Arrow kept from TrustedParts
        assert ("DigiKey", "digikey") in sellers and ("Arrow Electronics", "trustedparts") in sellers
        assert ("DigiKey", "trustedparts") not in sellers
        assert cap["stock"] == 250000 + 80000
        assert cap["lead_days"] == 84 and cap["lead_source"] == "digikey"  # DigiKey beats TrustedParts' 15 weeks
        arrow = next(o for o in det["offers"] if o["seller"] == "Arrow Electronics")
        assert arrow["lead_days"] == 105 and arrow["url"] == "https://arrow/buy" and arrow["prices"][0]["qty"] == 1
        assert cap["hts"] == "8532.24.0020"
        assert cap["rohs"] == "Compliant" and cap["rohs_source"] == "digikey"
        assert cap["reach"] == "Contains SVHC" and "reach_svhc" in cap["flags"]
        assert det["sources"]["digikey"]["reach"] == "REACH Affected"  # raw value kept per source

        ldo = parts["TLV73333PDBVR"]
        assert ldo["coo"] == "China" and ldo["coo_source"] == "farnell"
        assert ldo["lifecycle"] == "EOL" and "lifecycle" in ldo["flags"]
        assert ldo["lead_days"] == 98 and ldo["lead_source"] == "farnell"
        assert ldo["stock"] == 1200 + 50
        assert ldo["rohs"] == "Compliant" and ldo["rohs_source"] == "farnell"  # Farnell's rohsStatusCode "YES"
        assert ldo["reach"] == "" and "not_rohs" not in ldo["flags"]

        mcu = parts["STM32G431KBT6"]
        assert "not_found" in mcu["flags"]

        from supplywatch import db

        with db.session() as s:
            assert db.kv_get(s, "trustedparts_sample")["PartNumber"] in ("GCM155R71C104KA55D", "TLV73333PDBVR")

    # a commit removing the project excludes it on the next sync
    _git("rm", "-rq", "PRJ-00099_Test", cwd=repo)
    _git("commit", "-qm", "rm", cwd=repo)
    jobs.bom_sync("test")
    d = c.get("/api/data", headers=H).json()
    assert not any(p["active"] for p in d["projects"])


def test_local_folder_dedupes_and_takes_over(env, tmp_path, monkeypatch):
    """Two copies of a project in a folder: only the newest is synced. A project already known from another
    source (here Altium 365) hands its active flag and build quantity to the folder copy and is deactivated."""
    import os
    import time

    fake_records(monkeypatch)
    old, new = tmp_path / "old", tmp_path / "new"
    make_project(old)
    make_project(new)
    stale = time.time() - 86400
    for f in old.rglob("*"):
        os.utime(f, (stale, stale))
    monkeypatch.setenv("PROJECT_DIRS", str(tmp_path))
    monkeypatch.setenv("GIT_REPOS", "")
    from supplywatch import config, db

    config.reload()
    from sqlalchemy import select

    from supplywatch.db import Project
    from supplywatch.sources import git_repos

    with db.session() as s:
        s.add(
            Project(
                source="a365",
                external_id="a365:x::Lite",
                name="PRJ-00099_Test_Rev0.1",
                variant="Lite",
                build_qty=250,
                active=True,
            )
        )
    log = []
    r = git_repos.sync(log)
    assert r["projects"] == 1
    assert any(ln.startswith(f"skipped {tmp_path}/old/") for ln in log), log
    with db.session() as s:
        rows = {(p.source, p.variant): p for p in s.scalars(select(Project)).all()}
        lite, base = rows[("git", "Lite")], rows[("git", "[No Variations]")]
        assert "/new/" in lite.location and lite.revision.startswith("saved ")
        assert lite.active and lite.build_qty == 250  # taken over from the Altium 365 copy
        assert not rows[("a365", "Lite")].active
        assert not base.active  # project already known; this variant wasn't, so it doesn't start counting
    assert any("settings taken over from the a365 copy" in ln for ln in log)
    # a second sync keeps the folder copy's settings and doesn't log the takeover again
    log2 = []
    git_repos.sync(log2)
    assert not any("taken over" in ln for ln in log2)
    with db.session() as s:
        assert s.scalar(select(Project).where(Project.source == "git", Project.variant == "Lite")).build_qty == 250


def test_git_token_is_scrubbed(env, monkeypatch):
    monkeypatch.setenv("GIT_TOKEN", "s3cret")
    from supplywatch import config

    config.reload()
    from supplywatch.sources import git_repos

    url = git_repos._auth_url("https://github.com/cdds/prj.git")
    assert "s3cret" in url and "s3cret" not in git_repos._scrub(f"fatal: could not read {url}")


def test_country_names():
    from supplywatch.countries import country_name

    assert country_name("CN") == "China" and country_name("MYS") == "Malaysia" and country_name("TW") == "Taiwan"
    assert country_name("Japan") == "Japan" and country_name("") == ""


def test_no_login_mode(env, monkeypatch):
    monkeypatch.delenv("AUTH_MODE", raising=False)
    from supplywatch import config

    config.reload()
    assert config.settings.auth_mode == "none"
    from fastapi.testclient import TestClient

    from supplywatch import main

    c = TestClient(main.app)
    me = c.get("/api/me").json()
    assert me == {"email": "anonymous", "admin": True}
    assert c.patch("/api/projects/999", json={"build_qty": 5}).status_code == 403  # CSRF header still required
    assert (
        c.patch("/api/projects/999", json={"build_qty": 5}, headers={"X-Requested-With": "supplywatch"}).status_code
        == 404
    )
