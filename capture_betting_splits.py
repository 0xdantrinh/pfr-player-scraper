"""Capture betting splits (% Handle + % Bets) from multiple sources.

Sources:
  draftkings  — DraftKings Network (default). Single-book. Supports: ufl, ufc, nfl, nba, mlb, worldcup26
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
import itertools
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

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
    "ufl":         "UFL",
    "ufc":         "UFC",
    "nfl":         "NFL",
    "nba":         "NBA",
    "mlb":         "MLB",
    "worldcup26":  "World Cup 2026",
    # Values must match the <option> values in the site's own sport dropdown
    # (name="tb_eg" on the betting-splits page) — DK switched this from numeric
    # event-group IDs to plain sport-name strings; the old numeric IDs now
    # silently fall back to unfiltered/generic content instead of erroring.
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
    slug = "-".join(reversed(parts)) if len(parts) > 1 else clean.lower()
    # Guard against path separators/colons leaking in from unexpected scrape
    # text (e.g. an un-stripped date/time) and breaking the saved filepath.
    return re.sub(r'[/\\:]', '-', slug)


def _payout_mult(odds_str: str | None) -> float | None:
    """Net payout multiplier from American odds string.
    +150 → 1.50 (bettor wins $1.50 per $1 wagered)
    -150 → 0.667 (bettor wins $0.667 per $1 wagered)
    """
    if not odds_str:
        return None
    try:
        v = int(str(odds_str).replace("+", "").replace("−", "-").replace("–", "-"))
        return v / 100 if v > 0 else 100 / abs(v)
    except (ValueError, ZeroDivisionError):
        return None


def calculate_vegas_liability(
    p1_handle_pct: int | None,
    p2_handle_pct: int | None,
    p1_odds: str | None,
    p2_odds: str | None,
) -> dict | None:
    """Vegas net P&L per $100 total handle if each side wins.

    Positive = Vegas profits, negative = Vegas is exposed (liability).
    """
    m1 = _payout_mult(p1_odds)
    m2 = _payout_mult(p2_odds)
    if None in (p1_handle_pct, p2_handle_pct, m1, m2):
        return None

    h1 = p1_handle_pct  # already out of 100
    h2 = p2_handle_pct

    p1_wins_net = round(h2 - h1 * m1, 2)
    p2_wins_net = round(h1 - h2 * m2, 2)

    if p1_wins_net < 0 and p2_wins_net >= 0:
        exposed_side = "p1"
        exposed_amount = round(abs(p1_wins_net), 2)
    elif p2_wins_net < 0 and p1_wins_net >= 0:
        exposed_side = "p2"
        exposed_amount = round(abs(p2_wins_net), 2)
    elif p1_wins_net < 0 and p2_wins_net < 0:
        exposed_side = "p1" if p1_wins_net < p2_wins_net else "p2"
        exposed_amount = round(max(abs(p1_wins_net), abs(p2_wins_net)), 2)
    else:
        exposed_side = "balanced"
        exposed_amount = 0.0

    # Break-even shift: how many handle % points the exposed side needs to move to neutralize.
    # Solve h_exposed = h_other × mult(other_odds) for neutral, then diff from current.
    # Negative = handle needs to drop; positive = handle needs to rise.
    if exposed_side == "p1":
        be = round(h2 * m2, 2)
        break_even_shift = round(be - h1, 2)
    elif exposed_side == "p2":
        be = round(h1 * m1, 2)
        break_even_shift = round(be - h2, 2)
    else:
        break_even_shift = 0.0

    return {
        "p1_wins_net": p1_wins_net,
        "p2_wins_net": p2_wins_net,
        "exposed_side": exposed_side,
        "exposed_amount": exposed_amount,
        "break_even_shift": break_even_shift,
    }


def compute_vegas_outcome_matrix(
    ml_liability: dict | None,
    spread_liability: dict | None,
    total_liability: dict | None,
) -> list[dict] | None:
    """Vegas net P&L for every combination of ML winner x spread cover x total result.

    Each leg's liability is evaluated independently elsewhere, but the book's
    real exposure for a given final outcome is the sum across all three legs —
    a game can net negative on the moneyline AND the spread and still be fine
    for the book if the total result covers both losses.

    Returns all 8 combinations (some may be unreachable for a given spread
    line/favorite, but identifying which ones requires odds parsing this
    keeps independent of — an unreachable cell's book_net is still a
    well-defined number and harmless to include).
    """
    if None in (ml_liability, spread_liability, total_liability):
        return None

    matrix = []
    for ml_side, spread_side, total_side in itertools.product(
        ("p1", "p2"), ("p1", "p2"), ("over", "under")
    ):
        book_net = round(
            ml_liability[f"{ml_side}_wins_net"]
            + spread_liability[f"{spread_side}_wins_net"]
            + total_liability[f"{total_side}_wins_net"],
            2,
        )
        matrix.append({
            "ml": ml_side,
            "spread": spread_side,
            "total": total_side,
            "book_net": book_net,
            "book_ok": book_net > 0,
        })
    return matrix


def check_mr_vegas_flag(p1_odds: str | None, p2_odds: str | None,
                        p1_bets_pct: int | None, p2_bets_pct: int | None) -> bool:
    """
    Mr Vegas Theory: Vegas leans towards the dog to win.
    Flag triggers if either participant has odds of +180 or better
    AND the public bet (bets %) on that same underdog is under 30%.
    """
    for odds_str, bets_pct in [(p1_odds, p1_bets_pct), (p2_odds, p2_bets_pct)]:
        if not odds_str:
            continue
        try:
            odds_val = int(odds_str.replace("+", "").replace("−", "-").replace("–", "-"))
        except (ValueError, AttributeError):
            continue
        # Positive odds of +180 or higher indicates underdog
        if odds_val >= 180 and bets_pct is not None and bets_pct < 30:
            return True
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
    """Parse all three bet-type tabs from one ScoresAndOdds consensus page.

    All tabs (moneyline, total, spread/runline/puckline) are present in the
    same rendered HTML. Match them by position — DraftKings and SAO both
    return games in the same order across tabs.

    Page text patterns per tab:
      Moneyline: TOR % OF BETS ATL  13% 87%  18% 82%  BEST AWAY +220  BEST HOME -250
      Total:     OVER (o7.5) % OF BETS UNDER (u7.5)  94% 6%  90% 10%  BEST OVER even  BEST UNDER -110
      Spread:    TOR (+1.5) % OF BETS ATL (-1.5)  12% 88%  13% 87%  BEST AWAY even  BEST HOME -110
    """
    soup = BeautifulSoup(html, "lxml")
    full_text = soup.get_text(" ", strip=True)

    ml_list     = _sao_parse_moneyline(full_text)
    total_list  = _sao_parse_total(full_text)
    spread_list = _sao_parse_spread(full_text)

    log.info("SAO parsed: %d moneyline, %d total, %d spread games",
             len(ml_list), len(total_list), len(spread_list))

    games = []
    for i, ml in enumerate(ml_list):
        game = dict(ml)
        if i < len(total_list):
            game["total"] = total_list[i]
        if i < len(spread_list):
            game["spread"] = spread_list[i]
            # Debug: log the dates being used
            ml_dt = game.get("game_datetime", "")
            spread_dt = spread_list[i].get("game_datetime")
            log.info(f"DEBUG: ML_DT={ml_dt}, SPREAD_DT={spread_dt}")
            # If moneyline date is late night (unreasonable game time), use spread date
            if "12:30AM" in ml_dt or "1:00AM" in ml_dt or "2:00AM" in ml_dt or "3:00AM" in ml_dt:
                if spread_dt:
                    game["game_datetime"] = spread_dt
        games.append(game)
    return games


def _sao_pcts_odds(text: str, start: int, is_total: bool = False) -> tuple:
    """Extract (bets1, bets2, handle1, handle2, odds1, odds2) from text after a % OF BETS match.

    Page layout (all tabs): bets bars THEN handle bars THEN '% of Money' label THEN odds.
    All percentages appear BEFORE '% of Money'.

    Total quirk: when one side is dominant (e.g. OVER 94%), the minority bets
    bar (6%) may not render as text. Pattern becomes:
      4 pcts → [bets1, bets2, handle1, handle2]  (both sides shown)
      3 pcts → [bets1, handle1, handle2]          (minority bets omitted; bets2=100-bets1)
      2 pcts → [handle1, handle2]                 (only handle shown)
    """
    segment = text[start:start + 500]

    # Find the '% of Money' / '% OF MONEY' label which comes AFTER all pcts
    money_m = re.search(r'%\s+[Oo]f\s+[Mm]oney|%\s+OF\s+MONEY', segment)
    if money_m:
        pct_block = segment[:money_m.start()]
    else:
        # No label found — take first 200 chars to avoid bleeding into next game
        pct_block = segment[:200]

    pcts = [int(m.group()) for m in re.finditer(r'\b(\d{1,3})(?=%)', pct_block)]

    if len(pcts) >= 4:
        bets1, bets2, handle1, handle2 = pcts[0], pcts[1], pcts[2], pcts[3]
    elif len(pcts) == 3 and is_total:
        # Minority bets pct not shown — derive it
        if pcts[0] + pcts[1] == 100:
            # First two are bets, third is one handle side
            bets1, bets2, handle1 = pcts[0], pcts[1], pcts[2]
            handle2 = 100 - handle1
        else:
            # First is dominant bets; second/third are handle
            bets1, handle1, handle2 = pcts[0], pcts[1], pcts[2]
            bets2 = 100 - bets1
    elif len(pcts) == 3:
        bets1, bets2, handle1, handle2 = pcts[0], pcts[1], pcts[2], None
    elif len(pcts) == 2:
        bets1, bets2, handle1, handle2 = pcts[0], pcts[1], None, None
    else:
        return None, None, None, None, None, None

    if is_total:
        o1_m = re.search(r'[Bb]est\s+[Oo]ver\s+[ou][\d.]*\s+([+\-]?\d+|even)', segment, re.IGNORECASE)
        o2_m = re.search(r'[Bb]est\s+[Uu]nder\s+[ou][\d.]*\s+([+\-]?\d+|even)', segment, re.IGNORECASE)
    else:
        o1_m = re.search(r'[Bb]est\s+[Aa]way\s+[Oo]dds\s+([+\-]?\d+|even)', segment, re.IGNORECASE)
        o2_m = re.search(r'[Bb]est\s+[Hh]ome\s+[Oo]dds\s+([+\-]?\d+|even)', segment, re.IGNORECASE)
    odds1 = o1_m.group(1) if o1_m else None
    odds2 = o2_m.group(1) if o2_m else None
    return bets1, bets2, handle1, handle2, odds1, odds2


def _sao_parse_moneyline(text: str) -> list[dict]:
    """Moneyline: plain 2-5 letter slug % OF BETS slug ..."""
    dt_pat  = re.compile(r'(\d{1,2}/\d{1,2})\s+(\d{1,2}:\d{2}(?:AM|PM))', re.IGNORECASE)
    # Must NOT be preceded by ( which would indicate a spread line
    pat = re.compile(
        r'(?<!\))\s([A-Z]{2,5})\s+%\s*[Oo][Ff]\s+[Bb][Ee][Tt][Ss]\s+([A-Z]{2,5})\s+(?!\()',
    )
    results = []
    for m in pat.finditer(text):
        away, home = m.group(1), m.group(2)
        b1, b2, h1, h2, o1, o2 = _sao_pcts_odds(text, m.end())
        if b1 is None:
            continue
        # Find the closest date by looking backward and taking the LAST match (closest to game)
        pre = text[max(0, m.start() - 3000):m.start()]
        dt_matches = list(dt_pat.finditer(pre))
        log.info(f"DEBUG_MONEYLINE: Found {len(dt_matches)} dates: {[dt.group(0) for dt in dt_matches]}")
        # Prefer reasonable game times (not 12:30 AM or other late night times)
        dt_m = None
        if dt_matches:
            # Try the last matches first, but skip unreasonable times
            for dt_match in reversed(dt_matches):
                time_str = dt_match.group(2).upper()
                if not ("12:30AM" in time_str or "1:00AM" in time_str or "2:00AM" in time_str):
                    dt_m = dt_match
                    break
            # If all matches were bad times, use the last one anyway
            if not dt_m:
                dt_m = dt_matches[-1]
        # Otherwise look forward
        if not dt_m:
            dt_m = dt_pat.search(text[m.end():min(len(text), m.end() + 500)])
        game_dt = f"{dt_m.group(1)} {dt_m.group(2)}" if dt_m else None
        sharp2 = (h2 - b2) if h2 is not None and b2 is not None else None
        results.append({
            "matchup": f"{away} @ {home}", "separator": "@",
            "participant1": away, "participant2": home,
            "participant1_slug": away, "participant2_slug": home,
            "game_datetime": game_dt,
            "participant1_odds": o1, "participant2_odds": o2,
            "participant1_handle_pct": h1, "participant2_handle_pct": h2,
            "participant1_bets_pct": b1, "participant2_bets_pct": b2,
            "p2_sharp_delta": sharp2, "p1_sharp_delta": -sharp2 if sharp2 is not None else None,
        })
    return results


def _sao_parse_total(text: str) -> list[dict]:
    """Total: OVER (oX.X) % OF BETS UNDER (uX.X) ..."""
    pat = re.compile(
        r'OVER\s*\(([oO][\d.]+)\)\s+%\s*[Oo][Ff]\s+[Bb][Ee][Tt][Ss]\s+UNDER\s*\(([uU][\d.]+)\)',
        re.IGNORECASE,
    )
    results = []
    for m in pat.finditer(text):
        over_line  = m.group(1)
        under_line = m.group(2)
        line_val   = re.search(r'[\d.]+', over_line)
        line       = line_val.group() if line_val else None
        b1, b2, h1, h2, o1, o2 = _sao_pcts_odds(text, m.end(), is_total=True)
        if b1 is None:
            continue
        sharp_over = (h1 - b1) if h1 is not None and b1 is not None else None
        results.append({
            "line": line,
            "over_bets_pct": b1, "under_bets_pct": b2,
            "over_handle_pct": h1, "under_handle_pct": h2,
            "over_odds": o1, "under_odds": o2,
            "over_sharp_delta": sharp_over,
        })
    return results


def _sao_parse_spread(text: str) -> list[dict]:
    """Spread/Runline/Puckline: ABBR (±X.X) % OF BETS ABBR (±X.X) ..."""
    pat = re.compile(
        r'([A-Z]{2,5})\s*\(([+\-][\d.]+)\)\s+%\s*[Oo][Ff]\s+[Bb][Ee][Tt][Ss]\s+'
        r'([A-Z]{2,5})\s*\(([+\-][\d.]+)\)',
    )
    results = []
    for m in pat.finditer(text):
        slug1, line1, slug2, line2 = m.group(1), m.group(2), m.group(3), m.group(4)
        b1, b2, h1, h2, o1, o2 = _sao_pcts_odds(text, m.end())
        if b1 is None:
            continue
        sharp2 = (h2 - b2) if h2 is not None and b2 is not None else None
        results.append({
            "participant1_line": line1, "participant2_line": line2,
            "participant1_bets_pct": b1, "participant2_bets_pct": b2,
            "participant1_handle_pct": h1, "participant2_handle_pct": h2,
            "participant1_odds": o1, "participant2_odds": o2,
            "p1_sharp_delta": -sharp2 if sharp2 is not None else None,
            "p2_sharp_delta": sharp2,
        })
    return results


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

        # Date/time is sometimes embedded in the title itself (e.g. UFC fighter
        # matchups) — strip it before splitting on "vs"/"@" so it isn't
        # swallowed into p2_name (which would otherwise leak a "/" into the
        # saved filename).
        dt_m = re.search(r'(\d{1,2}/\d{1,2}),?\s+(\d{1,2}:\d{2}(?:AM|PM))', title_text, re.IGNORECASE)
        game_dt = f"{dt_m.group(1)}, {dt_m.group(2)}" if dt_m else None
        matchup_text = title_text[:dt_m.start()].strip() if dt_m else title_text

        # Try "@ " separator (team sports) then "vs" (individual sports like UFC)
        sep_m = re.search(r'(.+?)\s+(@|vs\.?)\s+(.+?)(?:\s+opens|\s*$)', matchup_text, re.IGNORECASE)
        if not sep_m:
            continue
        p1_name   = sep_m.group(1).strip()
        separator = sep_m.group(2).strip()   # "@" or "vs"
        p2_name   = sep_m.group(3).strip()
        is_team_sport = separator == "@"

        # Fallback: use whole-block text if the title itself lacked a date/time
        if game_dt is None:
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

    ML_KEYS = [
        "participant1_handle_pct", "participant2_handle_pct",
        "participant1_bets_pct", "participant2_bets_pct",
        "p1_sharp_delta", "p2_sharp_delta",
        "participant1_odds", "participant2_odds",
    ]
    TOTAL_KEYS  = ["over_bets_pct", "under_bets_pct", "over_handle_pct",
                   "under_handle_pct", "over_odds", "under_odds", "over_sharp_delta"]
    SPREAD_KEYS = ["participant1_bets_pct", "participant2_bets_pct",
                   "participant1_handle_pct", "participant2_handle_pct",
                   "participant1_odds", "participant2_odds",
                   "p1_sharp_delta", "p2_sharp_delta"]

    def _build(source: dict, prev_source: dict | None, keys: list, prefix: str = "") -> None:
        for key in keys:
            hist_key = f"{prefix}{key}_history"
            val = source.get(key) if source else None
            if prev_source is None:
                history[hist_key] = [val] if val is not None else []
            else:
                prev_hist = (previous or {}).get(hist_key, [])
                history[hist_key] = prev_hist + [val]

    _build(current, previous, ML_KEYS)

    cur_total  = current.get("total")
    cur_spread = current.get("spread")
    _build(cur_total,  previous, TOTAL_KEYS,  prefix="total_")
    _build(cur_spread, previous, SPREAD_KEYS, prefix="spread_")

    return history


def check_mr_vegas_flag_with_history(current_p1_odds: str | None, current_p2_odds: str | None,
                                      current_p1_bets_pct: int | None, current_p2_bets_pct: int | None,
                                      previous: dict | None) -> bool:
    """
    Mr Vegas Theory: Vegas leans towards the dog to win.
    Flag triggers if either participant has/had odds of +180 or better
    AND the public bet on that underdog is under 30% at capture time.
    Once true, the flag persists (sticky) because it validates the theory.
    """
    # If previous record had the flag, keep it true (sticky)
    if previous and previous.get("mr_vegas_flag") is True:
        return True

    # Check current odds + current public betting split
    return check_mr_vegas_flag(current_p1_odds, current_p2_odds, current_p1_bets_pct, current_p2_bets_pct)


def check_ufc_mr_vegas_flag(p1_odds: str | None, p2_odds: str | None,
                             p1_bets_pct: int | None, p2_bets_pct: int | None,
                             p1_handle_pct: int | None, p2_handle_pct: int | None) -> bool:
    """
    UFC Mr Vegas Theory: a live underdog (+300 or better) that the public
    isn't backing — tickets under 50% or handle under 40% on that fighter —
    gets flagged.
    """
    for odds_str, bets_pct, handle_pct in (
        (p1_odds, p1_bets_pct, p1_handle_pct),
        (p2_odds, p2_bets_pct, p2_handle_pct),
    ):
        if not odds_str:
            continue
        try:
            odds_val = int(odds_str.replace("+", "").replace("−", "-").replace("–", "-"))
        except (ValueError, AttributeError):
            continue
        if odds_val < 300:
            continue
        if (bets_pct is not None and bets_pct < 50) or (handle_pct is not None and handle_pct < 40):
            return True
    return False


def check_ufc_mr_vegas_flag_with_history(league: str, current: dict, previous: dict | None) -> bool:
    """
    UFC Mr Vegas: sticky flag, only ever set for league == 'ufc'.
    """
    if league != "ufc":
        return False

    if previous and previous.get("ufc_mr_vegas_flag") is True:
        return True

    return check_ufc_mr_vegas_flag(
        current.get("participant1_odds"),
        current.get("participant2_odds"),
        current.get("participant1_bets_pct"),
        current.get("participant2_bets_pct"),
        current.get("participant1_handle_pct"),
        current.get("participant2_handle_pct"),
    )


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
        game.get("participant1_bets_pct"),
        game.get("participant2_bets_pct"),
        previous
    )

    # UFC-specific Mr Vegas flag — sticky, ufc only
    ufc_mr_vegas_flag = check_ufc_mr_vegas_flag_with_history(league, game, previous)

    # Build history arrays with the current capture time
    capture_times = previous.get("captured_at_history", []) if previous else []
    capture_times.append(capture_time)

    # Source label: "draftkings-network" or "betmgm_caesars"
    source_label = "betmgm_caesars" if source == "betmgm_caesars" else "draftkings-network"

    # Vegas liability — moneyline, spread, total
    ml_liability = calculate_vegas_liability(
        game.get("participant1_handle_pct"),
        game.get("participant2_handle_pct"),
        game.get("participant1_odds"),
        game.get("participant2_odds"),
    )
    spread = game.get("spread") or {}
    spread_liability = calculate_vegas_liability(
        spread.get("participant1_handle_pct"),
        spread.get("participant2_handle_pct"),
        spread.get("participant1_odds"),
        spread.get("participant2_odds"),
    )
    total = game.get("total") or {}
    _tl = calculate_vegas_liability(
        total.get("over_handle_pct"),
        total.get("under_handle_pct"),
        total.get("over_odds"),
        total.get("under_odds"),
    )
    if _tl:
        total_liability = {
            "over_wins_net":  _tl["p1_wins_net"],
            "under_wins_net": _tl["p2_wins_net"],
            "exposed_side":   "over" if _tl["exposed_side"] == "p1" else
                              "under" if _tl["exposed_side"] == "p2" else _tl["exposed_side"],
            "exposed_amount": _tl["exposed_amount"],
        }
    else:
        total_liability = None

    outcome_matrix = compute_vegas_outcome_matrix(ml_liability, spread_liability, total_liability)

    record = {
        **game,
        "league":      league,
        "source":      source_label,
        "captured_at": capture_time,
        "mr_vegas_flag": mr_vegas_flag,
        **({"ufc_mr_vegas_flag": ufc_mr_vegas_flag} if league == "ufc" else {}),
        **({"vegas_liability": ml_liability} if ml_liability else {}),
        **({"spread_vegas_liability": spread_liability} if spread_liability else {}),
        **({"total_vegas_liability": total_liability} if total_liability else {}),
        **({"vegas_outcome_matrix": outcome_matrix} if outcome_matrix else {}),
        **history,          # ML + total_ + spread_ history arrays
        "captured_at_history": capture_times,
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

def _parse_dk_row(sodd) -> dict | None:
    """Parse one tb-sodd element → {name, line, odds, handle_pct, bets_pct}."""
    slip = sodd.find(class_="tb-slipline")
    odd_el = sodd.find(class_="tb-odd-s")
    if not slip or not odd_el:
        return None
    name = slip.get_text(" ", strip=True)
    # Extract spread line from name if present (e.g. "ATL Braves -1.5" → line="-1.5")
    line_m = re.search(r'([+\-][\d.]+)\s*$', name)
    line = line_m.group(1) if line_m else None

    odds_text = odd_el.get_text(" ", strip=True).replace("opens in a new tab", "").strip()
    odds_m = re.search(r'([+−\-]\d{2,4})', odds_text)
    odds = odds_m.group(1).replace("−", "-") if odds_m else None

    pcts = [int(m.group(1)) for m in re.finditer(r'(\d{1,3})%', sodd.get_text())]
    return {
        "name":       name,
        "line":       line,
        "odds":       odds,
        "handle_pct": pcts[0] if len(pcts) > 0 else None,
        "bets_pct":   pcts[1] if len(pcts) > 1 else None,
    }


def parse_dk_all_html(html: str) -> list[dict]:
    """Parse DraftKings tb_emt=0 ('All') page — one fetch for Moneyline + Spread + Total.

    Each game block (tb-se) contains multiple tb-market-wrap sections labelled by
    tb-se-head: 'Moneyline', 'Run Line'/'Spread'/'Puckline', 'Total'.
    The tb-slipline element carries the participant name WITH the spread line
    (e.g. 'ATL Braves -1.5'), solving the null-line problem of the old 3-fetch approach.
    """
    soup = BeautifulSoup(html, "lxml")
    games = []

    for block in soup.find_all("div", class_="tb-se"):
        title_el = block.find(class_="tb-se-title")
        if not title_el:
            continue
        title_text = title_el.get_text(" ", strip=True)

        # Date/time is sometimes embedded in the title itself (e.g. UFC fighter
        # matchups: "Katie Volynets vs Tatjana Maria 7/12, 12:30PM") — strip it
        # before splitting on "vs"/"@" so it isn't swallowed into p2_name (which
        # would otherwise leak a "/" into the saved filename).
        dt_m = re.search(r'(\d{1,2}/\d{1,2}),?\s+(\d{1,2}:\d{2}(?:AM|PM))', title_text, re.IGNORECASE)
        game_dt = f"{dt_m.group(1)}, {dt_m.group(2)}" if dt_m else None
        matchup_text = title_text[:dt_m.start()].strip() if dt_m else title_text

        sep_m = re.search(r'(.+?)\s+(@|vs\.?)\s+(.+?)(?:\s+opens|\s*$)', matchup_text, re.IGNORECASE)
        if not sep_m:
            continue
        p1_name   = sep_m.group(1).strip()
        separator = sep_m.group(2).strip()
        p2_name   = sep_m.group(3).strip()

        ml_data = spread_data = total_data = None

        # Structure: tb-market-wrap > (tb-se-head, tb-sm)+ pairs.
        # tb-sm wraps the participant rows for one bet type; tb-se-head is its preceding sibling.
        for sm in block.find_all(class_="tb-sm"):
            head = sm.find_previous_sibling(class_="tb-se-head")
            if not head:
                continue
            head_text = head.get_text(" ", strip=True).lower()
            rows = [r for r in (_parse_dk_row(s) for s in sm.find_all("div", class_="tb-sodd")) if r]
            if len(rows) < 2:
                continue

            if "moneyline" in head_text:
                # Rows ordered: home first on DK — identify by name match
                r2 = next((r for r in rows if p2_name.split()[0].lower() in r["name"].lower()), rows[1])
                r1 = next((r for r in rows if r is not r2), rows[0])
                sharp2 = (r2["handle_pct"] - r2["bets_pct"]
                          if r2["handle_pct"] is not None and r2["bets_pct"] is not None else None)
                ml_data = {
                    "participant1_odds":       r1["odds"],
                    "participant2_odds":       r2["odds"],
                    "participant1_handle_pct": r1["handle_pct"],
                    "participant2_handle_pct": r2["handle_pct"],
                    "participant1_bets_pct":   r1["bets_pct"],
                    "participant2_bets_pct":   r2["bets_pct"],
                    "p2_sharp_delta": sharp2,
                    "p1_sharp_delta": -sharp2 if sharp2 is not None else None,
                }

            elif any(k in head_text for k in ("run line", "spread", "puckline", "runline")):
                r2 = next((r for r in rows if p2_name.split()[0].lower() in r["name"].lower()), rows[1])
                r1 = next((r for r in rows if r is not r2), rows[0])
                sharp2 = (r2["handle_pct"] - r2["bets_pct"]
                          if r2["handle_pct"] is not None and r2["bets_pct"] is not None else None)
                spread_data = {
                    "participant1_line":       r1["line"],
                    "participant2_line":       r2["line"],
                    "participant1_odds":       r1["odds"],
                    "participant2_odds":       r2["odds"],
                    "participant1_handle_pct": r1["handle_pct"],
                    "participant2_handle_pct": r2["handle_pct"],
                    "participant1_bets_pct":   r1["bets_pct"],
                    "participant2_bets_pct":   r2["bets_pct"],
                    "p1_sharp_delta": -sharp2 if sharp2 is not None else None,
                    "p2_sharp_delta": sharp2,
                }

            elif "total" in head_text:
                over_r  = next((r for r in rows if "over"  in r["name"].lower()), rows[0])
                under_r = next((r for r in rows if "under" in r["name"].lower()), rows[1])
                line_m  = re.search(r'[\d.]+', over_r["name"])
                sharp_over = (over_r["handle_pct"] - over_r["bets_pct"]
                              if over_r["handle_pct"] is not None and over_r["bets_pct"] is not None else None)
                total_data = {
                    "line":             line_m.group() if line_m else None,
                    "over_bets_pct":    over_r["bets_pct"],
                    "under_bets_pct":   under_r["bets_pct"],
                    "over_handle_pct":  over_r["handle_pct"],
                    "under_handle_pct": under_r["handle_pct"],
                    "over_odds":        over_r["odds"],
                    "under_odds":       under_r["odds"],
                    "over_sharp_delta": sharp_over,
                }

        if ml_data is None:
            continue

        matchup_str = f"{p1_name} @ {p2_name}" if separator == "@" else f"{p1_name} vs {p2_name}"
        game = {
            "matchup":            matchup_str,
            "separator":          separator,
            "participant1":       p1_name,
            "participant2":       p2_name,
            "participant1_slug":  participant_slug(p1_name),
            "participant2_slug":  participant_slug(p2_name),
            "game_datetime":      game_dt,
            **ml_data,
        }
        if total_data:
            game["total"] = total_data
        if spread_data:
            game["spread"] = spread_data
        games.append(game)

    return games


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
        # Will be overridden per-game with correct game date
        out_subdir = os.path.join(OUTPUT_DIR, "betmgm_caesars", league, "{game_date}")
        s3_prefix  = f"betmgm_caesars/{league}/{{game_date}}"
        log.info("Parsed %d games from ScoresAndOdds (BetMGM+Caesars)", len(games))
    else:
        event_group = EVENT_GROUPS.get(league)
        if not event_group:
            raise ValueError(
                f"League '{league}' not supported for draftkings. "
                f"Choose from: {', '.join(EVENT_GROUPS)}"
            )
        url   = (f"https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
                 f"?tb_eg={quote_plus(event_group)}&tb_edate=n7days&tb_emt=0")
        html  = fetch_rendered_html(url, league)
        games = parse_dk_all_html(html)
        # Will be overridden per-game with correct game date
        out_subdir = os.path.join(OUTPUT_DIR, "draftkings", league, "{game_date}")
        s3_prefix  = f"draftkings/{league}/{{game_date}}"
        log.info("Parsed %d games from DraftKings Network (All types)", len(games))

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

        # Substitute game date into templates
        game_date = parse_game_date(game["game_datetime"])
        game_subdir = out_subdir.format(game_date=game_date) if out_subdir else None
        game_s3_prefix = s3_prefix.format(game_date=game_date) if s3_prefix else None

        path = save_game(game, league, dry_run,
                         source=source, out_subdir=game_subdir, s3_prefix=game_s3_prefix)
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
