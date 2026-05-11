"""Capture DraftKings betting splits (% Handle + % Bets) for UFL games.

Scrapes the DK Network page via FlareSolverr and saves a pre-kickoff
snapshot to a local JSON file. Run on the same machine as FlareSolverr.

Output: splits/{YYYY-MM-DD}/{away}@{home}.json

Usage:
    # Daemon — polls every 30 min, auto-captures 30-90 min before kickoff
    python capture_betting_splits.py --league ufl

    # One-shot
    python capture_betting_splits.py --league ufl --once

    # Force-capture ALL upcoming games right now (ignore time window)
    python capture_betting_splits.py --league ufl --once --force

    # Dry-run — parse and print without saving files
    python capture_betting_splits.py --league ufl --once --dry-run
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

FLARESOLVERR_URL = "http://localhost:8191/v1"
OUTPUT_DIR       = "splits"

EVENT_GROUPS = {
    "ufl": "212333",
}

CAPTURE_MIN      = 30   # minutes before kickoff
CAPTURE_MAX      = 90
POLL_INTERVAL    = 30 * 60  # daemon sleep (seconds)

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

def team_alias(name: str) -> str:
    return TEAM_ALIAS.get(name.strip(), name.strip()[:3].upper())


# ── FlareSolverr ──────────────────────────────────────────────────────────────

def fetch_rendered_html(url: str) -> str:
    log.info("Fetching via FlareSolverr: %s", url)
    r = requests.post(FLARESOLVERR_URL, json={
        "cmd": "request.get",
        "url": url,
        "session": "dk-splits",
        "session_ttl_minutes": 60,
        "maxTimeout": 120000,
    }, timeout=(10, 130))
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr error: {data.get('message')}")
    log.info("Got %d bytes (page HTTP %s)", len(data["solution"]["response"]), data["solution"].get("status"))
    return data["solution"]["response"]


# ── HTML parser ───────────────────────────────────────────────────────────────

def parse_splits_html(html: str) -> list[dict]:
    games  = []
    chunks = re.split(r'(?=<div[^>]*class="[^"]*\btb-se\b)', html)

    for chunk in chunks:
        if "tb-sodd" not in chunk:
            continue

        matchup_m = re.search(r'([A-Za-z.\s]+)\s+@\s+([A-Za-z.\s]+?)(?:\s+opens in a new tab|\s+<)', chunk)
        if not matchup_m:
            continue
        away_name = matchup_m.group(1).strip()
        home_name = matchup_m.group(2).strip()

        dt_m       = re.search(r'(\d{1,2}/\d{1,2}),?\s+(\d{1,2}:\d{2}(?:AM|PM))', chunk)
        game_dt    = f"{dt_m.group(1)}, {dt_m.group(2)}" if dt_m else None

        team_rows = []
        for sodd in re.split(r'(?=<div[^>]*class="[^"]*\btb-sodd\b)', chunk):
            if "tb-sodd" not in sodd:
                continue
            text  = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', sodd)).strip()
            odds_m = re.search(r'([+−\-]\d{2,4})', text)
            if not odds_m:
                continue
            team_name = text[:odds_m.start()].replace("opens in a new tab", "").strip()
            odds      = odds_m.group(1).replace("−", "-").replace("–", "-")
            pcts      = [int(m.group(1)) for m in re.finditer(r'(\d{1,3})%', text)]
            team_rows.append({
                "team":       team_name,
                "alias":      team_alias(team_name),
                "odds":       odds,
                "handle_pct": pcts[0] if pcts else None,
                "bets_pct":   pcts[1] if len(pcts) > 1 else None,
            })

        if len(team_rows) < 2:
            continue

        home_row = next((r for r in team_rows if home_name.split()[0].lower() in r["team"].lower()), team_rows[1])
        away_row = next((r for r in team_rows if r is not home_row), team_rows[0])

        games.append({
            "matchup":         f"{away_name} @ {home_name}",
            "away_team":       away_name,  "home_team":      home_name,
            "away_alias":      away_row["alias"], "home_alias": home_row["alias"],
            "game_datetime":   game_dt,
            "away_odds":       away_row["odds"],  "home_odds":  home_row["odds"],
            "away_handle_pct": away_row["handle_pct"],
            "home_handle_pct": home_row["handle_pct"],
            "away_bets_pct":   away_row["bets_pct"],
            "home_bets_pct":   home_row["bets_pct"],
        })

    return games


# ── Timing ────────────────────────────────────────────────────────────────────

def parse_kickoff(game_dt: str | None) -> datetime | None:
    if not game_dt:
        return None
    m = re.match(r'(\d+)/(\d+),?\s+(\d+):(\d+)(AM|PM)', game_dt)
    if not m:
        return None
    month, day, hr, mn, ampm = int(m[1]), int(m[2]), int(m[3]), int(m[4]), m[5]
    if ampm == "PM" and hr < 12: hr += 12
    if ampm == "AM" and hr == 12: hr  = 0
    # UFL kickoffs are Eastern Time (UTC-4 in summer)
    from datetime import timedelta
    et_dt = datetime(datetime.now().year, month, day, hr, mn, tzinfo=timezone.utc)
    return et_dt + timedelta(hours=4)  # ET → UTC

def mins_until(kickoff: datetime) -> float:
    return (kickoff - datetime.now(timezone.utc)).total_seconds() / 60


# ── File writer ───────────────────────────────────────────────────────────────

def save_splits(game: dict, kickoff: datetime | None, league: str) -> str:
    date_str  = kickoff.strftime("%Y-%m-%d") if kickoff else datetime.now().strftime("%Y-%m-%d")
    filename  = f"{game['away_alias']}@{game['home_alias']}.json"
    out_dir   = os.path.join(OUTPUT_DIR, date_str)
    os.makedirs(out_dir, exist_ok=True)
    filepath  = os.path.join(out_dir, filename)

    sharp_home = (
        game["home_handle_pct"] - game["home_bets_pct"]
        if game["home_handle_pct"] is not None and game["home_bets_pct"] is not None
        else None
    )

    record = {
        **game,
        "league":      league,
        "source":      "draftkings-network",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        # positive = sharp money on that side (handle % > public ticket %)
        "home_sharp_delta": sharp_home,
        "away_sharp_delta": -sharp_home if sharp_home is not None else None,
    }

    with open(filepath, "w") as f:
        json.dump(record, f, indent=2)

    return filepath


# ── Main ──────────────────────────────────────────────────────────────────────

def run_once(league: str, force: bool, dry_run: bool) -> list[str]:
    event_group = EVENT_GROUPS.get(league)
    if not event_group:
        raise ValueError(f"Unknown league: {league}")

    url   = f"https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/?tb_eg={event_group}&tb_edate=n7days&tb_emt=Moneyline"
    html  = fetch_rendered_html(url)
    games = parse_splits_html(html)
    log.info("Parsed %d games", len(games))

    saved = []
    for game in games:
        kickoff   = parse_kickoff(game["game_datetime"])
        mins      = mins_until(kickoff) if kickoff else None
        in_window = force or (mins is not None and CAPTURE_MIN <= mins <= CAPTURE_MAX)

        sharp_home = (
            game["home_handle_pct"] - game["home_bets_pct"]
            if game["home_handle_pct"] is not None and game["home_bets_pct"] is not None
            else None
        )
        sharp_label = (
            f"sharp={'HOME' if sharp_home > 0 else 'AWAY'} Δ{abs(sharp_home)}%"
            if sharp_home is not None else "sharp=n/a"
        )

        log.info(
            "%s | %s | handle H=%s%%/A=%s%%  bets H=%s%%/A=%s%%  %s",
            game["matchup"],
            f"{mins:.0f}min" if mins is not None else "?min",
            game["home_handle_pct"], game["away_handle_pct"],
            game["home_bets_pct"],   game["away_bets_pct"],
            sharp_label,
        )

        if not in_window:
            continue

        if dry_run:
            log.info("  [DRY RUN] would save %s@%s.json", game["away_alias"], game["home_alias"])
        else:
            path = save_splits(game, kickoff, league)
            log.info("  Saved → %s", path)
            saved.append(path)

    return saved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--league",  default="ufl", choices=list(EVENT_GROUPS))
    parser.add_argument("--once",    action="store_true")
    parser.add_argument("--force",   action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.once:
        saved = run_once(args.league, args.force, args.dry_run)
        log.info("Done — %d file(s) saved", len(saved))
        return

    log.info("Daemon: polling every %d min, capturing %d-%d min before kickoff",
             POLL_INTERVAL // 60, CAPTURE_MIN, CAPTURE_MAX)
    while True:
        try:
            saved = run_once(args.league, args.force, args.dry_run)
            if saved:
                log.info("Captured %d file(s)", len(saved))
        except Exception as e:
            log.error("Run failed: %s", e, exc_info=True)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
