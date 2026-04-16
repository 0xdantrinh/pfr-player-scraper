import os
import json
import re
from datetime import datetime
from bs4 import BeautifulSoup, Comment
from scraper import fetch_page


def parse_team_page(html, url, team_id, season):
    soup = BeautifulSoup(html, "lxml")

    text = soup.get_text(" ", strip=True)

    def extract_rank(label):
        m = re.search(label + r"[^0-9]*([0-9]+)", text)
        if m:
            return int(m.group(1))

    record = {}
    m = re.search(r"Record:\s*(\d+)-(\d+)(?:-(\d+))?", text)
    if m:
        record = {
            "wins": int(m.group(1)),
            "losses": int(m.group(2)),
            "ties": int(m.group(3) or 0)
        }

    coach = None
    coach_match = re.search(r"Coach:\s*([^\n]+)", text)
    if coach_match:
        coach = coach_match.group(1).strip()

    data = {
        "team_id": team_id.upper(),
        "season": int(season),
        "source_url": url,
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "record": record,
        "coach": coach,
        "offense_context": {
            "points_rank": extract_rank("Points"),
            "yards_rank": extract_rank("Yards"),
            "pass_yards_rank": extract_rank("Pass Yds"),
            "rush_yards_rank": extract_rank("Rush Yds")
        }
    }

    return data


def scrape_team(team_id, season):
    team_id_lower = team_id.lower()
    url = f"https://www.pro-football-reference.com/teams/{team_id_lower}/{season}.htm"

    html = fetch_page(url)

    data = parse_team_page(html, url, team_id, season)

    os.makedirs(f"teams/{team_id.upper()}", exist_ok=True)

    path = f"teams/{team_id.upper()}/{season}.json"

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    print("Saved", path)

    return path


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("Usage: python team_scraper.py TEAM_ID SEASON")
        sys.exit(1)

    scrape_team(sys.argv[1], sys.argv[2])
