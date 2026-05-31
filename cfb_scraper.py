import re
import requests
from bs4 import BeautifulSoup, Comment
import json
import sys
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")

# Sticky session for sports-reference.com/cfb — separate domain from
# pro-football-reference.com, so needs its own Cloudflare cookie.
CFB_SESSION = "cfb-main"


def fetch_page(url):
    # Fast request first (no challenge interaction)
    payload = {
        "cmd": "request.get",
        "url": url,
        "session": CFB_SESSION,
        "session_ttl_minutes": 60,
        "maxTimeout": 120000
    }

    r = requests.post(FLARESOLVERR_URL, json=payload, timeout=(10,120))

    if r.status_code != 200:
        print("FlareSolverr error:", r.text)

    r.raise_for_status()

    data = r.json()
    html = data["solution"]["response"]

    # Detect Cloudflare challenge page
    if "cf-chl" in html.lower() or "just a moment" in html.lower():
        print("Challenge detected, retrying with solver...")

        payload["tabs_till_verify"] = 5
        payload["maxTimeout"] = 300000

        r = requests.post(FLARESOLVERR_URL, json=payload, timeout=(10,600))
        r.raise_for_status()
        data = r.json()
        html = data["solution"]["response"]

    return html


def normalize_rows(headers, rows):
    out = []
    for r in rows:
        if headers and r and r[0] == headers[0]:
            continue

        row = r[: len(headers)]
        if len(row) < len(headers):
            row += [""] * (len(headers) - len(row))

        obj = {}
        for i, col in enumerate(headers):
            key = col.strip().lower().replace(" ", "_")
            obj[key] = row[i]

        yr = obj.get("year_id") or obj.get("year") or obj.get("season")

        # remove blank rows
        if not yr:
            continue

        # remove column header rows
        if yr in ["Season", "Year"]:
            continue

        # remove career / totals rows
        if yr == "Career":
            continue

        # remove section label rows (Receiving, Rushing, etc)
        if not str(yr)[0].isdigit():
            continue

        obj["year_id"] = str(yr).replace("*", "")

        out.append(obj)
    return out


def _infer_table_id(headers):
    """Infer a table ID from column header data-stat keys for tables with no HTML id."""
    h = set(h.lower() for h in headers if h)
    if "rush_att" in h or ("att" in h and "yds" in h and "rec" in h):
        return "rushing_standard"
    if "tackles_solo" in h or "tackles_combined" in h or "def_int" in h:
        return "defense_standard"
    if "kick_ret" in h or "punt_ret" in h:
        return "kick_return_standard"
    if "pass_cmp" in h or "pass_att" in h:
        return "passing_standard"
    if "rec" in h and "rec_yds" in h:
        return "receiving_standard"
    return None


def parse_tables(soup, stats):
    for table in soup.find_all("table"):
        tid = table.get("id")

        header_row = table.select_one("thead tr:not(.over_header)") or table.find("tr")

        headers = []
        if header_row:
            headers = [
                th.get("data-stat") or th.get_text(strip=True)
                for th in header_row.find_all("th")
            ]

        if not tid:
            tid = _infer_table_id(headers)
        if not tid:
            continue

        rows = []
        bold_flags = []  # parallel list: which rows had any bold (led-conference) cell
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            cols = [c.get_text(strip=True) for c in cells]
            # A bold <strong> tag in any data cell means that stat led the conference
            led = any(bool(c.find("strong")) for c in cells if c.name == "td")
            rows.append(cols)
            bold_flags.append(led)

        normalized = normalize_rows(headers, rows)
        # Attach led_conference flag to each normalized row (index aligns after normalize_rows
        # strips header/career rows, so we re-derive by matching year_id)
        bold_by_year = {}
        for row, led in zip(rows, bold_flags):
            if row:
                bold_by_year[row[0]] = led
        for obj in normalized:
            yr = obj.get("year_id", "")
            obj["led_conference"] = bold_by_year.get(yr, False)

        stats[tid] = normalized


def parse_comment_tables(soup, stats):
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        if "table" not in c:
            continue
        try:
            cs = BeautifulSoup(c, "lxml")
            parse_tables(cs, stats)
        except Exception:
            pass


def extract_meta(soup):
    info = {}

    # Name — try div#info or div#meta
    for div_id in ("info", "meta"):
        container = soup.find("div", id=div_id)
        if container:
            h1 = container.find("h1")
            if h1:
                info["name"] = h1.get_text(strip=True)
                break

    # Bio paragraphs — search the full page so we're not sensitive to nesting
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if "School:" in txt and "school" not in info:
            a = p.find("a")
            if a:
                info["school"] = a.get_text(strip=True)
        if "Position:" in txt and "position" not in info:
            info["position"] = txt.split("Position:")[-1].strip()
        if "Draft:" in txt and "draft" not in info:
            m = re.search(r"(\d+)(?:st|nd|rd|th) round.*?(\d+)(?:st|nd|rd|th) overall.*?(\d{4})", txt)
            if m:
                info["draft"] = {
                    "round": int(m.group(1)),
                    "pick":  int(m.group(2)),
                    "year":  int(m.group(3)),
                }

    return info


def parse_leaderboard_awards(soup):
    """Parse the 'Appearances on Leaderboards, Awards, and Honors' section.

    Returns a dict with:
      led_conference_count  - seasons where a stat led the conference (#1)
      all_american_count    - All-American selections
      all_conference_count  - All-Conference selections
      leaderboard_appearances - total leaderboard entries
    """
    out = {
        "led_conference_count":    0,
        "all_american_count":      0,
        "all_conference_count":    0,
        "leaderboard_appearances": 0,
    }

    # SR renders leaderboard data in div#div_leaderboard / div.leaderboard_grid —
    # a sibling of the section heading, not a child. Find it directly.
    def _leaderboard_text(s):
        grid = s.find("div", id="div_leaderboard") or s.find("div", class_="leaderboard_grid")
        if grid:
            return grid.get_text(" ", strip=True)
        # Fallback: walk up from the h2 heading to a common ancestor and grab all text
        for h2 in s.find_all(["h2", "h3", "h4"]):
            if "leaderboard" in h2.get_text(strip=True).lower() or "awards" in h2.get_text(strip=True).lower():
                wrapper = h2.find_parent("div", id=lambda x: x and "leaderboard" in x) or h2.find_parent()
                return wrapper.get_text(" ", strip=True) if wrapper else None
        return None

    text = _leaderboard_text(soup)

    if not text:
        # Also check HTML comments (SR sometimes hides sections in comments)
        for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
            if "leaderboard" in c.lower() or "awards and honors" in c.lower():
                inner = BeautifulSoup(c, "lxml")
                text = _leaderboard_text(inner) or str(c)
                break

    if not text:
        return out

    # Count #1 appearances (led conference)
    out["led_conference_count"] = len(re.findall(r'\(#1\)', text))
    # Total leaderboard entries
    out["leaderboard_appearances"] = len(re.findall(r'\(#\d+\)', text))
    # Award keywords
    text_lower = text.lower()
    out["all_american_count"]  = text_lower.count("all-american")
    out["all_conference_count"] = text_lower.count("all-conference") + text_lower.count("all-sec") + \
                                   text_lower.count("all-big") + text_lower.count("all-acc") + \
                                   text_lower.count("all-pac") + text_lower.count("all-mac") + \
                                   text_lower.count("all-aac") + text_lower.count("all-mwc")

    return out


def parse_page(html, url):
    soup = BeautifulSoup(html, "lxml")

    slug = url.split("/")[-1].split(".")[0]

    stats = {}
    parse_tables(soup, stats)
    parse_comment_tables(soup, stats)

    # compute simple career totals from receiving table if present
    career = {}
    rec_rows = stats.get("receiving_standard", [])
    if rec_rows:
        total_rec = 0
        total_yds = 0
        total_td = 0
        for r in rec_rows:
            try:
                total_rec += int(r.get("rec") or 0)
                total_yds += int(r.get("rec_yds") or 0)
                total_td += int(r.get("rec_td") or 0)
            except Exception:
                pass

        career["receiving"] = {
            "rec": total_rec,
            "yards": total_yds,
            "td": total_td
        }

    return {
        "player_id": slug,
        "source": "sports-reference-cfb",
        "source_url": url,
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "player_info": extract_meta(soup),
        "stats": stats,
        "career": career,
        "awards": parse_leaderboard_awards(soup),
    }


def scrape_player(url):
    html = fetch_page(url)
    data = parse_page(html, url)

    pid = data["player_id"]

    with open(f"cfb_{pid}.json", "w") as f:
        json.dump(data, f, indent=2)

    print("Saved", f"cfb_{pid}.json")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python cfb_scraper.py <cfb_player_url>")
        sys.exit(1)

    scrape_player(sys.argv[1])
