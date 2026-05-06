"""footballdb.com player scraper — sibling to scraper.py / cfb_scraper.py.

Why this exists:
  PFR is missing data for fringe / multi-league career players (XFL, UFL,
  practice-squad-only NFL stints). footballdb.com aggregates pro stats across
  FBS / NFL / XFL / UFL on a single page and includes birthdate (which PFR
  often hides). Example: Jalan McClendon — no PFR profile in our DB, but
  footballdb has 13 seasons spanning NC State, Baylor, Washington (NFL),
  LA + Vegas (XFL), DC + Houston + Columbus (UFL).

Architecture (mirrors scraper.py / cfb_scraper.py):
  - fetch_page(url) → returns HTML
  - parse_page(html, url) → returns dict with player_info + stats
  - worker.py picks up SQS messages and routes URLs containing
    "footballdb.com" to this module's parse_page()

Difference from PFR scraper: footballdb is NOT behind Cloudflare, so we
use plain `requests.get()` instead of FlareSolverr. Falls back to FlareSolverr
if a future block forces it (commented hook left in place).
"""

import json
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# ── HTTP fetch ────────────────────────────────────────────────────────────────

def fetch_page(url: str, timeout: int = 30) -> str:
    """Plain GET — footballdb has no Cloudflare challenge as of 2026.
    If a future block forces it, swap in FlareSolverr (see scraper.py)."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    last_exc = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"footballdb fetch failed for {url}: {last_exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

_HEIGHT_RE = re.compile(r"(\d+)\s*-\s*(\d+)")
_WEIGHT_RE = re.compile(r"(\d+)")
_BIRTH_RE  = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")


def _parse_height_inches(s: str) -> int | None:
    m = _HEIGHT_RE.search(s or "")
    if not m:
        return None
    return int(m.group(1)) * 12 + int(m.group(2))


def _parse_birthdate(s: str) -> str | None:
    """Return YYYY-MM-DD if a M/D/YYYY substring is present, else None."""
    m = _BIRTH_RE.search(s or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _num(v):
    """Coerce numeric strings to float/int. '--' and '' → None."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "--", "—"):
        return None
    try:
        return int(s) if s.lstrip("-").isdigit() else float(s)
    except ValueError:
        return None


# ── Bio block ─────────────────────────────────────────────────────────────────

def _extract_player_info(soup: BeautifulSoup) -> dict:
    """Pull bio fields from the meta block under <h1>.

    Format (one paragraph, single-line):
        '<Team> (<League>), #<Jersey> Position: QB Height: 6-0 Weight: 219
         Birthdate: October 21, 1995 10/21/1995 College: Baylor ...'
    """
    info: dict = {}

    h1 = soup.find("h1")
    if h1:
        info["name"] = h1.get_text(strip=True)

    # Find the bio paragraph: it's the first text block containing "Position:"
    bio_text = ""
    for el in soup.find_all(["p", "div", "section"]):
        text = el.get_text(" ", strip=True)
        if "Position:" in text and "College:" in text and len(text) < 1500:
            bio_text = text
            break
    if not bio_text:
        return info

    # Position
    m = re.search(r"Position:\s*([A-Z/]+)", bio_text)
    if m:
        info["position"] = m.group(1)

    # Height (e.g. "6-0")
    m = re.search(r"Height:\s*([\d-]+)", bio_text)
    if m:
        h = _parse_height_inches(m.group(1))
        if h:
            info["height_in"] = h

    # Weight ("Weight: 219")
    m = re.search(r"Weight:\s*(\d+)", bio_text)
    if m:
        info["weight_lb"] = int(m.group(1))

    # Birthdate ("Birthdate: October 21, 1995 10/21/1995")
    bd = _parse_birthdate(bio_text)
    if bd:
        info["birthdate"] = bd

    # College ("College: Baylor")
    m = re.search(r"College:\s*([A-Za-z .'&-]+?)(?:\s+Stats|\s+Game|\s+\d|$)", bio_text)
    if m:
        info["college"] = m.group(1).strip()

    # Team + jersey ("Columbus Aviators (UFL), #8")
    m = re.search(r"^(.+?)\s+\(([A-Z]+)\),\s*#?(\d+)", bio_text)
    if m:
        info["current_team"] = m.group(1).strip()
        info["current_league"] = m.group(2)
        info["jersey"] = int(m.group(3))

    return info


# ── Stats tables ──────────────────────────────────────────────────────────────

# footballdb concatenates the data-stat label with the value in some columns
# (e.g. "TD%" header renders 'TD%T%' in cell text, then '0.0' in the data cell).
# To stay robust we trust the *position* of cells in each row, not the header
# label. Column order for QB tables is well-defined and stable.

PASSING_COLS = [
    "year_id", "league", "team",
    "games", "games_started",
    "attempts", "completions", "completion_pct",
    "yards", "yards_per_attempt", "yards_per_game",
    "touchdowns", "td_pct",
    "interceptions", "int_pct",
    "longest_pass", "first_downs", "twenty_plus",
    "sacks_taken", "sack_yards_lost",
    "rating",
]

RUSHING_COLS = [
    "year_id", "league", "team",
    "games", "games_started",
    "attempts", "yards", "yards_per_attempt", "yards_per_game",
    "longest_rush", "touchdowns",
    "first_downs", "twenty_plus",
    "fumbles", "fumbles_lost",
]


def _row_to_dict(cells: list[str], cols: list[str]) -> dict:
    """Map a parsed row's cells onto column names by index. Excess cells are
    dropped; missing tail cells become None."""
    out: dict = {}
    for i, col in enumerate(cols):
        v = cells[i] if i < len(cells) else None
        if col in ("year_id", "league", "team"):
            out[col] = (v or "").strip() or None
        else:
            out[col] = _num(v)
    return out


def _parse_stats_table(table, cols: list[str]) -> list[dict]:
    """Extract data rows from a footballdb stats <table>. Skips:
       - header section row (first cell empty until the category label)
       - the column-name row
       - footer TOTALS rows (FBS/NFL/XFL/UFL TOTALS — not per-season)
       - empty rows
    """
    rows_out: list[dict] = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        first = cells[0]
        # Skip header-band row (first cell is blank, last cell is category like 'Passing')
        if first == "" or first.lower() in ("year",):
            continue
        # Skip TOTALS rows — second cell holds the league, third is "TOTALS"
        if "TOTAL" in (cells[2] if len(cells) > 2 else "").upper():
            continue
        if first.upper() == "FBS" or first.upper() == "NFL" or first.upper() == "XFL" or first.upper() == "UFL":
            # footer TOTALS row variant — first cell is league
            continue
        # Year must look like a 4-digit number for a real season row
        if not (first.isdigit() and 1980 <= int(first) <= 2100):
            continue

        # Strip team-abbreviation suffix: footballdb concatenates the team name
        # with its alias (e.g. 'North Carolina StNCST'). The visible name
        # appears first; the trailing 2-5 uppercase letters are the alias.
        if len(cells) > 2:
            tname = cells[2]
            m = re.match(r"^(.*?)([A-Z]{2,5})$", tname)
            if m and m.group(1):
                cells[2] = m.group(1).strip()

        rows_out.append(_row_to_dict(cells, cols))
    return rows_out


def _find_stats_table(soup: BeautifulSoup, category_label: str) -> object | None:
    """Find the *first* <table> whose header band contains the category label
    (e.g. 'Passing', 'Rushing'). The first such table is the Regular Season
    table — the same DOM order shows Postseason / Preseason after.
    """
    label_lower = category_label.lower()
    for t in soup.find_all("table"):
        # The first row's cells include an embedded category label
        first_row = t.find("tr")
        if not first_row:
            continue
        text = first_row.get_text(" ", strip=True).lower()
        if label_lower in text:
            return t
    return None


# ── Top-level parse ───────────────────────────────────────────────────────────

# URL pattern: https://www.footballdb.com/players/{slug}-{footballdb_id}
# where footballdb_id is e.g. 'mccleja02'.
_FDB_ID_RE = re.compile(r"/players/[^/]+-([a-z]+\d+)/?$")


def _extract_footballdb_id(url: str) -> str | None:
    m = _FDB_ID_RE.search(url)
    return m.group(1) if m else None


def parse_page(html: str, url: str) -> dict:
    """Parse a footballdb player page into a structured dict.

    Output shape (mirrors PFR scraper where possible):
        {
          "footballdb_id": "mccleja02",
          "source_url":    "https://www.footballdb.com/players/...",
          "scraped_at":    "2026-...Z",
          "player_info": { name, position, height_in, weight_lb, college,
                           birthdate, current_team, current_league, jersey },
          "stats": {
            "passing": [{year_id, league, team, games, attempts, ...}, ...],
            "rushing": [...],
          },
          "source": "footballdb",
        }

    Year rows are split by league naturally — `league` field carries
    'FBS' (college), 'NFL', 'XFL', 'UFL'. Downstream transform splits these
    into the right DDB SK pattern.
    """
    soup = BeautifulSoup(html, "html.parser")

    fdb_id = _extract_footballdb_id(url)
    info = _extract_player_info(soup)

    passing_tbl = _find_stats_table(soup, "Passing")
    rushing_tbl = _find_stats_table(soup, "Rushing")

    return {
        "footballdb_id": fdb_id,
        "source_url":    url,
        "scraped_at":    datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "player_info":   info,
        "stats": {
            "passing": _parse_stats_table(passing_tbl, PASSING_COLS) if passing_tbl else [],
            "rushing": _parse_stats_table(rushing_tbl, RUSHING_COLS) if rushing_tbl else [],
        },
        "source": "footballdb",
    }


# ── CLI entrypoint (for local testing) ───────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python footballdb_scraper.py <player_url>", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    html = fetch_page(url)
    data = parse_page(html, url)
    print(json.dumps(data, indent=2))
