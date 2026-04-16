import requests
from bs4 import BeautifulSoup
import json
import sys
import os

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")


def fetch_page(url):
    payload = {
        "cmd": "request.get",
        "url": url,
        "session": "pfr",
        "maxTimeout": 300000
    }

    r = requests.post(FLARESOLVERR_URL, json=payload, timeout=300)

    if r.status_code != 200:
        print("FlareSolverr error:", r.text)

    r.raise_for_status()

    data = r.json()

    return data["solution"]["response"]


def extract_player_info(soup):
    info = {}
    meta = soup.find("div", {"id": "meta"})

    if not meta:
        return info

    name_tag = meta.find("h1")
    if name_tag:
        info["name"] = name_tag.get_text(strip=True)

    ps = meta.find_all("p")

    for p in ps:
        text = p.get_text(" ", strip=True)

        if "Position" in text:
            info["position"] = text
        elif "College" in text:
            info["college"] = text
        elif "Height" in text or "Weight" in text:
            info["physical"] = text

    return info


def parse_tables(html):
    soup = BeautifulSoup(html, "lxml")

    results = {}

    results["player_info"] = extract_player_info(soup)

    tables = soup.find_all("table")

    for table in tables:
        table_id = table.get("id")

        if not table_id:
            continue

        headers = []
        header_row = table.find("tr")

        if header_row:
            headers = [th.get_text(strip=True) for th in header_row.find_all("th")]

        rows = []

        for tr in table.find_all("tr"):
            cols = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]

            if cols and cols != headers:
                rows.append(cols)

        results[table_id] = {
            "columns": headers,
            "rows": rows
        }

    return results


def scrape_player(url):
    html = fetch_page(url)

    data = parse_tables(html)

    player_id = url.split("/")[-1].replace(".htm", "")

    with open(f"{player_id}.json", "w") as f:
        json.dump(data, f, indent=2)

    print("Saved", player_id + ".json")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scraper.py <player_url>")
        sys.exit(1)

    scrape_player(sys.argv[1])
