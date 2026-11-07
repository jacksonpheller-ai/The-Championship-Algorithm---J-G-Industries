"""Line snapshots per game day, for closing-line value (CLV) and 'has the line moved past us'.

data/lines/<date>.json = [ {"at": iso-utc, "label": "opener|morning|pretip|afternoon|...",
                            "games": {game_id: {spread_home, spread_home_price, spread_away_price,
                                                total, over_price, under_price, ml_home, ml_away,
                                                model_margin, model_total}}}, ... ]
Every nightly run appends one snapshot. The earliest snapshot that contains a game is the
number we priced at; the latest is the best proxy for the close.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import DATA_DIR

LINES_DIR = DATA_DIR / "lines"
FIELDS = ("spread_home", "spread_home_price", "spread_away_price", "total", "over_price", "under_price",
          "ml_home", "ml_away")


def _path(date) -> Path:
    return LINES_DIR / f"{date}.json"


def load(date) -> list[dict]:
    p = _path(date)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except ValueError:
        return []


def record(date, label: str, games: list[dict], models: dict[str, dict] | None = None, at: str | None = None) -> Path:
    """Append a snapshot. `games` are slate entries with odds.close (pipeline shape);
    `models` maps game id -> {"margin": m_final, "total": t_final}."""
    snaps = load(date)
    entry = {"at": at or datetime.now(timezone.utc).isoformat(timespec="seconds"), "label": label, "games": {}}
    for g in games:
        close = ((g.get("odds") or {}).get("close") or {})
        rec = {k: close.get(k) for k in FIELDS}
        m = (models or {}).get(g["id"], {})
        rec["model_margin"] = m.get("margin")
        rec["model_total"] = m.get("total")
        entry["games"][str(g["id"])] = rec
    if not entry["games"]:
        return _path(date)
    snaps.append(entry)
    LINES_DIR.mkdir(parents=True, exist_ok=True)
    _path(date).write_text(json.dumps(snaps, separators=(",", ":")))
    return _path(date)


def first_snapshot(date, game_id: str) -> dict | None:
    for s in load(date):
        g = s["games"].get(str(game_id))
        if g and (g.get("total") is not None or g.get("spread_home") is not None):
            return {**g, "at": s["at"], "label": s["label"]}
    return None


def closing_line(date, game_id: str) -> dict | None:
    for s in reversed(load(date)):
        g = s["games"].get(str(game_id))
        if g and (g.get("total") is not None or g.get("spread_home") is not None):
            return {**g, "at": s["at"], "label": s["label"]}
    return None


def _line_for(bet: dict, snap: dict) -> float | None:
    k = bet.get("kind")
    if k == "total":
        return snap.get("total")
    if k == "spread":
        sh = snap.get("spread_home")
        if sh is None:
            return None
        return sh if bet.get("side") == "home" else -sh
    return None


def movement(date, bet: dict, tolerance: float = 0.5) -> dict | None:
    """Compare the line a bet was priced at with the latest snapshot.
    still_ok: the current number is within `tolerance` of the priced number or better for us."""
    first, last = first_snapshot(date, bet.get("game_id")), closing_line(date, bet.get("game_id"))
    if not first or not last:
        return None
    then, now = _line_for(bet, first), _line_for(bet, last)
    if then is None or now is None:
        return None
    side = bet.get("side")
    if bet.get("kind") == "total":
        better = (now <= then) if side == "over" else (now >= then)
        worse_by = (now - then) if side == "over" else (then - now)
    else:  # spread: a bigger number is better for the side taking the points
        better = now >= then
        worse_by = then - now
    return {"priced_at": first["at"], "line_then": then, "line_now": now, "still_ok": bool(better or worse_by <= tolerance),
            "moved_by": round(float(now - then), 2)}


def clv(bet: dict, closing: dict | None) -> float | None:
    """Closing line value in points in the bet's favour (positive = we beat the close)."""
    if not closing:
        return None
    k, side, line = bet.get("kind"), bet.get("side"), bet.get("line")
    if line is None:
        return None
    if k == "total":
        ct = closing.get("total")
        if ct is None:
            return None
        return float(ct - line) if side == "over" else float(line - ct)
    if k == "spread":
        sh = closing.get("spread_home")
        if sh is None:
            return None
        close_for_side = sh if side == "home" else -sh
        return float(line - close_for_side)  # we hold a bigger number than the close -> positive
    return None
