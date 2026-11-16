"""PrizePicks boards pasted by hand, saved to the repository and shared between the two of us.

No server can read PrizePicks (Cloudflare refuses GitHub's network), so the board arrives by hand:
one of us opens PrizePicks' projections address on a phone, copies the text and pastes it into the
page. The page parses it, packs it into one short line of text and opens a pre-filled GitHub issue
titled "Board <slate date> ...". A small workflow (.github/workflows/board.yml) runs when that
issue is opened: it calls `python -m jghedge boards --event <file>`, which

  * decodes the packed board (`decode`),
  * archives it as data/boards/<slate date>/<UTC read time>Z.json, e.g. 2026-11-16/20261116T234105Z.json,
  * publishes the newest one as docs/data/board.json (the page polls it, so the other person's
    page picks the board up with no taps),
  * comments on the issue and closes it.

The nightly `ingest` does the same for any board issue the workflow missed.

The packed format (written by the page, jghedge/site/template.html: packBoard) is
"JGB1." + base64url(deflate-raw(JSON)) or, when the browser cannot compress,
"JGB0." + base64url(JSON). The JSON is

    {"v": 1, "read_at": ISO time pasted, "date": slate date (Eastern),
     "n": projections in the pasted text, "skipped": {"unsupported": k, "other_league": k},
     "games": [start_time, ...], "players": [[name, team], ...],
     "rows": [[player index, stat key, line, line type, game index, multiplier or null], ...]}

line type: 0 standard, 1 demon, 2 goblin, 3 anything else. The multiplier is copied from the board
when the board carries one; it is null otherwise (no payout is ever assumed here).
"""
from __future__ import annotations

import base64
import json
import os
import re
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR, SITE_DATA

BOARD_DIR = DATA_DIR / "boards"
LATEST = SITE_DATA / "board.json"
OWNER_REPO = os.environ.get("GITHUB_REPOSITORY", "jacksonpheller-ai/The-Championship-Algorithm---J-G-Industries")
API = f"https://api.github.com/repos/{OWNER_REPO}"
TITLE_RE = re.compile(r"^\s*board\s+(\d{4}-\d{2}-\d{2})", re.I)
PACK_RE = re.compile(r"JGB([01])\.([A-Za-z0-9_\-]+=*)")
TYPES = {0: "standard", 1: "demon", 2: "goblin", 3: "other"}
MAX_PACKED = 200_000      # characters: a board is a few thousand; anything far bigger is not one


def _b64(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def decode(text: str) -> dict | None:
    """Packed board (or an issue body that contains one) -> payload dict, or None."""
    if not text or len(text) > 4 * MAX_PACKED:
        return None
    m = PACK_RE.search(text)
    if not m or len(m.group(2)) > MAX_PACKED:
        return None
    try:
        raw = _b64(m.group(2))
        if m.group(1) == "1":
            raw = zlib.decompress(raw, -15)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, zlib.error, UnicodeDecodeError):
        return None
    return payload if valid(payload) else None


def encode(payload: dict, compress: bool = True) -> str:
    """The inverse of `decode` (the page does this; kept here for tests and tools)."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if compress:
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        raw = c.compress(raw) + c.flush()
    return ("JGB1." if compress else "JGB0.") + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def valid(p) -> bool:
    if not isinstance(p, dict) or p.get("v") != 1:
        return False
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(p.get("date") or "")):
        return False
    if not isinstance(p.get("rows"), list) or not isinstance(p.get("players"), list):
        return False
    try:
        datetime.fromisoformat(str(p.get("read_at")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def expand(p: dict) -> list[dict]:
    """Payload -> flat rows {name, team, stat, line, odds_type, multiplier, start_time}. Bad rows are dropped."""
    players, games, out = p.get("players") or [], p.get("games") or [], []
    for r in p.get("rows") or []:
        if not isinstance(r, list) or len(r) < 4:
            continue
        try:
            pi, stat, line, t = int(r[0]), str(r[1]), float(r[2]), int(r[3])
        except (TypeError, ValueError):
            continue
        if not (0 <= pi < len(players)) or line != line:
            continue
        gi = r[4] if len(r) > 4 else None
        mult = r[5] if len(r) > 5 else None
        pl = players[pi] if isinstance(players[pi], list) else [players[pi], None]
        out.append({
            "name": str(pl[0] or ""), "team": (pl[1] if len(pl) > 1 else None) or None, "stat": stat, "line": line,
            "odds_type": TYPES.get(t, "other"),
            "multiplier": float(mult) if isinstance(mult, (int, float)) and mult > 0 else None,
            "start_time": games[gi] if isinstance(gi, int) and 0 <= gi < len(games) else None,
        })
    return out


def _stamp(read_at: str) -> datetime:
    return datetime.fromisoformat(str(read_at).replace("Z", "+00:00")).astimezone(timezone.utc)


def archive_path(p: dict) -> Path:
    return BOARD_DIR / p["date"] / (_stamp(p["read_at"]).strftime("%Y%m%dT%H%M%S") + "Z.json")


def summary(rows: list[dict]) -> dict:
    by = {}
    for r in rows:
        by[r["odds_type"]] = by.get(r["odds_type"], 0) + 1
    return {"lines": len(rows), "by_type": by, "players": len({r["name"] for r in rows})}


def archive(p: dict, packed: str, source: dict | None = None) -> tuple[Path, bool]:
    """Write the board to data/boards/<date>/<time>Z.json. Returns (path, written); a board already
    archived (same date and read time) is left as it is."""
    path = archive_path(p)
    if path.exists():
        return path, False
    rows = expand(p)
    rec = {"date": p["date"], "read_at": _stamp(p["read_at"]).isoformat(timespec="seconds").replace("+00:00", "Z"),
           "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
           "source": source or {}, "pasted_projections": p.get("n"), "skipped": p.get("skipped") or {},
           "summary": summary(rows), "rows": rows, "packed": packed}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=0, separators=(",", ":")))
    return path, True


def publish_latest() -> dict | None:
    """docs/data/board.json = the newest archived board (by slate date, then read time), packed as the
    page wrote it so the page decodes it the same way it decodes an issue."""
    files = sorted(BOARD_DIR.glob("*/*Z.json"))
    if not files:
        return None
    newest = max(files, key=lambda f: (f.parent.name, f.name))
    rec = json.loads(newest.read_text())
    out = {"date": rec["date"], "read_at": rec["read_at"], "saved_at": rec.get("saved_at"), "source": rec.get("source") or {},
           "summary": rec.get("summary"), "packed": rec["packed"], "file": str(newest.relative_to(DATA_DIR.parent))}
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(out, separators=(",", ":")))
    return out


def load_archive(day: str) -> list[dict]:
    """Every board archived for a slate date, oldest first."""
    d = BOARD_DIR / day
    return [json.loads(f.read_text()) for f in sorted(d.glob("*Z.json"))] if d.exists() else []


def from_issue(issue: dict) -> tuple[dict, str] | None:
    """(payload, packed text) when the issue is a board issue whose title date matches the board's own."""
    m = TITLE_RE.match(issue.get("title") or "")
    if not m:
        return None
    body = issue.get("body") or ""
    pm = PACK_RE.search(body)
    p = decode(body)
    if not p or not pm or p["date"] != m.group(1):
        return None
    return p, pm.group(0)


def _source(issue: dict) -> dict:
    return {"issue": issue.get("number"), "by": (issue.get("user") or {}).get("login")}


def save_issue(issue: dict) -> dict:
    """Archive one board issue. Returns what happened (for the issue comment and the log)."""
    got = from_issue(issue)
    if not got:
        return {"ok": False, "why": "no board found: the title must start 'Board <date>' and the body hold the JGB text the page wrote"}
    p, packed = got
    path, written = archive(p, packed, _source(issue))
    publish_latest()
    s = summary(expand(p))
    return {"ok": True, "written": written, "path": str(path.relative_to(DATA_DIR.parent)), "date": p["date"],
            "read_at": p["read_at"], "summary": s}


def comment_text(res: dict) -> str:
    if not res.get("ok"):
        return f"Not saved: {res.get('why')}."
    s, t = res["summary"], res["summary"]["by_type"]
    kinds = ", ".join(f"{v} {k}" for k, v in sorted(t.items()))
    return (f"Saved for both: {s['lines']} lines ({kinds}) from {s['players']} players for {res['date']}, "
            f"read {res['read_at']}. Archived at `{res['path']}`." + ("" if res.get("written") else " (already archived)"))


def _gh():
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not tok:
        return None
    return {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "jg-desk-bot"}


def close_issue(num: int, text: str) -> None:
    import requests

    h = _gh()
    if not h:
        return
    requests.post(f"{API}/issues/{num}/comments", headers=h, json={"body": text}, timeout=30)
    requests.patch(f"{API}/issues/{num}", headers=h, json={"state": "closed"}, timeout=30)


def ingest() -> dict:
    """Archive every open board issue (the nightly catch-up for the board workflow). No token: no-op."""
    import requests

    h = _gh()
    if not h:
        return {"skipped": "no GITHUB_TOKEN"}
    seen, saved = 0, 0
    for page in range(1, 4):
        r = requests.get(f"{API}/issues", headers=h, params={"state": "open", "per_page": 100, "page": page}, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        for issue in batch:
            if issue.get("pull_request") or not TITLE_RE.match(issue.get("title") or ""):
                continue
            seen += 1
            res = save_issue(issue)
            saved += bool(res.get("ok") and res.get("written"))
            close_issue(issue["number"], comment_text(res))
        if len(batch) < 100:
            break
    return {"board_issues": seen, "archived": saved}


def from_event(path: str) -> dict:
    """The board workflow: the issue comes from the event file GitHub writes, so no API read is needed."""
    ev = json.loads(Path(path).read_text())
    issue = ev.get("issue") or {}
    res = save_issue(issue)
    res["issue"] = issue.get("number")
    return res
