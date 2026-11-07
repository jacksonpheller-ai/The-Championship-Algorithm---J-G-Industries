"""Shared record of bets actually placed, kept as GitHub issues.

The dashboard's "Log to shared record" opens a pre-filled issue titled "Bets <date>" whose
body ends with a ```json block (date, bankroll, profile, bets[] with legs). The nightly job
reads those issues, grades every bet from final box scores, stores them in
data/shared_ledger.json, publishes docs/data/shared_ledger.json, and comments the result on
the issue once all its bets are graded (then closes it). Runs with the workflow's GITHUB_TOKEN;
without a token it is a no-op, so local runs never touch GitHub.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import requests

from .config import DATA_DIR, SITE_DATA
from .data import history
from .data.espn import PLAYER_COLS
from .ledger import _settle_leg

OWNER_REPO = os.environ.get("GITHUB_REPOSITORY", "jacksonpheller-ai/The-Championship-Algorithm---J-G-Industries")
API = f"https://api.github.com/repos/{OWNER_REPO}"
FILE = DATA_DIR / "shared_ledger.json"
TITLE_RE = re.compile(r"^\s*bets\s+(\d{4}-\d{2}-\d{2})", re.I)
JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def _headers():
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not tok:
        return None
    return {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "jg-desk-bot"}


def load() -> dict:
    if FILE.exists():
        return json.loads(FILE.read_text())
    return {"entries": [], "issues": {}}


def save(data: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, indent=1))
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    (SITE_DATA / "shared_ledger.json").write_text(json.dumps(data, separators=(",", ":")))


def _parse_issue(issue: dict) -> list[dict]:
    m = TITLE_RE.match(issue.get("title") or "")
    if not m:
        return []
    jm = JSON_RE.search(issue.get("body") or "")
    if not jm:
        return []
    try:
        payload = json.loads(jm.group(1))
    except ValueError:
        return []
    date = payload.get("date") or m.group(1)
    out = []
    for i, b in enumerate(payload.get("bets") or []):
        legs = b.get("legs") or []
        if not legs or not isinstance(b.get("stake"), (int, float)) or b["stake"] <= 0:
            continue
        out.append({"id": f"{issue['number']}-{i}", "issue": issue["number"], "date": date,
                    "by": (issue.get("user") or {}).get("login"), "ref": b.get("ref"),
                    "type": "parlay" if len(legs) > 1 else "single",
                    "kind": "parlay" if len(legs) > 1 else legs[0].get("kind"),
                    "label": str(b.get("label") or "")[:200], "price": b.get("price"), "dec": float(b.get("dec") or 0),
                    "stake": float(b["stake"]), "legs": [{k: l.get(k) for k in ("game_id", "kind", "side", "line", "player_id", "stat", "dec", "label")} for l in legs],
                    "result": None, "pnl": None})
    return out


def ingest(season: int) -> dict:
    h = _headers()
    data = load()
    if h is None:
        return {"skipped": "no GITHUB_TOKEN"}
    issues = []
    for page in (1, 2, 3):
        r = requests.get(f"{API}/issues", headers=h, params={"state": "open", "per_page": 100, "page": page}, timeout=30)
        if r.status_code != 200:
            return {"error": f"GitHub API {r.status_code}"}
        batch = [i for i in r.json() if "pull_request" not in i]
        issues.extend(batch)
        if len(batch) < 100:
            break
    known = {e["id"] for e in data["entries"]}
    added = 0
    for issue in issues:
        for e in _parse_issue(issue):
            if e["id"] not in known:
                data["entries"].append(e)
                known.add(e["id"])
                added += 1
        if str(issue["number"]) not in data["issues"]:
            data["issues"][str(issue["number"])] = {"title": issue["title"], "state": "open"}
    # grade
    games = {g["id"]: g for g in history.load_season(season)}
    players = {}
    for g in games.values():
        for p in g.get("players", []):
            r = dict(zip(PLAYER_COLS, p))
            r["pra"], r["pr"], r["pa"], r["ra"] = r["pts"] + r["reb"] + r["ast"], r["pts"] + r["reb"], r["pts"] + r["ast"], r["reb"] + r["ast"]
            players[(g["id"], str(r["id"]))] = r
    graded = 0
    for e in data["entries"]:
        if e.get("result") is not None:
            continue
        res = [_settle_leg(l, games, players) for l in e["legs"]]
        if any(x is None for x in res):
            continue
        if e["type"] == "single":
            r0 = res[0]
            e["result"] = r0
            e["pnl"] = round(e["stake"] * (e["dec"] - 1), 2) if r0 == "win" else (-e["stake"] if r0 == "loss" else 0.0)
        else:
            if "loss" in res:
                e["result"], e["pnl"] = "loss", -e["stake"]
            else:
                dec = 1.0
                for l, r1 in zip(e["legs"], res):
                    dec *= float(l.get("dec") or 1) if r1 == "win" else 1.0
                e["result"] = "win" if dec > 1 else "push"
                e["pnl"] = round(e["stake"] * (dec - 1), 2)
        graded += 1
    # close fully graded issues with a summary comment
    closed = 0
    for num, meta in data["issues"].items():
        if meta.get("state") == "closed":
            continue
        mine = [e for e in data["entries"] if str(e["issue"]) == num]
        if not mine or any(e["result"] is None for e in mine):
            continue
        pnl = sum(e["pnl"] or 0 for e in mine)
        staked = sum(e["stake"] for e in mine if e["result"] in ("win", "loss"))
        lines = [f"- {e['label']}: **{e['result']}** ({'+' if (e['pnl'] or 0) >= 0 else ''}{e['pnl']:.2f})" for e in mine]
        body = (f"Graded {len(mine)} bets for {mine[0]['date']}: net **{'+' if pnl >= 0 else ''}{pnl:.2f}** on {staked:.2f} staked.\n\n"
                + "\n".join(lines) + "\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_")
        requests.post(f"{API}/issues/{num}/comments", headers=h, json={"body": body}, timeout=30)
        requests.patch(f"{API}/issues/{num}", headers=h, json={"state": "closed"}, timeout=30)
        meta["state"] = "closed"
        meta["graded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        closed += 1
    save(data)
    return {"issues_seen": len(issues), "added": added, "graded": graded, "closed": closed, "entries": len(data["entries"])}
