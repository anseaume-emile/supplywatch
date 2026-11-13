import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("AUTH_MODE", "proxy")
    monkeypatch.setenv("ADMIN_USERS", "admin@cdds.ai")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("NEXAR_CLIENT_ID", "cid")
    monkeypatch.setenv("NEXAR_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "dk")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "dks")
    monkeypatch.setenv("MOUSER_API_KEY", "mk")
    monkeypatch.setenv("BASE_URL", "https://parts.example.com")
    monkeypatch.setenv("BOM_SYNC_CRON", "off")
    monkeypatch.setenv("SUPPLY_REFRESH_CRON", "off")
    monkeypatch.setenv("NEXAR_SUPPLY", "true")
    monkeypatch.setenv("SUPPLY_MAX_AGE_HOURS", "120")
    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "repos"))
    for k in (
        "NEXAR_LEAD_TIME",
        "NEXAR_SPECS",
        "A365_WORKSPACE_URL",
        "A365_PROJECT_FILTER",
        "GIT_REPOS",
        "PROJECT_DIRS",
        "TRUSTEDPARTS_COMPANY_ID",
        "TRUSTEDPARTS_API_KEY",
        "FARNELL_API_KEY",
        "GIT_TOKEN",
        "PROJECT_FILTER",
    ):
        monkeypatch.delenv(k, raising=False)
    from supplywatch import config, db

    config.reload()
    db.init()
    from supplywatch.sources import distributors

    monkeypatch.setattr(distributors.Mouser, "MIN_INTERVAL", 0)
    monkeypatch.setattr(distributors.Farnell, "MIN_INTERVAL", 0)
    yield config.settings
