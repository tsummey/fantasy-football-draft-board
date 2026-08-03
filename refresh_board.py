"""
Refreshes the DATA array and header timestamp inside index.html using live,
crowd-sourced ADP (Average Draft Position) data from FantasyFootballCalculator's
free public API (no key/login required):

  https://fantasyfootballcalculator.com/api/v1/adp/ppr?teams=12&year=2026

Run manually any time:  python refresh_board.py
Or on a schedule (see the Windows Task Scheduler task "FantasyBoardRefresh").

What it does:
  1. Pulls current PPR ADP for every player being drafted in real mock/live
     drafts this week.
  2. Splits players into our board's rows (QB, RB, WR, TE, FLEX, K, DST) and
     keeps the same depth per row as the current board (QB 18, RB/WR 25,
     TE 18, FLEX/K 15, DST 12).
  3. Converts ADP rank within each row into the existing 1-10 rating scale.
  4. Reuses the hand-written "reasoning" text for any player already on the
     board (matched by name); brand-new players get an honest, sourced note
     citing their live ADP instead of invented commentary.
  5. Rewrites the DATA array and the header's "Updated ..." badge in
     index.html in place. Nothing else in the file (legend, strategies,
     drafted-marking feature, styling) is touched.
"""

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

import requests

HTML_PATH = Path(__file__).parent / "index.html"
API_URL = "https://fantasyfootballcalculator.com/api/v1/adp/ppr?teams=12&year=2026"

DEPTH = {"QB": 18, "RB": 25, "WR": 25, "TE": 18, "FLEX": 15, "K": 15, "DST": 12}
POSITION_MAP = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "PK": "K", "DEF": "DST"}

TEAM_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}


def fetch_adp():
    resp = requests.get(API_URL, timeout=20)
    resp.raise_for_status()
    return resp.json()


def load_existing_data(html_text):
    """Parse the current DATA array so we can reuse hand-written reasoning text."""
    m = re.search(r"const DATA = (\[.*?\]);", html_text, re.S)
    if not m:
        raise RuntimeError("Could not find DATA array in index.html")
    raw = m.group(1)
    # The array is JS object literal syntax (unquoted keys) - convert to JSON.
    raw = re.sub(r"(\w+):", r'"\1":', raw)
    raw = re.sub(r",(\s*[\]}])", r"\1", raw)  # trailing commas
    existing = json.loads(raw)
    lookup = {}
    for row in existing:
        lookup.setdefault(row["player"].lower(), row["reasoning"])
    return lookup


def rating_scale(rank_idx, total):
    """Map a 0-based rank within a row to a 1-10 draft-value rating."""
    if total <= 1:
        return 9.9
    hi, lo = 9.9, 3.5
    return round(hi - (hi - lo) * (rank_idx / (total - 1)), 1)


def build_rows(adp_players, reasoning_lookup):
    by_pos = {"QB": [], "RB": [], "WR": [], "TE": [], "K": [], "DST": []}
    for p in adp_players:
        our_pos = POSITION_MAP.get(p["position"])
        if not our_pos:
            continue
        by_pos[our_pos].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: p["adp"])

    rows = []

    def make_reasoning(p, is_flex=False):
        name = p["_display_name"]
        existing = reasoning_lookup.get(name.lower())
        if existing:
            return existing
        note = (
            f"Currently going around pick {p['adp_formatted']} (ADP {p['adp']:.1f}) "
            f"across {p['times_drafted']} live PPR drafts polled this week - "
            f"no prior scouting note on file yet, so treat this as a data-only placeholder."
        )
        return note

    for pos in ["QB", "RB", "WR", "TE"]:
        depth = DEPTH[pos]
        picks = by_pos[pos][:depth]
        for i, p in enumerate(picks):
            display_name = p["name"]
            p["_display_name"] = display_name
            rows.append({
                "position": pos,
                "depth": i + 1,
                "player": display_name,
                "team": p["team"],
                "rating": rating_scale(i, len(picks)),
                "reasoning": make_reasoning(p),
            })

    # FLEX: next-best RB/WR/TE beyond each position's own primary cutoff.
    flex_pool = []
    for pos in ["RB", "WR", "TE"]:
        flex_pool.extend(by_pos[pos][DEPTH[pos]:])
    flex_pool.sort(key=lambda p: p["adp"])
    flex_picks = flex_pool[: DEPTH["FLEX"]]
    for i, p in enumerate(flex_picks):
        display_name = p["name"]
        p["_display_name"] = display_name
        rows.append({
            "position": "FLEX",
            "depth": i + 1,
            "player": display_name,
            "team": p["team"],
            "rating": rating_scale(i, len(flex_picks)),
            "reasoning": make_reasoning(p, is_flex=True),
        })

    for pos in ["K", "DST"]:
        depth = DEPTH[pos]
        picks = by_pos[pos][:depth]
        for i, p in enumerate(picks):
            if pos == "DST":
                display_name = TEAM_NAMES.get(p["team"], p["name"].replace(" Defense", ""))
            else:
                display_name = p["name"]
            p["_display_name"] = display_name
            rows.append({
                "position": pos,
                "depth": i + 1,
                "player": display_name,
                "team": p["team"],
                "rating": rating_scale(i, len(picks)),
                "reasoning": make_reasoning(p),
            })

    return rows


def format_data_js(rows):
    lines = ["const DATA = ["]
    for r in rows:
        reasoning = r["reasoning"].replace("\\", "\\\\").replace('"', '\\"')
        player = r["player"].replace('"', '\\"')
        lines.append(
            f'  {{ position: "{r["position"]}", depth: {r["depth"]}, player: "{player}", '
            f'team: "{r["team"]}", rating: {r["rating"]},\n'
            f'    reasoning: "{reasoning}" }},'
        )
    lines.append("];")
    return "\n".join(lines)


def main():
    if not HTML_PATH.exists():
        print(f"ERROR: {HTML_PATH} not found", file=sys.stderr)
        sys.exit(1)

    html_text = HTML_PATH.read_text(encoding="utf-8")
    reasoning_lookup = load_existing_data(html_text)

    payload = fetch_adp()
    adp_players = payload["players"]
    meta = payload["meta"]

    rows = build_rows(adp_players, reasoning_lookup)
    new_data_js = format_data_js(rows)

    html_text = re.sub(
        r"const DATA = \[.*?\];", new_data_js, html_text, count=1, flags=re.S
    )

    today = date.today().strftime("%B %-d, %Y") if sys.platform != "win32" else date.today().strftime("%B %#d, %Y")
    badge_text = f"Updated {today} &middot; Live ADP"
    html_text = re.sub(
        r'<span class="badge">.*?</span>',
        f'<span class="badge">{badge_text}</span>',
        html_text,
        count=1,
    )

    HTML_PATH.write_text(html_text, encoding="utf-8")
    print(f"[{datetime.now().isoformat(timespec='seconds')}] Refreshed {len(rows)} rows "
          f"from {meta['total_drafts']} live drafts ({meta['start_date']} to {meta['end_date']}).")


if __name__ == "__main__":
    main()
