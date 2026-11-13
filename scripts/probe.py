"""Query the supply sources for a few MPNs and save exactly what they send back.

    python scripts/probe.py LM358DR GRM188R71H104KA93D            # all configured sources except Nexar
    python scripts/probe.py LM358DR --sources mouser,farnell
    python scripts/probe.py LM358DR --sources nexar               # counts against the Octopart plan

Reads credentials from .env (or the environment). Raw responses go to probe-out/<source>/NN.json, with API keys and
tokens redacted; the parsed values (lead time, COO, lifecycle, stock) are printed. Nothing touches the app database.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SECRET_PARAMS = re.compile(r"(apikey|api_key|callinfo\.apikey|client_secret|token)=[^&]*", re.I)
TOKEN_URLS = ("oauth2/token", "connect/token")


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if m and not line.lstrip().startswith("#"):
            os.environ.setdefault(m.group(1), m.group(2).strip().strip('"').strip("'"))


def recording_client(out: Path, timeout) -> httpx.Client:
    out.mkdir(parents=True, exist_ok=True)
    n = [0]

    def save(r: httpx.Response):
        r.read()
        n[0] += 1
        url = SECRET_PARAMS.sub(lambda m: m.group(1) + "=***", str(r.request.url))
        if any(t in url for t in TOKEN_URLS):
            body = "<token response omitted>"
        else:
            try:
                body = r.json()
            except ValueError:
                body = r.text
        f = out / f"{n[0]:02d}.json"
        f.write_text(
            json.dumps(
                {"request": f"{r.request.method} {url}", "status": r.status_code, "body": body},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"  {r.status_code} {r.request.method} {url[:100]}  ->  {f.relative_to(ROOT)}")

    return httpx.Client(timeout=timeout, event_hooks={"response": [save]})


def summary(info: dict | None) -> str:
    if not info:
        return "no match"
    offers = info.get("offers") or []
    leads = sorted({o["lead_days"] for o in offers if o.get("lead_days")})
    stock = sum(o.get("stock") or 0 for o in offers)
    return (
        f"lead_days={info.get('lead_days')}  offer leads={leads or '-'}  coo={info.get('coo') or '-'}  "
        f"lifecycle={info.get('lifecycle') or '-'}  offers={len(offers)}  stock={stock}"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mpns", nargs="+")
    ap.add_argument(
        "--sources",
        default="digikey,mouser,farnell,trustedparts",
        help="comma-separated: digikey, mouser, farnell, trustedparts, nexar",
    )
    ap.add_argument("--out", default=str(ROOT / "probe-out"))
    a = ap.parse_args()

    load_env(ROOT / ".env")
    # scratch database: TrustedParts and Nexar write to the kv/usage tables during a lookup
    os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp()) / 'probe.db'}"
    sys.path.insert(0, str(ROOT / "src"))
    from supplywatch import config, db
    from supplywatch.sources import distributors as d
    from supplywatch.sources import nexar, nexar_supply, trustedparts

    config.reload()
    db.init()

    out = Path(a.out)
    items = [(m, "") for m in a.mpns]
    available = {
        "digikey": (d.DigiKey.enabled, lambda c: {db.norm_mpn(m): d.DigiKey(c).lookup(m) for m in a.mpns}, d.TIMEOUT),
        "mouser": (d.Mouser.enabled, lambda c: d.Mouser(c).lookup_many(a.mpns), d.TIMEOUT),
        "farnell": (d.Farnell.enabled, lambda c: {db.norm_mpn(m): d.Farnell(c).lookup(m) for m in a.mpns}, d.TIMEOUT),
        "trustedparts": (
            trustedparts.enabled,
            lambda c: trustedparts.lookup_many(items, [], client=c),
            trustedparts.TIMEOUT,
        ),
        "nexar": (
            lambda: bool(config.settings.supply_id),
            lambda c: nexar_supply.lookup(items, [], client=c),
            nexar.TIMEOUT,
        ),
    }
    for name in [s.strip().lower() for s in a.sources.split(",") if s.strip()]:
        if name not in available:
            print(f"\n== {name}: unknown source")
            continue
        enabled, run, timeout = available[name]
        print(f"\n== {name}")
        if not enabled():
            print("  not configured in .env, skipped")
            continue
        try:
            res = run(recording_client(out / name, timeout))
        except Exception as e:  # show the failure and carry on with the next source
            print(f"  FAILED: {e}")
            continue
        for m in a.mpns:
            print(f"  {m}: {summary(res.get(db.norm_mpn(m)))}")
    print(f"\nRaw responses saved under {out}")


if __name__ == "__main__":
    main()
