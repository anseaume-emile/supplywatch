"""Runtime configuration, read once from environment variables (see .env.example)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def _list(name: str) -> list[str]:
    """Comma- or newline-separated list."""
    return [x.strip() for x in re.split(r"[,\n]", os.getenv(name, "")) if x.strip()]


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


@dataclass
class Settings:
    # --- general -------------------------------------------------------------
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://localhost:8080").rstrip("/"))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:////data/supplywatch.db"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY"))
    currency: str = field(default_factory=lambda: _env("CURRENCY", "CHF"))
    country: str = field(default_factory=lambda: _env("COUNTRY", "CH"))
    timezone: str = field(default_factory=lambda: _env("TZ", "Europe/Zurich"))

    # --- auth: none (no login) or proxy (trust user header from an SSO proxy) -------
    auth_mode: str = field(default_factory=lambda: _env("AUTH_MODE", "none"))
    auth_user_header: str = field(default_factory=lambda: _env("AUTH_USER_HEADER", "X-Forwarded-Email"))
    auth_groups_header: str = field(default_factory=lambda: _env("AUTH_GROUPS_HEADER", "X-Forwarded-Groups"))
    admin_users: list[str] = field(default_factory=lambda: [u.lower() for u in _list("ADMIN_USERS")])
    admin_group: str = field(default_factory=lambda: _env("ADMIN_GROUP"))
    # MCP server at /mcp for AI assistants. Off unless set; clients send "Authorization: Bearer <token>".
    mcp_token: str = field(default_factory=lambda: _env("MCP_TOKEN"))

    # --- BOM source: git repositories / mounted folders with Altium projects -------
    git_repos: list[str] = field(default_factory=lambda: _list("GIT_REPOS"))  # url or url#branch
    git_token: str = field(default_factory=lambda: _env("GIT_TOKEN"))
    git_username: str = field(default_factory=lambda: _env("GIT_USERNAME"))
    git_ssh_key: str = field(default_factory=lambda: _env("GIT_SSH_KEY"))  # path inside the container
    repos_dir: str = field(default_factory=lambda: _env("REPOS_DIR", "/data/repos"))
    project_dirs: list[str] = field(default_factory=lambda: _list("PROJECT_DIRS"))
    project_filter: str = field(default_factory=lambda: _env("PROJECT_FILTER", r"^PRJ-"))  # regex on .PrjPcb name

    # --- supply sources ---------------------------------------------------------
    trustedparts_company_id: str = field(default_factory=lambda: _env("TRUSTEDPARTS_COMPANY_ID"))
    trustedparts_api_key: str = field(default_factory=lambda: _env("TRUSTEDPARTS_API_KEY"))
    digikey_client_id: str = field(default_factory=lambda: _env("DIGIKEY_CLIENT_ID"))
    digikey_client_secret: str = field(default_factory=lambda: _env("DIGIKEY_CLIENT_SECRET"))
    digikey_site: str = field(default_factory=lambda: _env("DIGIKEY_SITE", "CH"))
    digikey_language: str = field(default_factory=lambda: _env("DIGIKEY_LANGUAGE", "en"))
    mouser_api_key: str = field(default_factory=lambda: _env("MOUSER_API_KEY"))
    farnell_api_key: str = field(default_factory=lambda: _env("FARNELL_API_KEY"))
    farnell_store: str = field(default_factory=lambda: _env("FARNELL_STORE", "ch.farnell.com"))

    # --- optional: Nexar (Octopart data and/or Altium 365). Off unless credentials are set. ---
    nexar_client_id: str = field(default_factory=lambda: _env("NEXAR_CLIENT_ID"))
    nexar_client_secret: str = field(default_factory=lambda: _env("NEXAR_CLIENT_SECRET"))
    nexar_supply_client_id: str = field(default_factory=lambda: _env("NEXAR_SUPPLY_CLIENT_ID"))
    nexar_supply_client_secret: str = field(default_factory=lambda: _env("NEXAR_SUPPLY_CLIENT_SECRET"))
    nexar_supply: bool = field(default_factory=lambda: _bool("NEXAR_SUPPLY", False))
    nexar_lead_time: bool = field(default_factory=lambda: _bool("NEXAR_LEAD_TIME", False))
    nexar_specs: bool = field(default_factory=lambda: _bool("NEXAR_SPECS", False))
    nexar_monthly_part_budget: int = field(default_factory=lambda: _int("NEXAR_MONTHLY_PART_BUDGET", 1800))
    nexar_authorized_only: bool = field(default_factory=lambda: _bool("NEXAR_AUTHORIZED_ONLY", True))
    a365_workspace_url: str = field(default_factory=lambda: _env("A365_WORKSPACE_URL").rstrip("/"))
    a365_project_filter: str = field(default_factory=lambda: _env("A365_PROJECT_FILTER"))

    # --- scheduling (crontab syntax in TZ, or "off") ------------------------------
    bom_sync_cron: str = field(default_factory=lambda: _env("BOM_SYNC_CRON", "*/30 6-20 * * 1-5"))
    supply_refresh_cron: str = field(default_factory=lambda: _env("SUPPLY_REFRESH_CRON", "30 5 * * *"))
    # Parts refreshed more recently than this are skipped by scheduled / "due" refreshes.
    supply_max_age_hours: int = field(default_factory=lambda: _int("SUPPLY_MAX_AGE_HOURS", 20))
    include_alternates: bool = field(default_factory=lambda: _bool("INCLUDE_ALTERNATES", True))

    # --- risk thresholds --------------------------------------------------------
    long_lead_days: int = field(default_factory=lambda: _int("LONG_LEAD_DAYS", 84))
    low_stock_multiple: int = field(default_factory=lambda: _int("LOW_STOCK_MULTIPLE", 3))

    @property
    def supply_id(self) -> str:
        return self.nexar_supply_client_id or self.nexar_client_id

    @property
    def supply_secret(self) -> str:
        return self.nexar_supply_client_secret or self.nexar_client_secret

    @property
    def nexar_supply_enabled(self) -> bool:
        return self.nexar_supply and bool(self.supply_id and self.supply_secret)

    @property
    def nexar_redirect_uri(self) -> str:
        return f"{self.base_url}/admin/nexar/callback"


settings = Settings()


def reload() -> Settings:
    global settings
    settings = Settings()
    return settings
