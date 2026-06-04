"""Capture betting splits (% Handle + % Bets) from multiple sources.

Sources:
  draftkings  — DraftKings Network (default). Single-book. Supports: ufl, ufc, nfl, nba, mlb
  betmgm_caesars — ScoresAndOdds.com consensus (BetMGM + Caesars combined).
                   Supports: nfl, nba, mlb, ncaaf, ncaab, nhl, wnba

Output:
  DraftKings:    splits/{league}/{YYYY-MM-DD}/{away}@{home}.json
  BetMGM+Caesar: splits/betmgm_caesars/{league}/{YYYY-MM-DD}/{away}@{home}.json

S3:
  DraftKings:    {SPLITS_S3_BUCKET}/{league}/{date}/{file}.json
  BetMGM+Caesar: {SPLITS_S3_BUCKET}/betmgm_caesars/{league}/{date}/{file}.json

Usage:
    python capture_betting_splits.py --league ufl                            # DK, save
    python capture_betting_splits.py --league mlb --source betmgm_caesars   # SAO, save
    python capture_betting_splits.py --league nfl --source betmgm_caesars --dry-run
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta

import boto3
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Import plotting function
try:
    from plot_splits import plot_game
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

FLARESOLVERR_URL = "http://localhost:8191/v1"
OUTPUT_DIR       = "splits"
AWS_REGION       = os.getenv("AWS_REGION", "us-east-1")
SPLITS_S3_BUCKET = os.getenv("SPLITS_S3_BUCKET")

if not SPLITS_S3_BUCKET:
    raise ValueError("SPLITS_S3_BUCKET environment variable is required")

s3_client = boto3.client("s3", region_name=AWS_REGION)

EVENT_GROUPS = {
    "ufl":  "212333",
    "ufc":  "9034",
    "nfl":  "88808",
    "nba":  "42648",
    "mlb":  "84240",
    # Add more as needed — find the tb_eg value from the URL when on that sport's page
}

# ScoresAndOdds.com (BetMGM + Caesars aggregated data)
SAO_BASE_URL = "https://www.scoresandodds.com/{sport}/consensus-picks"
SAO_SPORTS = {
    "nfl":   "nfl",
    "nba":   "nba",
    "mlb":   "mlb",
    "ncaaf": "ncaaf",
    "ncaab": "ncaab",
    "nhl":   "nhl",
    "wnba":  "wnba",
}

ALL_LEAGUES = sorted(set(list(EVENT_GROUPS) + list(SAO_SPORTS)))

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


def check_mr_vegas_flag(p1_odds: str | None, p2_odds: str | None) -> bool:
    """
    Mr Vegas Theory: Vegas leans towards the dog to win.
    Flag triggers if either participant has odds of +180 or better.
    """
    try:
        # Parse odds strings like "+180", "-135"
        for odds_str in [p1_odds, p2_odds]:
            if not odds_str:
                continue
            odds_val = int(odds_str.replace("+", "").replace("−", "-").replace("–", "-"))
            # Positive odds of +180 or higher indicates underdog
            if odds_val >= 180:
                return True
    except (ValueError, AttributeError):
        pass
    return False


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


# ── ScoresAndOdds.com fetch + parse (BetMGM + Caesars combined) ──────────────

def fetch_sao_html(sport: str) -> str:
    url = SAO_BASE_URL.format(sport=SAO_SPORTS[sport])
    log.info("Fetching ScoresAndOdds via FlareSolverr: %s", url)
    r = requests.post(FLARESOLVERR_URL, json={
        "cmd": "request.get",
        "url": url,
        "session": "sao-splits",
        "session_ttl_minutes": 60,
        "maxTimeout": 120000,
    }, timeout=(10, 150))
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


def parse_sao_games(html: str) -> list[dict]:
    """Parse ScoresAndOdds consensus picks page (MONEYLINE tab default).

    The page renders abbreviations in the splits section:
      TOR  % OF BETS  ATL   13%  87%   18%  82%  % OF MONEY
      BEST AWAY ODDS +220   BEST HOME ODDS -250

    Falls back to text regex when CSS structure is unknown.
    """
    soup = BeautifulSoup(html, "lxml")
    games = []

    # ── Strategy 1: CSS-selector based (inspect rendered HTML to refine) ──────
    # Try common scoresandodds game card classes
    card_selectors = [
        "div.consensus-game", "div.game-card", "div.splits-game",
        "div[class*='game']", "div[class*='consensus']",
    ]
    cards = []
    for sel in card_selectors:
        cards = soup.select(sel)
        if cards:
            log.info("Found %d game cards via selector: %s", len(cards), sel)
            break

    if cards:
        for card in cards:
            text = card.get_text(" ", strip=True)
            game = _parse_sao_card_text(text)
            if game:
                games.append(game)
        if games:
            return games

    # ── Strategy 2: Full-page text regex fallback ─────────────────────────────
    log.info("CSS selectors found no cards — falling back to full-page text regex")
    full_text = soup.get_text(" ", strip=True)
    # Log a snippet to help diagnose HTML structure in dry-run mode
    log.debug("Page text snippet (first 500 chars): %s", full_text[:500])

    # Scan for blocks: ABBR % OF BETS ABBR bets1% bets2% handle1% handle2%
    pattern = re.compile(
        r'([A-Z]{2,5})\s+%\s*[Oo][Ff]\s+[Bb][Ee][Tt][Ss]\s+([A-Z]{2,5})'
        r'\s+(\d{1,3})%\s+(\d{1,3})%'   # bets pcts
        r'\s+(\d{1,3})%\s+(\d{1,3})%',  # handle pcts (after % OF MONEY label)
        re.DOTALL,
    )
    odds_pattern = re.compile(
        r'BEST\s+AWAY\s+ODDS\s+([+\-]\d+).*?BEST\s+HOME\s+ODDS\s+([+\-]\d+)',
        re.DOTALL | re.IGNORECASE,
    )
    dt_pattern = re.compile(r'(\d{1,2}/\d{1,2})\s+(\d{1,2}:\d{2}(?:AM|PM))', re.IGNORECASE)

    pos = 0
    for m in pattern.finditer(full_text):
        away_slug = m.group(1)
        home_slug = m.group(2)
        bets_away, bets_home     = int(m.group(3)), int(m.group(4))
        handle_away, handle_home = int(m.group(5)), int(m.group(6))

        # Look for odds in the ~300 chars after the pct match
        segment = full_text[m.start():m.end() + 300]
        odds_m = odds_pattern.search(segment)
        odds_away = odds_m.group(1) if odds_m else None
        odds_home = odds_m.group(2) if odds_m else None

        # Look for game datetime in the 200 chars before the match
        pre = full_text[max(0, m.start() - 200):m.start()]
        dt_m = dt_pattern.search(pre)
        game_dt = f"{dt_m.group(1)} {dt_m.group(2)}" if dt_m else None

        sharp_home = handle_home - bets_home
        games.append({
            "matchup":            f"{away_slug} @ {home_slug}",
            "separator":          "@",
            "participant1":       away_slug,
            "participant2":       home_slug,
            "participant1_slug":  away_slug,
            "participant2_slug":  home_slug,
            "game_datetime":      game_dt,
            "participant1_odds":        odds_away,
            "participant2_odds":        odds_home,
            "participant1_handle_pct":  handle_away,
            "participant2_handle_pct":  handle_home,
            "participant1_bets_pct":    bets_away,
            "participant2_bets_pct":    bets_home,
            "p2_sharp_delta": sharp_home,
            "p1_sharp_delta": -sharp_home,
        })
        pos = m.end()

    return games


def _parse_sao_card_text(text: str) -> dict | None:
    """Parse a single game card's text into a splits dict."""
    m = re.search(
        r'([A-Z]{2,5})\s+%\s*[Oo][Ff]\s+[Bb][Ee][Tt][Ss]\s+([A-Z]{2,5})'
        r'\s+(\d{1,3})%\s+(\d{1,3})%\s+(\d{1,3})%\s+(\d{1,3})%',
        text,
    )
    if not m:
        return None
    away_slug, home_slug = m.group(1), m.group(2)
    bets_away, bets_home     = int(m.group(3)), int(m.group(4))
    handle_away, handle_home = int(m.group(5)), int(m.group(6))

    odds_m = re.search(
        r'BEST\s+AWAY\s+ODDS\s+([+\-]\d+).*?BEST\s+HOME\s+ODDS\s+([+\-]\d+)',
        text, re.IGNORECASE | re.DOTALL,
    )
    odds_away = odds_m.group(1) if odds_m else None
    odds_home = odds_m.group(2) if odds_m else None

    dt_m = re.search(r'(\d{1,2}/\d{1,2})\s+(\d{1,2}:\d{2}(?:AM|PM))', text, re.IGNORECASE)
    game_dt = f"{dt_m.group(1)} {dt_m.group(2)}" if dt_m else None

    sharp_home = handle_home - bets_home
    return {
        "matchup":            f"{away_slug} @ {home_slug}",
        "separator":          "@",
        "participant1":       away_slug,
        "participant2":       home_slug,
        "participant1_slug":  away_slug,
        "participant2_slug":  home_slug,
        "game_datetime":      game_dt,
        "participant1_odds":        odds_away,
        "participant2_odds":        odds_home,
        "participant1_handle_pct":  handle_away,
        "participant2_handle_pct":  handle_home,
        "participant1_bets_pct":    bets_away,
        "participant2_bets_pct":    bets_home,
        "p2_sharp_delta": sharp_home,
        "p1_sharp_delta": -sharp_home,
    }


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


def load_previous_game(filepath: str) -> dict | None:
    """Load the previous version of a game file if it exists."""
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def calculate_deltas(current: dict, previous: dict | None) -> dict:
    """Deltas are now redundant with history arrays. Keeping this for backwards compatibility if needed."""
    return {}


def build_history_arrays(current: dict, previous: dict | None) -> dict:
    """Build historical arrays for tracking market movement over time."""
    history = {}

    # If no previous record, start fresh arrays with current values
    if previous is None:
        tracked_keys = [
            "participant1_handle_pct", "participant2_handle_pct",
            "participant1_bets_pct", "participant2_bets_pct",
            "p1_sharp_delta", "p2_sharp_delta",
            "participant1_odds", "participant2_odds",
        ]
        for key in tracked_keys:
            val = current.get(key)
            history[f"{key}_history"] = [val] if val is not None else []
        return history

    # Append current values to previous history arrays
    tracked_keys = [
        "participant1_handle_pct", "participant2_handle_pct",
        "participant1_bets_pct", "participant2_bets_pct",
        "p1_sharp_delta", "p2_sharp_delta",
        "participant1_odds", "participant2_odds",
    ]
    for key in tracked_keys:
        hist_key = f"{key}_history"
        prev_hist = previous.get(hist_key, [])
        # Preserve previous history and append current value
        history[hist_key] = prev_hist + [current.get(key)]

    return history


def check_mr_vegas_flag_with_history(current_p1_odds: str | None, current_p2_odds: str | None, 
                                      previous: dict | None) -> bool:
    """
    Mr Vegas Theory: Vegas leans towards the dog to win.
    Flag triggers if either participant has/had odds of +180 or better.
    Once true, the flag persists (sticky) because it validates the theory.
    """
    # If previous record had the flag, keep it true (sticky)
    if previous and previous.get("mr_vegas_flag") is True:
        return True
    
    # Check current odds
    return check_mr_vegas_flag(current_p1_odds, current_p2_odds)


def upload_to_s3(filepath: str, s3_prefix: str, filename: str) -> None:
    """Upload a splits file to S3 under the given prefix (e.g. 'ufl/2026-06-04')."""
    s3_key = f"{s3_prefix}/{filename}"
    try:
        with open(filepath, "rb") as f:
            s3_client.put_object(
                Bucket=SPLITS_S3_BUCKET,
                Key=s3_key,
                Body=f.read(),
                ContentType="application/json"
            )
        log.info("Uploaded to s3://%s/%s", SPLITS_S3_BUCKET, s3_key)
    except Exception as e:
        log.error("Failed to upload to S3: %s", e)
        raise


def save_game(game: dict, league: str, dry_run: bool,
              source: str = "draftkings", out_subdir: str | None = None,
              s3_prefix: str | None = None) -> str:
    date_str = parse_game_date(game["game_datetime"])
    sep      = "-vs-" if game["separator"].lower().startswith("v") else "@"
    filename = f"{game['participant1_slug']}{sep}{game['participant2_slug']}.json"

    # Default paths for DK; SAO caller passes explicit subdir/prefix
    if out_subdir is None:
        out_subdir = os.path.join(OUTPUT_DIR, league, date_str)
    if s3_prefix is None:
        s3_prefix = f"{league}/{date_str}"

    filepath = os.path.join(out_subdir, filename)

    # Generate capture timestamp
    capture_time = datetime.now(timezone.utc).isoformat()

    # Load previous version to build history arrays and check mr_vegas history
    previous = load_previous_game(filepath)
    history = build_history_arrays(game, previous)

    # Check mr_vegas flag with history — sticky flag
    mr_vegas_flag = check_mr_vegas_flag_with_history(
        game["participant1_odds"],
        game["participant2_odds"],
        previous
    )

    # Build history arrays with the current capture time
    capture_times = previous.get("captured_at_history", []) if previous else []
    capture_times.append(capture_time)

    # Source label: "draftkings-network" or "betmgm_caesars"
    source_label = "betmgm_caesars" if source == "betmgm_caesars" else "draftkings-network"

    record = {
        **game,
        "league":      league,
        "source":      source_label,
        "captured_at": capture_time,
        "mr_vegas_flag": mr_vegas_flag,
        **history,  # Historical arrays for plotting
        "captured_at_history": capture_times,  # Explicit capture times
    }

    if dry_run:
        log.info("[DRY RUN] %s → %s", game["matchup"], filepath)
    else:
        os.makedirs(out_subdir, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(record, f, indent=2)

        # Upload to S3
        upload_to_s3(filepath, s3_prefix, filename)

        # Auto-generate and save plot
        if HAS_MATPLOTLIB:
            plot_dir = os.path.join("plots", source, league, date_str)
            plot_filename = filename.replace(".json", ".png")
            plot_path = os.path.join(plot_dir, plot_filename)
            try:
                plot_game(record, show=False, save_path=plot_path)
                log.info("Plot saved: %s", plot_path)
            except Exception as e:
                log.warning("Failed to generate plot: %s", e)

    return filepath


# ── Main ──────────────────────────────────────────────────────────────────────

def run_once(league: str, dry_run: bool, source: str = "draftkings") -> list[str]:
    today = datetime.now().strftime("%Y-%m-%d")

    if source == "betmgm_caesars":
        if league not in SAO_SPORTS:
            raise ValueError(
                f"League '{league}' not supported for betmgm_caesars. "
                f"Choose from: {', '.join(SAO_SPORTS)}"
            )
        html  = fetch_sao_html(league)
        games = parse_sao_games(html)
        out_subdir = os.path.join(OUTPUT_DIR, "betmgm_caesars", league, today)
        s3_prefix  = f"betmgm_caesars/{league}/{today}"
        log.info("Parsed %d games from ScoresAndOdds (BetMGM+Caesars)", len(games))
    else:
        event_group = EVENT_GROUPS.get(league)
        if not event_group:
            raise ValueError(
                f"League '{league}' not supported for draftkings. "
                f"Choose from: {', '.join(EVENT_GROUPS)}"
            )
        url   = (f"https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
                 f"?tb_eg={event_group}&tb_edate=n7days&tb_emt=Moneyline")
        html  = fetch_rendered_html(url, league)
        games = parse_splits_html(html)
        out_subdir = None  # save_game uses default
        s3_prefix  = None  # save_game uses default
        log.info("Parsed %d games from DraftKings Network", len(games))

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
        path = save_game(game, league, dry_run,
                         source=source, out_subdir=out_subdir, s3_prefix=s3_prefix)
        saved.append(path)

    return saved


def main():
    parser = argparse.ArgumentParser(
        description="Capture betting splits from DraftKings or BetMGM+Caesars (ScoresAndOdds)"
    )
    parser.add_argument(
        "--league", default="ufl", choices=ALL_LEAGUES,
        help="Sport/league. DK supports: " + ", ".join(EVENT_GROUPS) +
             "  |  BetMGM+Caesars supports: " + ", ".join(SAO_SPORTS),
    )
    parser.add_argument(
        "--source", default="draftkings",
        choices=["draftkings", "betmgm_caesars"],
        help="Data source. 'draftkings' = DK Network; 'betmgm_caesars' = ScoresAndOdds.com",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print without saving or uploading")
    args = parser.parse_args()

    saved = run_once(args.league, args.dry_run, source=args.source)
    log.info("Done — %d file(s) %s", len(saved), "would be saved" if args.dry_run else "saved")
    if saved:
        for p in saved:
            print(f"  {p}")


if __name__ == "__main__":
    main()
