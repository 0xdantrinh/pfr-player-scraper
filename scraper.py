import requests
from bs4 import BeautifulSoup, Comment
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
        elif "Draft" in text:
            info["draft"] = text

    return info


def extract_hof_monitor(soup):
    hof = {}
    text = soup.get_text(" ", strip=True)

    if "HOF Monitor" in text or "HOF" in text:
        try:
            for line in text.split():
                if line.replace(".", "", 1).isdigit():
                    hof["score"] = line
                    break
        except Exception:
            pass

    return hof


def extract_transactions(soup):
    transactions = []

    trans_section = soup.find(string=lambda x: x and "Transactions" in x)

    if trans_section:
        ul = trans_section.find_parent().find_next("ul")

        if ul:
            for li in ul.find_all("li"):
                transactions.append(li.get_text(" ", strip=True))

    return transactions


def extract_related_links(soup):
    links = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if "gamelog" in href:
            links.setdefault("gamelogs", []).append(href)
        if "splits" in href:
            links.setdefault("splits", []).append(href)

    return links


def parse_tables_from_soup(soup, results):
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


def parse_comment_tables(soup, results):
    comments = soup.find_all(string=lambda text: isinstance(text, Comment))

    for comment in comments:
        if "table" in comment:
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
        "source_url": url
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
