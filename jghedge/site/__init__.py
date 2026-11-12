"""Build the dashboard: docs/index.html (the web page: GitHub Pages, or opened straight from disk)
and dist/jg-hedging-co.html (the same page without the document wrapper, for local use), plus
docs/data/version.json, which an open page polls to find a newer build. The page carries the main
slate (docs/data/latest.json) and, when it exists, tomorrow's (docs/data/next.json)."""
from __future__ import annotations

import json
from pathlib import Path

from ..config import ROOT, SITE_DATA, SITE_DIR

TEMPLATE = Path(__file__).with_name("template.html")
DIST = ROOT / "dist"


def _load(name: str) -> dict:
    p = SITE_DATA / name
    return json.loads(p.read_text()) if p.exists() else {}


REPO_URL = "https://github.com/jacksonpheller-ai/The-Championship-Algorithm---J-G-Industries"
PAGES_URL = "https://jacksonpheller-ai.github.io/The-Championship-Algorithm---J-G-Industries/"
RAW_URL = "https://raw.githack.com/jacksonpheller-ai/The-Championship-Algorithm---J-G-Industries/main/docs/index.html"
# Where any copy of the page (the site, a downloaded file) looks for a newer build: raw.githubusercontent
# allows cross-site reads and needs no setup while the repository is public.
UPDATE_BASE = "https://raw.githubusercontent.com/jacksonpheller-ai/The-Championship-Algorithm---J-G-Industries/main/docs/"


def _recent_results(n: int = 7) -> dict:
    d = SITE_DATA / "results"
    if not d.exists():
        return {}
    files = sorted(p for p in d.glob("*.json") if p.stem != "index")[-n:]
    return {p.stem: json.loads(p.read_text()) for p in files}


def _lines_for(slate: dict) -> list:
    """Today's line snapshots (data/lines/<date>.json) so the page can show the opener, the
    number we priced at and the latest line per game."""
    d = (slate or {}).get("date")
    if not d:
        return []
    p = ROOT / "data" / "lines" / f"{d}.json"
    try:
        return json.loads(p.read_text()) if p.exists() else []
    except ValueError:
        return []


def _slates() -> tuple[dict, dict | None, str | None]:
    """(main slate, tomorrow's slate or None, build stamp). The build stamp is the newer of the two
    'generated' times: an open page compares it with docs/data/version.json to find a newer build."""
    slate, nxt = _load("latest.json"), _load("next.json")
    if not nxt.get("date") or not nxt.get("games") or nxt.get("date") == slate.get("date"):
        nxt = None
    stamps = [x for x in (slate.get("generated"), (nxt or {}).get("generated")) if x]
    return slate, nxt, (max(stamps) if stamps else None)


def render() -> str:
    slate, nxt, build = _slates()
    data = {"slate": slate, "next": nxt, "build": build, "ledger": _load("ledger.json"), "backtest": _load("backtest.json"),
            "replay": _load("replay_open.json"), "results": _recent_results(), "shared": _load("shared_ledger.json") or None,
            "lines": _lines_for(slate), "repo": REPO_URL, "pages_url": PAGES_URL, "raw_url": RAW_URL,
            "update_base": UPDATE_BASE}
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace('"__JG_DATA__"', blob)


def build_standalone() -> Path:
    body = render()
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    page = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            "<meta name=\"robots\" content=\"noindex\">\n</head>\n<body>\n" + body + "\n</body>\n</html>\n")
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
    # a few bytes the open page polls to learn that a newer build exists (template: checkForUpdate)
    slate, _nxt, build = _slates()
    (SITE_DATA / "version.json").write_text(json.dumps({"generated": build, "date": slate.get("date")}))
    (SITE_DIR / ".nojekyll").write_text("")
    DIST.mkdir(exist_ok=True)
    out = DIST / "jg-hedging-co.html"
    out.write_text(body, encoding="utf-8")
    return out
