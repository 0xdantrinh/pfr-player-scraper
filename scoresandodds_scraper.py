import json
import re
from bs4 import BeautifulSoup

# reuse the existing FlareSolverr fetcher from scraper.py
from scraper import fetch_page

import sys

sport = sys.argv[1] if len(sys.argv) > 1 else "mlb"
URL = f"https://www.scoresandodds.com/{sport}/consensus-picks"


def parse_consensus(html):
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    # pattern for: TEAM % of Bets TEAM 12% 88% 9% 91% % of Money
    pattern = re.compile(r"([A-Z]{2,3})\s+% of Bets\s+([A-Z]{2,3}).*?(\d+)%\s+(\d+)%\s+(\d+)%\s+(\d+)%")

    games = []

    for m in pattern.findall(text):
        away, home, bets_away, bets_home, money_away, money_home = m

        games.append({
            "away_team": away,
            "home_team": home,
            "bets_pct": {
                away: int(bets_away),
                home: int(bets_home)
            },
            "money_pct": {
                away: int(money_away),
                home: int(money_home)
            }
        })

    return games


def scrape():
    html = fetch_page(URL)
    data = parse_consensus(html)

    output_file = f"scoresandodds_{sport}_consensus.json"

    with open(output_file, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Scraped {len(data)} games for {sport} -> {output_file}")


if __name__ == "__main__":
    scrape()
