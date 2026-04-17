import requests
from bs4 import BeautifulSoup, Comment
import json
import sys
import os
from datetime import datetime

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")


def fetch_page(url):
    payload = {
        "cmd": "request.get",
        "url": url,
        "session": "pfr",
        "session_ttl_minutes": 60,
        "maxTimeout": 300000,
        "tabs_till_verify": 5
    }

    r = requests.post(FLARESOLVERR_URL, json=payload, timeout=(10,600))

    if r.status_code != 200:
        print("FlareSolverr error:", r.text)

    r.raise_for_status()

    return r.json()["solution"]["response"]


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

        yr = obj.get("year") or obj.get("season")
        if yr in ["Season", "Year", ""]:
            continue

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

    return {
        "player_id": slug,
        "source": "sports-reference-cfb",
        "source_url": url,
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "player_info": extract_meta(soup),
        "stats": stats,
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
