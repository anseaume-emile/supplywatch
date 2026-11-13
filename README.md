# Supply Watch

A self-hosted procurement view of every component in the CDDS Altium projects: stock per supplier, lead time, lifecycle, country of origin and trade codes, and shortages against planned build quantities.

It uses no paid services:

* **BOMs** come straight from the Altium project files in your git repositories: `.PrjPcb`, `.SchDoc` and `.BomDoc` are parsed on the server. You need no Altium licence and no Altium 365.
* **Supply data** comes from free distributor APIs: **TrustedParts** (ECIA's feed of authorised distributors), **DigiKey**, **Mouser** and **Farnell**.

Octopart (Nexar) and Altium 365 can be switched on later; see [Optional: Nexar](#optional-nexar).

**Owner:** Emile Anseaume (hardware / procurement tooling).

## Layout
The folder follows the CDDS application layout (Development view), so it can sit in the monorepo as `applications/supplywatch/`:

| Path | Contents |
|---|---|
| `src/supplywatch/` | The application (FastAPI app, BOM parsers, supply sources, MCP server, web UI in `static/`) |
| `tests/` | Unit tests (pytest); all external APIs are mocked |
| `docker/` | `Dockerfile`, `docker-compose.yml`, `entry_point.sh` and the `Caddyfile` for HTTPS |
| `scripts/probe.py` | Queries the supply sources for a few MPNs and saves the raw responses |
| `Makefile` | Commands, named `supplywatch-<action>`; `build`, `test`, `lint` are the common CI entry points |
| `pyproject.toml`, `uv.lock` | Dependencies (managed with uv); ruff and ty settings |
| `requirements.txt` | Exported from `uv.lock` for tools that don't read it; don't edit by hand |

## What procurement sees

| Page | Contents |
|---|---|
| Overview | Shortages, lifecycle risks, long lead times, COO coverage, lead-time and COO breakdowns |
| Parts | Each MPN once, across all active projects: required quantity, authorised stock and coverage, number of suppliers, lead time, lifecycle, COO, best price for the required quantity, issues. Filters; CSV export |
| Part page | Every supplier offer (stock, on order, MOQ, lead, packaging, price breaks, link), values from each source, COO entry |
| Projects | Build quantity per project/variant (drives "required" and shortages), git revision, BOM with supply status, CSV export |
| Admin | Sources, manual syncs, BOM upload for projects not in git, run log |

Issues flagged: no MPN, not found, lifecycle (EOL/NRND/obsolete), shortage, low stock, long lead, single source, no COO, stale data.

## How BOMs are read

For every `.PrjPcb` whose name matches `PROJECT_FILTER` (default `^PRJ-`):

1. The schematic documents and ActiveBOM file are taken from the project file.
2. Every component is read from each `.SchDoc`: designator, CMP number (Design Item ID), description, comment, footprint and parameters.
   * Components marked *Standard (No BOM)*, *Graphical* or *Net Tie* are skipped, as Altium does.
   * Multi-part symbols are counted once.
   * Unannotated parts (`R?`) are skipped and logged.
3. MPNs come from the component parameters: the CDDS `1st/2nd/3rd source - Manufacturer / MPN` style, or Altium's `Manufacturer 1 / Manufacturer Part Number 1`.
   * If a component has none, the part choices in the project's `.BomDoc` are used. That covers managed Altium 365 library parts, such as the Helbling parts in PRJ-00006.
4. Variants from the project file become separate project entries without their not-fitted parts. Only the base design is active by default; switch on the variant you actually build on the Projects page.

Checked against Altium's own compiled output for PRJ-00002, 00004, 00006 and 00007: the same 207 placements, designators and preferred MPNs.

**Limitation:** multi-channel designs (`Repeat(...)` sheet symbols) get their channel designators at compile time. The parser logs a warning and counts the channel sheet once.

Syncs pull the repositories every 30 minutes in working hours (`BOM_SYNC_CRON`). New parts get supply data immediately.

## Where each field comes from

| Field | TrustedParts | DigiKey | Mouser | Farnell | Manual |
|---|---|---|---|---|---|
| Stock per distributor | ✔ many authorised distributors | ✔ | ✔ + on order | ✔ | |
| Prices, MOQ | ✔ | ✔ | ✔ | ✔ | |
| Lead time | ✔ (field name found at runtime) | ✔ 1st choice | ✔ | ✔ | |
| Lifecycle | if listed | ✔ | ✔ | ✔ | |
| HTS / ECCN | | ✔ | ✔ | | |
| Country of origin | | if present | if present | ✔ **`countryOfOrigin`** | ✔ wins |

* DigiKey, Mouser and Farnell data replaces those distributors' listings inside TrustedParts, so stock is not counted twice.
* Country codes are shown as country names.
* Manual COO entries override API values and survive every refresh.

## Setup

### 1. API keys (all free)
| Service | Where | `.env` |
|---|---|---|
| TrustedParts | <https://www.trustedparts.com>: create an account → My Account → API Keys (approved by ECIA) | `TRUSTEDPARTS_COMPANY_ID`, `TRUSTEDPARTS_API_KEY` |
| DigiKey | <https://developer.digikey.com>: organisation → production app with *Product Information v4* | `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` |
| Mouser | <https://www.mouser.com/api-search/>: Search API key | `MOUSER_API_KEY` |
| Farnell | <https://partner.element14.com>: register for the Product Search API | `FARNELL_API_KEY` |

Any subset works; more sources mean better coverage.

**TrustedParts terms:** data is for internal purchasing decisions and may be cached for at most a week. The app refreshes daily.

### 2. Git access
* List the project repositories in `GIT_REPOS`, comma-separated (`url#branch` pins a branch). `.env.example` already lists the four current projects' remotes.
* **The current projects live in Altium 365 version control** (`afs-vcs-eu1.365.altium.com`). Altium does not document access from plain git clients.
  * **Test first:** from any machine, run `git clone <url>` using an Altium account email and password.
  * **If that works:** put a dedicated read-only Altium account's email in `GIT_USERNAME` and its password in `GIT_TOKEN`.
  * **If it doesn't** (e.g. SSO-only accounts): add a second remote on GitHub or GitLab and push to both, or mirror the repos with a scheduled job. Then point `GIT_REPOS` at the mirror.
* **HTTPS:** a read-only token in `GIT_TOKEN`, e.g. a GitHub fine-grained token with *Contents: read* on those repositories.
* **SSH:** mount a read-only deploy key and set `GIT_SSH_KEY`.
* **Local folders or a file share instead of (or as well as) git:** set `ALTIUM_PROJECTS_DIR` to the folder on the host
  (e.g. `C:/Users/Public/Documents/Altium`, Altium's default projects folder) and `PROJECT_DIRS=/projects`;
  `docker/docker-compose.yml` mounts it read-only. Rebuild/restart with `make supplywatch-docker-up`.
  * A project found more than once (two copies in the folder, or the same project in git and in the folder) is synced
    once, from the most recently saved copy; the sync log lists the copies it skipped.
  * When a project moves to a new location, its active variants and build quantities carry over and the old copy is
    deactivated, so demand is never counted twice.

### 3. Run
```bash
cp .env.example .env        # fill in the values
openssl rand -hex 32        # → SECRET_KEY
make supplywatch-docker-up  # docker compose --env-file .env -f docker/docker-compose.yml up -d --build
```
Open `https://<server>` (see [HTTPS](#https) to make browsers trust it).

> **There is no login** (`AUTH_MODE=none`): anyone who can reach the server can view everything and run syncs. Keep it on the internal network or VPN; don't expose it to the internet.

**Adding SSO later:** put an SSO proxy in front of the app, such as [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/), Authelia, Authentik or Cloudflare Access. Then:
1. Set `AUTH_MODE=proxy`.
2. List the admins in `ADMIN_USERS` or `ADMIN_GROUP`.
3. Make sure the app is only reachable through the proxy. `docker/docker-compose.yml` already publishes port 8080 on 127.0.0.1 only.

The app then reads the user's email from `AUTH_USER_HEADER`. Only admins can run syncs, upload BOMs and include or exclude projects.

### 4. First run
1. Open **Admin** and click **Sync BOMs now**. A supply refresh follows automatically.
2. On **Projects**, set build quantities and choose which variants are active.

Projects outside git can be added from an Altium BOM export (CSV) on the Admin page.

## Schedule and limits
* **BOM sync:** every 30 min, Mon–Fri 06:00–20:00. A daily supply refresh runs at 05:30.
* **Daily usage:** the four current projects have 130 MPNs including alternates. At one refresh a day that stays well inside the free limits:
  * DigiKey: about 1,000 requests/day
  * Mouser: 1,000 requests/day, 30/min
  * TrustedParts: 15,000 parts/day with real-time data
  * Farnell: about 2 requests/s
* The app paces requests to stay under the per-minute limits.

## Operations
* **Data:** kept in the `supplywatch-data` volume, both the database and the repository clones. Back up `/data/supplywatch.db`, or point `DATABASE_URL` at Postgres.
* **Workers:** run one container with one worker. The scheduler and the job lock are in-process.
* **API docs:** `/api/docs`.
  * JSON: `/api/data`, `/api/parts/{mpn}`, `/api/status`
  * CSV: `/api/export/parts.csv`, `/api/export/projects/{id}.csv`

## MCP server (AI assistants)
Claude, Cursor and other MCP clients can use Supply Watch at `https://<server>/mcp` (streamable HTTP).
It is off until you set `MCP_TOKEN` in `.env` (`openssl rand -hex 32`) and restart. Clients send it as a bearer token:
```bash
claude mcp add --transport http supplywatch https://<server>/mcp --header "Authorization: Bearer <MCP_TOKEN>"
```
* **Read tools:** `overview`, `list_parts` (filter by issue flag, project or text), `get_part`, `list_projects`, `get_project_bom`, `sync_status`
* **Write tools:** `set_build_qty`, `set_project_active`, `set_coo`, `refresh_part`, `run_bom_sync`, `run_supply_refresh`. Changes are logged as user `mcp`.
* The token gives full access, including with `AUTH_MODE=proxy`. Give it only to people who may change build quantities and run syncs, and change it if it leaks.
* The endpoint is on the same port as the UI, so it is reachable wherever the UI is. claude.ai web connectors connect from Anthropic's servers and can't reach a server that is only on the internal network; use Claude Code, Claude Desktop or another local client.
* Clients must trust the server's root certificate; see [HTTPS](#https). Node-based clients (Cursor, VS Code, `mcp-remote`) also need `NODE_EXTRA_CA_CERTS`.

## HTTPS
[Caddy](https://caddyserver.com) (the `caddy` service in `docker/docker-compose.yml`, configured by `docker/Caddyfile`) serves the UI and `/mcp` over HTTPS on port 443. Port 80 redirects to it. The app's own port 8080 only accepts connections from the server itself.

* **Names:** list every name and IP that clients use in `HTTPS_HOSTS`, e.g. `eans, 10.150.101.165, localhost`. If clients connect by IP, also set that IP in `HTTPS_DEFAULT_SNI`. Then run `make supplywatch-docker-up`.
* **Certificates** come from Caddy's own certificate authority and renew themselves. Its root certificate is valid for 10 years and is kept in the `caddy-data` volume. If you delete that volume, Caddy creates a new root and every client has to trust it again.
* **Trust the root once on each client device.** Export it from the server:
  ```bash
  docker compose --env-file .env -f docker/docker-compose.yml cp caddy:/data/caddy/pki/authorities/local/root.crt supplywatch-root-ca.crt
  ```
  Then install it on the client:
  * **Windows** (PowerShell, for your user only; Chrome and Edge use this store):
    `Import-Certificate -FilePath supplywatch-root-ca.crt -CertStoreLocation Cert:\CurrentUser\Root`
  * **macOS:** `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain supplywatch-root-ca.crt`
  * **Firefox** has its own store: Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import.
  * **Node-based MCP clients** (Cursor, VS Code, Claude Desktop through `mcp-remote`) ignore the system store. Set the environment variable `NODE_EXTRA_CA_CERTS` to the full path of `supplywatch-root-ca.crt`, then restart the app.
  * **Python clients:** set `SSL_CERT_FILE` to the file, or pass it as the CA bundle.
* **Switching to a company or Let's Encrypt certificate later:** change `tls internal` in `docker/Caddyfile` (see Caddy's `tls` directive). Clients that already trust a public or company CA then need no setup.
* **Windows hosts:** Docker Desktop opens ports 80 and 443 itself. If clients can't connect, allow inbound TCP 80/443 in Windows Firewall.

## Check on the first live run
* **TrustedParts lead time, MOQ and packaging:** these field names are not published. The app finds them by key name and stores the first raw result (`trustedparts_sample` in the `kv` table) so you can confirm.
* **Farnell lead time:** `stock.leastLeadTime` is assumed to be in days, and the `ch.farnell.com` store is assumed to price in CHF.

## Optional: Nexar
Octopart data and Altium 365 BOMs remain available and are off by default:
* **Octopart data:** set `NEXAR_CLIENT_ID`/`SECRET` and `NEXAR_SUPPLY=true`. It is added as another source, with a monthly budget guard (`NEXAR_MONTHLY_PART_BUDGET`).
* **Altium 365 BOMs:** set the Nexar credentials and sign in from the Admin page. Register `<BASE_URL>/admin/nexar/callback` as the redirect URI.

## Development
Python 3.12, [uv](https://docs.astral.sh/uv/) for dependencies, ruff for linting and formatting, ty for type checking. pre-commit runs ruff and ty on every commit.
```bash
make supplywatch-install      # uv sync + pre-commit install
make supplywatch-test         # pytest; all external APIs mocked, git tests use a local repo
make supplywatch-lint         # ruff check, ruff format --check, ty check
make supplywatch-format       # apply ruff fixes and formatting
make supplywatch-run          # dev server on :8080 with AUTH_MODE=none and ./dev.db
make supplywatch-build        # Docker image cdds/supplywatch:latest (TAG=..., PUSH=true)
make supplywatch-lock         # after changing dependencies: update uv.lock and requirements.txt
uv run scripts/probe.py LM358DR --sources mouser,farnell
```
Set `PROJECT_DIRS=/path/to/altium` for `supplywatch-run` to scan local projects.
