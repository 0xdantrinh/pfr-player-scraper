"""Import a footballdb.com player profile into DynamoDB.

Why this exists (Sprint 10):
  PFR is missing many fringe / multi-league career players entirely (e.g.
  Jalan McClendon — no PROFILE in our DB at all). footballdb.com aggregates
  FBS / NFL / XFL / UFL career stats on one page, plus the player's
  birthdate (which PFR often hides). This script:
    1. Calls the footballdb scraper at /Users/danieltrinh/Documents/Projects/pfr-player-scraper
       (or any path passed via --scraper-path)
    2. Transforms the parsed JSON into our standard DDB items
    3. Writes them under PLAYER#fdb:{footballdb_id} so they don't collide
       with PFR pfrId items

Item layout produced (per player):
  PLAYER#fdb:{id}        / PROFILE                — name, position, height, weight,
                                                    birthdate, college, currentTeam
  PLAYER#fdb:{id}        / CAREER                 — pro career totals (NFL+XFL+UFL summed)
  PLAYER#fdb:{id}        / SEASON#COLLEGE#{year}  — one item per college (FBS) season
  PLAYER#fdb:{id}        / SEASON#{year}          — one item per pro (NFL/XFL/UFL) season

The `fdb:` prefix matches the existing `cfb:` prefix convention used by
transform-pfr-data when it falls back to college-only profiles. The
cross-league-link utility (utils/cross-league-link.mjs) already does
fuzzy name matching against any PROFILE item, so these will be
discoverable automatically.

CLI:
    source scrapers/.venv/bin/activate
    python scrapers/footballdb/import_footballdb.py \\
        --url "https://www.footballdb.com/players/jalan-mcclendon-mccleja02"
    # add --dry-run to preview without writing
    # add --table=<name> to override (default: gruden-player-data)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3

REGION         = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
DEFAULT_TABLE  = os.environ.get("PLAYER_DATA_TABLE", "gruden-player-data")
DEFAULT_SCRAPER = os.environ.get(
    "FOOTBALLDB_SCRAPER_PATH",
    "/Users/danieltrinh/Documents/Projects/pfr-player-scraper/footballdb_scraper.py",
)


# ── Dynamic import of the scraper (sibling repo) ──────────────────────────────

def _load_scraper(scraper_path: str):
    p = Path(scraper_path)
    if not p.exists():
        raise FileNotFoundError(f"footballdb scraper not found at {scraper_path}")
    spec = importlib.util.spec_from_file_location("footballdb_scraper", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ── Stat normalization ────────────────────────────────────────────────────────

def _to_decimal(v) -> Decimal | None:
    """Float/int/str → Decimal for DDB. None / '' → None."""
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _strip_nulls(d: dict) -> dict:
    """Drop keys whose values are None — DDB handles missing keys natively."""
    return {k: v for k, v in d.items() if v is not None}


# ── Transformations ───────────────────────────────────────────────────────────

def build_profile_item(player_id: str, scraped: dict,
                         team_abbr: str | None = None,
                         season_year: int | None = None) -> dict:
    """Build the PROFILE DDB item from scraped JSON.

    When team_abbr is given (e.g. "STL"), set the GSI keys so getTeamPlayers()
    can find this player. footballdb returns full team names like "St. Louis
    BattleHawks" — we can't reliably map those to aliases, so we require the
    caller to pass the alias explicitly when they want roster discoverability.
    """
    info = scraped.get("player_info", {}) or {}
    year = season_year or int(time.strftime("%Y", time.gmtime()))
    item = {
        "PK":             f"PLAYER#{player_id}",
        "SK":             "PROFILE",
        "entityType":     "PROFILE",
        "footballdbId":   scraped.get("footballdb_id"),
        "name":           info.get("name"),
        "position":       info.get("position"),
        "positionGroup":  "quarterback" if info.get("position") == "QB" else None,
        "heightInches":   info.get("height_in"),
        "weightLbs":      info.get("weight_lb"),
        "birthdate":      info.get("birthdate"),
        "college":        info.get("college"),
        "currentTeamAbbr": team_abbr or info.get("current_team"),
        "currentTeamFull": info.get("current_team") if team_abbr else None,
        "currentLeague":  info.get("current_league"),
        "currentSeason":  year if team_abbr else None,
        "jerseyNumber":   info.get("jersey"),
        "source":         "footballdb",
        "sourceUrl":      scraped.get("source_url"),
        "updatedAt":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if team_abbr:
        item["GSI1PK"] = f"TEAM#{team_abbr}#S#{year}"
        item["GSI1SK"] = f"PLAYER#{player_id}"
    return _strip_nulls(item)


def _passing_dict(row: dict) -> dict:
    """Map a footballdb passing row → our standard `passing` shape."""
    return _strip_nulls({
        "attempts":       _to_decimal(row.get("attempts")),
        "completions":    _to_decimal(row.get("completions")),
        "cmpPct":         _to_decimal(row.get("completion_pct")),
        "yards":          _to_decimal(row.get("yards")),
        "yardsPerAttempt":_to_decimal(row.get("yards_per_attempt")),
        "yardsPerGame":   _to_decimal(row.get("yards_per_game")),
        "touchdowns":     _to_decimal(row.get("touchdowns")),
        "interceptions":  _to_decimal(row.get("interceptions")),
        "longestPass":    _to_decimal(row.get("longest_pass")),
        "sacks":          _to_decimal(row.get("sacks_taken")),
        "sackYards":      _to_decimal(row.get("sack_yards_lost")),
        "rating":         _to_decimal(row.get("rating")),
    })


def _rushing_dict(row: dict) -> dict:
    return _strip_nulls({
        "attempts":       _to_decimal(row.get("attempts")),
        "yards":          _to_decimal(row.get("yards")),
        "yardsPerCarry":  _to_decimal(row.get("yards_per_attempt")),
        "yardsPerGame":   _to_decimal(row.get("yards_per_game")),
        "longestRush":    _to_decimal(row.get("longest_rush")),
        "touchdowns":     _to_decimal(row.get("touchdowns")),
        "fumbles":        _to_decimal(row.get("fumbles")),
        "fumblesLost":    _to_decimal(row.get("fumbles_lost")),
    })


def _conference_for_school(school: str | None, league: str | None) -> str | None:
    """Conservative conference inference. footballdb uses school name like
    'North Carolina St', 'Baylor'. We only set conference for well-known
    P5 schools here — the rest stays null and gets median-imputed downstream.
    Better than guessing wrong (would corrupt the conference-tier feature).
    """
    if league != "FBS" or not school:
        return None
    s = school.strip().lower()
    P5_BY_SCHOOL = {
        # SEC
        "alabama": "SEC", "arkansas": "SEC", "auburn": "SEC", "florida": "SEC",
        "georgia": "SEC", "kentucky": "SEC", "lsu": "SEC", "ole miss": "SEC",
        "mississippi": "SEC", "missouri": "SEC", "south carolina": "SEC",
        "tennessee": "SEC", "texas a&m": "SEC", "vanderbilt": "SEC",
        "texas": "SEC", "oklahoma": "SEC",
        # Big Ten
        "illinois": "Big Ten", "indiana": "Big Ten", "iowa": "Big Ten",
        "maryland": "Big Ten", "michigan": "Big Ten", "michigan st": "Big Ten",
        "minnesota": "Big Ten", "nebraska": "Big Ten", "northwestern": "Big Ten",
        "ohio st": "Big Ten", "ohio state": "Big Ten", "penn st": "Big Ten",
        "penn state": "Big Ten", "purdue": "Big Ten", "rutgers": "Big Ten",
        "wisconsin": "Big Ten", "ucla": "Big Ten", "usc": "Big Ten",
        "oregon": "Big Ten", "washington": "Big Ten",
        # ACC
        "boston college": "ACC", "clemson": "ACC", "duke": "ACC",
        "florida st": "ACC", "florida state": "ACC", "georgia tech": "ACC",
        "louisville": "ACC", "miami": "ACC", "miami (fl)": "ACC",
        "north carolina": "ACC", "north carolina st": "ACC", "nc state": "ACC",
        "pittsburgh": "ACC", "pitt": "ACC", "syracuse": "ACC",
        "virginia": "ACC", "virginia tech": "ACC", "wake forest": "ACC",
        "stanford": "ACC", "california": "ACC", "smu": "ACC",
        # Big 12
        "baylor": "Big 12", "iowa st": "Big 12", "iowa state": "Big 12",
        "kansas": "Big 12", "kansas st": "Big 12", "kansas state": "Big 12",
        "oklahoma st": "Big 12", "oklahoma state": "Big 12", "tcu": "Big 12",
        "texas tech": "Big 12", "west virginia": "Big 12", "byu": "Big 12",
        "cincinnati": "Big 12", "houston": "Big 12", "ucf": "Big 12",
        "utah": "Big 12", "arizona": "Big 12", "arizona st": "Big 12",
        "arizona state": "Big 12", "colorado": "Big 12",
        # Notre Dame
        "notre dame": "Independent",
    }
    return P5_BY_SCHOOL.get(s)


def build_season_items(player_id: str, scraped: dict) -> list[dict]:
    """Produce one DDB SEASON#... item per (year, league) row.

    College years (league == 'FBS') become SEASON#COLLEGE#{year} items so
    they integrate with the existing CFB feature pipeline. Pro years
    (NFL/XFL/UFL) become SEASON#{year} with `league` field set so we can
    distinguish UFL/XFL stints in the future.
    """
    items: list[dict] = []
    pk = f"PLAYER#{player_id}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Index rushing rows by (year, team) so we can merge with passing
    rushing_by_key = {}
    for r in scraped.get("stats", {}).get("rushing", []):
        key = (str(r.get("year_id")), r.get("league"), r.get("team"))
        rushing_by_key[key] = r

    for p in scraped.get("stats", {}).get("passing", []):
        year = p.get("year_id")
        league = (p.get("league") or "").upper()
        team   = p.get("team")
        if not year or not year.isdigit():
            continue
        year_int = int(year)

        # Look up matching rushing row
        rush = rushing_by_key.get((str(year), p.get("league"), team), {})

        if league == "FBS":
            sk = f"SEASON#COLLEGE#{year_int}"
            entity = "SEASON_COLLEGE"
        else:
            sk = f"SEASON#{year_int}"
            entity = "SEASON"

        item = _strip_nulls({
            "PK":           pk,
            "SK":           sk,
            "entityType":   entity,
            "footballdbId": scraped.get("footballdb_id"),
            "year":         _to_decimal(year_int),
            "league":       league or None,
            "school":       team if league == "FBS" else None,
            "conference":   _conference_for_school(team, league),
            "teamAbbr":     team if league != "FBS" else None,
            "gamesPlayed":  _to_decimal(p.get("games")),
            "gamesStarted": _to_decimal(p.get("games_started")),
            "passing":      _passing_dict(p) or None,
            "rushing":      _rushing_dict(rush) or None,
            "source":       "footballdb",
            "updatedAt":    now,
        })
        items.append(item)

    return items


def _sum_passing(seasons: list[dict]) -> dict | None:
    """Sum passing stats across a list of season rows. Returns None if no attempts."""
    pa = sum(s.get("attempts") or 0 for s in seasons)
    if not pa:
        return None
    pc  = sum(s.get("completions") or 0 for s in seasons)
    py  = sum(s.get("yards") or 0 for s in seasons)
    pt  = sum(s.get("touchdowns") or 0 for s in seasons)
    pi  = sum(s.get("interceptions") or 0 for s in seasons)
    g   = sum(s.get("games") or 0 for s in seasons)
    gs  = sum(s.get("games_started") or 0 for s in seasons)
    return _strip_nulls({
        "attempts":      _to_decimal(pa),
        "completions":   _to_decimal(pc),
        "yards":         _to_decimal(py),
        "touchdowns":    _to_decimal(pt),
        "interceptions": _to_decimal(pi),
        "cmpPct":        _to_decimal(round(pc / pa * 100, 1)) if pa else None,
        "yardsPerAttempt": _to_decimal(round(py / pa, 2)) if pa else None,
        "gamesPlayed":   _to_decimal(g),
        "gamesStarted":  _to_decimal(gs),
    })


def build_career_item(player_id: str, scraped: dict) -> dict:
    """Aggregate pro career totals split by league.

    Produces per-league career buckets (nflCareer, uflCareer, xflCareer, etc.)
    plus overall pro totals. Awards from the page are stored here too.
    """
    seasons = scraped.get("stats", {}).get("passing", [])
    COLLEGE_LEAGUES = {"FBS", "D2", "D3", "JUCO", "NAIA"}
    pro = [s for s in seasons if (s.get("league") or "").upper() not in COLLEGE_LEAGUES]
    if not pro:
        return {}

    # Per-league buckets
    by_league: dict[str, list] = {}
    for s in pro:
        lg = (s.get("league") or "OTHER").upper()
        by_league.setdefault(lg, []).append(s)

    # Overall pro totals (games only from seasons that have real game counts)
    total_g  = sum(s.get("games") or 0 for s in pro)
    total_gs = sum(s.get("games_started") or 0 for s in pro)
    total_sp = len({s.get("year_id") for s in pro})

    # Build per-league career sub-objects (key = e.g. "uflCareer", "nflCareer")
    league_careers: dict[str, Any] = {}
    for lg, rows in by_league.items():
        key = f"{lg.lower()}Career"
        totals = _sum_passing(rows)
        if totals:
            league_careers[key] = totals

    # Awards
    raw_awards = scraped.get("awards", [])
    awards_list = [
        _strip_nulls({
            "award":  a.get("award"),
            "year":   _to_decimal(a.get("year")),
            "detail": a.get("detail"),
        })
        for a in raw_awards if a.get("award")
    ] if raw_awards else None

    return _strip_nulls({
        "PK":            f"PLAYER#{player_id}",
        "SK":            "CAREER",
        "entityType":    "CAREER",
        "footballdbId":  scraped.get("footballdb_id"),
        "seasonsPlayed": _to_decimal(total_sp),
        "gamesPlayed":   _to_decimal(total_g),
        "gamesStarted":  _to_decimal(total_gs),
        # Overall passing (all pro leagues combined)
        "passing":       _sum_passing(pro),
        # Per-league career buckets
        **league_careers,
        # Awards & honors
        "awards":        awards_list,
        "source":        "footballdb",
        "updatedAt":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


# ── Main ──────────────────────────────────────────────────────────────────────

def import_player(url: str, table_name: str = DEFAULT_TABLE,
                   scraper_path: str = DEFAULT_SCRAPER, dry_run: bool = False,
                   pfr_id_override: str | None = None,
                   team_abbr: str | None = None,
                   season_year: int | None = None) -> dict:
    """Scrape one footballdb player URL and (optionally) write to DDB.

    When pfr_id_override is provided, items are written under PLAYER#{pfrId}
    (overlaying any existing PFR record — useful when PFR has a stub PROFILE
    with null fields, e.g. Jalan McClendon's McclJa00). Without an override,
    we use a `fdb:{footballdb_id}` PK to avoid colliding with PFR records.

    Returns a summary dict for CLI output.
    """
    sc = _load_scraper(scraper_path)
    print(f"Fetching {url} ...")
    html = sc.fetch_page(url)
    scraped = sc.parse_page(html, url)

    fdb_id = scraped.get("footballdb_id")
    if not fdb_id:
        raise ValueError("Could not extract footballdb_id from URL")
    player_id = pfr_id_override if pfr_id_override else f"fdb:{fdb_id}"

    profile = build_profile_item(player_id, scraped, team_abbr=team_abbr, season_year=season_year)
    seasons = build_season_items(player_id, scraped)
    career  = build_career_item(player_id, scraped)

    items = [profile] + seasons + ([career] if career else [])

    print(f"\nName: {profile.get('name')}  ({profile.get('position')})")
    print(f"PK: {profile['PK']}")
    print(f"Items to write: {len(items)} ({sum(1 for i in seasons if i['SK'].startswith('SEASON#COLLEGE#'))} college + {sum(1 for i in seasons if not i['SK'].startswith('SEASON#COLLEGE#'))} pro + 1 PROFILE + {1 if career else 0} CAREER)")

    if dry_run:
        print("\n[dry-run] Sample item (PROFILE):")
        # JSON-render Decimals as strings for readability
        print(json.dumps({k: (str(v) if isinstance(v, Decimal) else v) for k, v in profile.items()}, indent=2))
        return {"player_id": player_id, "items": len(items), "dry_run": True}

    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(table_name)

    # MERGE strategy for PROFILE/CAREER: fill in missing/null fields only —
    # never clobber an existing non-null value. This is critical because:
    #   - currentTeamAbbr=CLB on the stub is the GSI partition key (footballdb
    #     would replace with the full team name "Columbus Aviators", breaking
    #     getTeamPlayers('CLB', 2026))
    #   - cfbSlug, cfbUrl, collegeCareer added by the CFB transform must persist
    # Fields we DO want to refresh always: updatedAt, sourceUrl, footballdbId.
    REFRESH_ALWAYS = {"updatedAt", "sourceUrl", "footballdbId", "source"}
    written = 0
    for item in items:
        sk = item["SK"]
        if sk in ("PROFILE", "CAREER"):
            existing = table.get_item(Key={"PK": item["PK"], "SK": sk}).get("Item") or {}
            merged = dict(existing)
            for k, v in item.items():
                if v is None:
                    continue
                if k in REFRESH_ALWAYS or existing.get(k) in (None, ""):
                    merged[k] = v
            table.put_item(Item=merged)
        else:
            table.put_item(Item=item)
        written += 1
    print(f"✓ Wrote {written} items to {table_name} (PROFILE/CAREER merged with existing)")
    return {"player_id": player_id, "items": written}


def import_from_file(path: str, table_name: str, scraper_path: str, dry_run: bool,
                      sleep_between: float = 1.5) -> None:
    """Bulk-import every player listed in a JSON file.

    File format — a JSON array of objects:
        [
          { "url": "https://www.footballdb.com/players/jalan-mcclendon-mccleja02",
            "pfrId": "McclJa00" },
          { "url": "https://www.footballdb.com/players/...",
            "pfrId": null },
          ...
        ]

    `pfrId` is optional — when present the importer overlays the existing PFR
    record (typical case: PFR stub PROFILE that needs name/bio backfill).
    When absent, items are written under PLAYER#fdb:{footballdb_id}.

    `sleep_between` adds a polite delay between requests (default 1.5s) so
    we don't hammer footballdb.
    """
    with open(path) as f:
        entries = json.load(f)
    if not isinstance(entries, list):
        raise ValueError(f"Expected a JSON array in {path}, got {type(entries).__name__}")

    print(f"Bulk import from {path}: {len(entries)} player(s)")
    succeeded = failed = 0
    for i, entry in enumerate(entries, start=1):
        url = entry.get("url")
        if not url:
            print(f"  [{i}/{len(entries)}] SKIP — no url field")
            continue
        pfr_id = entry.get("pfrId") or entry.get("pfr_id")
        try:
            print(f"\n[{i}/{len(entries)}] {url}  (pfrId={pfr_id or '—'})")
            import_player(url, table_name, scraper_path, dry_run, pfr_id_override=pfr_id)
            succeeded += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ FAILED: {e}")
            failed += 1
        if i < len(entries) and sleep_between > 0:
            time.sleep(sleep_between)

    print(f"\nDone. {succeeded} succeeded, {failed} failed.")


def main() -> None:
    p = argparse.ArgumentParser(description="Import footballdb.com player(s) into DynamoDB")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="Single player URL, e.g. https://www.footballdb.com/players/jalan-mcclendon-mccleja02")
    g.add_argument("--input-file", help="JSON file with [{url, pfrId?}, ...] for bulk import")
    p.add_argument("--table", default=DEFAULT_TABLE)
    p.add_argument("--scraper-path", default=DEFAULT_SCRAPER)
    p.add_argument("--pfr-id", default=None,
                   help="Optional pfrId to overlay (single-player mode only). For bulk, set pfrId per-entry in the input file.")
    p.add_argument("--team-abbr", default=None,
                   help="Current team alias (e.g. STL) — required for the player to show up in getTeamPlayers() roster lookups. footballdb only gives full team names which can't be reliably mapped to aliases.")
    p.add_argument("--season-year", type=int, default=None,
                   help="Current season year for GSI key (defaults to current year).")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--sleep", type=float, default=1.5, help="Seconds between requests in bulk mode (default 1.5)")
    args = p.parse_args()

    if args.input_file:
        import_from_file(args.input_file, args.table, args.scraper_path, args.dry_run, sleep_between=args.sleep)
    else:
        import_player(args.url, args.table, args.scraper_path, args.dry_run,
                      pfr_id_override=args.pfr_id,
                      team_abbr=args.team_abbr,
                      season_year=args.season_year)


if __name__ == "__main__":
    main()
