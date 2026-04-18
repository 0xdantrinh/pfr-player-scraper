import requests
from bs4 import BeautifulSoup, Comment
import json
import sys
import os
from datetime import datetime

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")


def fetch_page(url):
    # Fast request first (no challenge interaction)
    payload = {
        "cmd": "request.get",
        "url": url,
        "session": "pfr",
        "session_ttl_minutes": 60,
        "maxTimeout": 60000
    }

    r = requests.post(FLARESOLVERR_URL, json=payload, timeout=(10,60))

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


def parse_tables(soup, stats):
    for table in soup.find_all("table"):
        tid = table.get("id")
        if not tid:
            continue

        header_row = table.select_one("thead tr:not(.over_header)") or table.find("tr")

        headers = []
        if header_row:
            headers = [
                th.get("data-stat") or th.get_text(strip=True)
                for th in header_row.find_all("th")
            ]

        rows = []
        for tr in table.find_all("tr"):
            cols = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if not cols:
                continue
            rows.append(cols)

        stats[tid] = normalize_rows(headers, rows)


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
    meta = soup.find("div", id="info")
    if not meta:
        return info

    h1 = meta.find("h1")
    if h1:
        info["name"] = h1.get_text(strip=True)

    for p in meta.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if "School:" in txt:
            a = p.find("a")
            if a:
                info["school"] = a.get_text(strip=True)
        if "Position:" in txt:
            info["position"] = txt.split("Position:")[-1].strip()

    return info


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
        "career": career
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
