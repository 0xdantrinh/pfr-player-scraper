"""Capture DraftKings betting splits (% Handle + % Bets) for UFL games.

Scrapes the DK Network page via FlareSolverr and saves all current games
to local JSON files. Just run it whenever you want a fresh snapshot.

Output: splits/{YYYY-MM-DD}/{away}@{home}.json  (overwrites on re-run)

Usage:
    python capture_betting_splits.py --league ufl           # save all games
    python capture_betting_splits.py --league ufl --dry-run # print only
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

FLARESOLVERR_URL = "http://localhost:8191/v1"
OUTPUT_DIR       = "splits"

EVENT_GROUPS = {
    "ufl":  "212333",
    "ufc":  "9034",
    "nfl":  "88808",
    "nba":  "42648",
    "mlb":  "84240",
    # Add more as needed — find the tb_eg value from the URL when on that sport's page
}

# UFL team abbreviations — other sports use slugified participant names
TEAM_ALIAS = {
    "Dallas Renegades":      "DAL",
    "Orlando Storm":         "ORL",
    "Houston Gamblers":      "HOU",
    "St. Louis Battlehawks": "STL",
    "Birmingham Stallions":  "BHM",
    "D.C. Defenders":        "DC",
    "DC Defenders":          "DC",
    "Columbus Aviators":     "CLB",
    "Louisville Kings":      "LOU",
    "Arlington Renegades":   "DAL",
}

def participant_slug(name: str) -> str:
    """Short identifier for a team or fighter — alias if known, else slug."""
    clean = name.strip()
    if clean in TEAM_ALIAS:
        return TEAM_ALIAS[clean]
    # For fighters/individuals: "Daniel Barez" → "barez-daniel"
    parts = clean.lower().split()
    return "-".join(reversed(parts)) if len(parts) > 1 else clean.lower()


# ── FlareSolverr ──────────────────────────────────────────────────────────────

def fetch_rendered_html(url: str, league: str = "default") -> str:
    log.info("Fetching via FlareSolverr: %s", url)
    # All DK Network pages share the same Cloudflare domain — one session handles all leagues
    r = requests.post(FLARESOLVERR_URL, json={
        "cmd": "request.get",
        "url": url,
        "session": "dk-splits",
        "session_ttl_minutes": 60,
        "maxTimeout": 120000,
    }, timeout=(10, 150))
    # FlareSolverr returns 500 with JSON on challenge failure — read body before raising
    try:
        data = r.json()
    except Exception:
        r.raise_for_status()
        raise
    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr error: {data.get('message')}")
    html = data["solution"]["response"]
    log.info("Got %d bytes (HTTP %s)", len(html), data["solution"].get("status"))
    return html


# ── HTML parser (BeautifulSoup — robust against whitespace) ───────────────────

def parse_splits_html(html: str) -> list[dict]:
    soup  = BeautifulSoup(html, "lxml")
    games = []

    for block in soup.find_all("div", class_="tb-se"):
        # Matchup title — supports both "Away @ Home" (teams) and "Fighter1 vs Fighter2"
        title_el = block.find("h5") or block.find(class_="tb-se-title")
        if not title_el:
            continue
        title_text = title_el.get_text(" ", strip=True)

        # Try "@ " separator (team sports) then "vs" (individual sports like UFC)
        sep_m = re.search(r'(.+?)\s+(@|vs\.?)\s+(.+?)(?:\s+opens|\s*$)', title_text, re.IGNORECASE)
        if not sep_m:
            continue
        p1_name   = sep_m.group(1).strip()
        separator = sep_m.group(2).strip()   # "@" or "vs"
        p2_name   = sep_m.group(3).strip()
        is_team_sport = separator == "@"

        # Date/time
        block_text = block.get_text(" ", strip=True)
        dt_m       = re.search(r'(\d{1,2}/\d{1,2}),?\s+(\d{1,2}:\d{2}(?:AM|PM))', block_text)
        game_dt    = f"{dt_m.group(1)}, {dt_m.group(2)}" if dt_m else None

        # Each tb-sodd div = one participant row
        rows = []
        for sodd in block.find_all("div", class_="tb-sodd"):
            text   = sodd.get_text(" ", strip=True)
            odds_m = re.search(r'([+−\-]\d{2,4})', text)
            if not odds_m:
                continue
            name  = text[:odds_m.start()].replace("opens in a new tab", "").strip()
            odds  = odds_m.group(1).replace("−", "-").replace("–", "-")
            pcts  = [int(m.group(1)) for m in re.finditer(r'(\d{1,3})%', text)]
            rows.append({
                "name":       name,
                "slug":       participant_slug(name),
                "odds":       odds,
                "handle_pct": pcts[0] if pcts else None,
                "bets_pct":   pcts[1] if len(pcts) > 1 else None,
            })

        if len(rows) < 2:
            continue

        # For team sports: p1=away, p2=home. For "vs" sports: p1/p2 order as listed.
        if is_team_sport:
            p2_row = next((r for r in rows if p2_name.split()[0].lower() in r["name"].lower()), rows[1])
            p1_row = next((r for r in rows if r is not p2_row), rows[0])
        else:
            p1_row, p2_row = rows[0], rows[1]

        sharp_delta = (
            p2_row["handle_pct"] - p2_row["bets_pct"]
            if p2_row["handle_pct"] is not None and p2_row["bets_pct"] is not None
            else None
        )

        matchup_str = f"{p1_name} @ {p2_name}" if is_team_sport else f"{p1_name} vs {p2_name}"
        games.append({
            "matchup":          matchup_str,
            "separator":        separator,       # "@" or "vs"
            "participant1":     p1_name,
            "participant2":     p2_name,
            "participant1_slug": p1_row["slug"],
            "participant2_slug": p2_row["slug"],
            "game_datetime":    game_dt,
            "participant1_odds":       p1_row["odds"],
            "participant2_odds":       p2_row["odds"],
            "participant1_handle_pct": p1_row["handle_pct"],
            "participant2_handle_pct": p2_row["handle_pct"],
            "participant1_bets_pct":   p1_row["bets_pct"],
            "participant2_bets_pct":   p2_row["bets_pct"],
            # Sharp delta on participant2 (home team or fighter2)
            "p2_sharp_delta": sharp_delta,
            "p1_sharp_delta": -sharp_delta if sharp_delta is not None else None,
        })

    return games


# ── File writer ───────────────────────────────────────────────────────────────

def parse_game_date(game_dt: str | None) -> str:
    """Return YYYY-MM-DD for the game, defaulting to today."""
    if not game_dt:
        return datetime.now().strftime("%Y-%m-%d")
    m = re.match(r'(\d+)/(\d+)', game_dt)
    if not m:
        return datetime.now().strftime("%Y-%m-%d")
    year  = datetime.now().year
    month = int(m.group(1))
    day   = int(m.group(2))
    return f"{year}-{month:02d}-{day:02d}"


def save_game(game: dict, league: str, dry_run: bool) -> str:
    date_str = parse_game_date(game["game_datetime"])
    sep      = "-vs-" if game["separator"].lower().startswith("v") else "@"
    filename = f"{game['participant1_slug']}{sep}{game['participant2_slug']}.json"
    out_dir  = os.path.join(OUTPUT_DIR, league, date_str)
    filepath = os.path.join(out_dir, filename)

    record = {
        **game,
        "league":      league,
        "source":      "draftkings-network",
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }

    if dry_run:
        log.info("[DRY RUN] %s → %s", game["matchup"], filepath)
    else:
        os.makedirs(out_dir, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(record, f, indent=2)

    return filepath


# ── Main ──────────────────────────────────────────────────────────────────────

def run_once(league: str, dry_run: bool) -> list[str]:
    event_group = EVENT_GROUPS.get(league)
    if not event_group:
        raise ValueError(f"Unknown league: {league}. Add to EVENT_GROUPS.")

    url   = (f"https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
             f"?tb_eg={event_group}&tb_edate=n7days&tb_emt=Moneyline")
    html  = fetch_rendered_html(url, league)
    games = parse_splits_html(html)
    log.info("Parsed %d games", len(games))

    saved = []
    for game in games:
        sharp = game["p2_sharp_delta"]
        sharp_label = (
            f"sharp={'P2' if sharp > 0 else 'P1'} Δ{abs(sharp)}%"
            if sharp is not None else "sharp=n/a"
        )
        log.info(
            "%-42s  handle P1=%3s%% P2=%3s%%  bets P1=%3s%% P2=%3s%%  %s",
            game["matchup"],
            game["participant1_handle_pct"], game["participant2_handle_pct"],
            game["participant1_bets_pct"],   game["participant2_bets_pct"],
            sharp_label,
        )
        path = save_game(game, league, dry_run)
        saved.append(path)

    return saved


def main():
    parser = argparse.ArgumentParser(description="Capture DK betting splits")
    parser.add_argument("--league",  default="ufl", choices=list(EVENT_GROUPS),
                        help="Sport/league key. Known: " + ", ".join(f"{k}={v}" for k,v in EVENT_GROUPS.items()))
    parser.add_argument("--dry-run", action="store_true", help="Print without saving")
    args = parser.parse_args()

    saved = run_once(args.league, args.dry_run)
    log.info("Done — %d file(s) %s", len(saved), "would be saved" if args.dry_run else "saved")
    if saved:
        for p in saved:
            print(f"  {p}")


if __name__ == "__main__":
    main()
