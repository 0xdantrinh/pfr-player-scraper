import requests
from bs4 import BeautifulSoup, Comment
import json
import sys
import os
from datetime import datetime
import re

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

    return r.json()["solution"]["response"]


def parse_height(text):
    m = re.search(r"(\d+)-(\d+)", text)
    if m:
        return int(m.group(1)) * 12 + int(m.group(2))


def parse_weight(text):
    m = re.search(r"(\d+)lb", text)
    if m:
        return int(m.group(1))


def extract_player_info(soup):
    info = {}

    meta = soup.find("div", {"id": "meta"})
    if not meta:
        return info

    name_tag = meta.find("h1")
    if name_tag:
        info["name"] = name_tag.get_text(strip=True)

    text = meta.get_text(" ", strip=True)

    height = parse_height(text)
    weight = parse_weight(text)

    if height:
        info["height_in"] = height

    if weight:
        info["weight_lb"] = weight

    college = meta.find(string=lambda x: x and "College" in x)
    if college:
        info["college"] = college.strip()

    draft = meta.find(string=lambda x: x and "Draft" in x)
    if draft:
        info["draft"] = draft.strip()

    pos = meta.find(string=lambda x: x and "Position" in x)
    if pos:
        info["position"] = pos.strip()

    return info


def extract_hof_monitor(soup):
    text = soup.get_text(" ", strip=True)

    m = re.search(r"HOF Monitor[^0-9]*([0-9]+\.[0-9]+)", text)

    if m:
        return {"score": float(m.group(1))}

    return {}


def extract_transactions(soup):
    results = []

    section = soup.find("div", id="transactions")

    if not section:
        return results

    for li in section.find_all("li"):
        results.append(li.get_text(" ", strip=True))

    return results


def extract_related_links(soup):
    gamelogs = set()
    splits = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if "/gamelog" in href:
            gamelogs.add(href)

        if "/splits" in href:
            splits.add(href)

    return {
        "gamelogs": list(gamelogs),
        "splits": list(splits)
    }


def parse_tables_from_soup(soup, results):
    for table in soup.find_all("table"):
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

            if not cols:
                continue

            if cols == headers:
                continue

            rows.append(cols)

        results[table_id] = {
            "columns": headers,
            "rows": rows
        }


def parse_comment_tables(soup, results):
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        if "table" not in comment:
            continue

        try:
            comment_soup = BeautifulSoup(comment, "lxml")
            parse_tables_from_soup(comment_soup, results)
        except Exception:
            pass


def parse_page(html, url):
    soup = BeautifulSoup(html, "lxml")

    player_slug = url.split("/")[-1].replace(".htm", "")

    results = {
        "player_id": player_slug,
        "source_url": url,
        "scraped_at": datetime.utcnow().isoformat() + "Z"
    }

    results["player_info"] = extract_player_info(soup)
    results["hof_monitor"] = extract_hof_monitor(soup)
    results["transactions"] = extract_transactions(soup)
    results["related_pages"] = extract_related_links(soup)

    parse_tables_from_soup(soup, results)
    parse_comment_tables(soup, results)

    return results


def scrape_player(url):
    html = fetch_page(url)

    data = parse_page(html, url)

    player_id = data["player_id"]

    with open(f"{player_id}.json", "w") as f:
        json.dump(data, f, indent=2)

    print("Saved", player_id + ".json")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scraper.py <player_url>")
        sys.exit(1)

    scrape_player(sys.argv[1])
