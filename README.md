# Pro Football Reference Player Scraper (AWS Scalable Pipeline)

A scalable scraping pipeline for collecting player statistics from **Pro-Football-Reference** using **FlareSolverr** to bypass Cloudflare protections.

Designed to run on **AWS ECS Fargate** with **SQS for job distribution** and **S3 for storage**.

---

# Architecture

Pipeline flow:

Player URL
↓
SQS Queue
↓
ECS Fargate Scraper Workers
↓
FlareSolverr Service
↓
S3 JSON Storage

This architecture allows you to scale horizontally by increasing the number of scraper workers.

Example scaling:

- 10 workers → 10 concurrent scrapes
- 100 workers → 100 concurrent scrapes

---

# Components

## FlareSolverr

Handles Cloudflare challenges.

Container image:

```
ghcr.io/flaresolverr/flaresolverr:latest
```

Runs as an ECS service or task.

Workers send requests to:

```
http://flaresolverr:8191/v1
```

---

## Scraper Worker

Consumes player URLs from SQS and performs:

1. Fetch page through FlareSolverr
2. Parse stat tables
3. Upload structured JSON to S3

Worker file:

```
worker.py
```

---

# Repository Structure

```
pfr-player-scraper
│
├── scraper.py                  ← PFR + CFB page fetcher/parser (FlareSolverr)
├── worker.py                   ← SQS consumer: fetches pages, uploads JSON to S3
├── cfb_scraper.py              ← College Football Reference scraper (passing + receiving stats)
├── capture_betting_splits.py   ← DraftKings Network betting splits scraper (run locally)
├── enqueue_players.py          ← Helper: batch-enqueue player URLs to SQS
├── requirements.txt
├── Dockerfile
│
├── splits/                     ← Local JSON snapshots from capture_betting_splits.py
│   ├── ufl/
│   │   └── YYYY-MM-DD/
│   │       └── {away}@{home}.json
│   └── ufc/
│       └── YYYY-MM-DD/
│           └── {fighter1}vs{fighter2}.json
│
├── aws
│   └── ecs
│       ├── flaresolverr-task.json
│       └── scraper-task.json
│
└── README.md
```

---

# Local Development

Start FlareSolverr locally:

```
docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr
```

Install dependencies:

```
pip install -r requirements.txt
```

Run NFL player scraper manually:

```
python scraper.py https://www.pro-football-reference.com/players/B/BradTo00.htm
```

Output:

```
BradTo00.json
```

---

# College Football Reference Scraper

The repository now includes a **College Football Reference player scraper** for collecting college statistics from Sports Reference.

Example player page:

```
https://www.sports-reference.com/cfb/players/ceedee-lamb-1.html
```

## Scraper File

```
cfb_scraper.py
```

This scraper uses the **same FlareSolverr request pipeline** as the main Pro‑Football‑Reference scraper to avoid Cloudflare blocking.

## Running the College Scraper

```
python cfb_scraper.py https://www.sports-reference.com/cfb/players/ceedee-lamb-1.html
```

Example output:

```
cfb_ceedee-lamb-1.json
```

## Output Structure

Example:

```
{
  "player_id": "ceedee-lamb-1",
  "source": "sports-reference-cfb",
  "source_url": "https://www.sports-reference.com/cfb/players/ceedee-lamb-1.html",
  "scraped_at": "2026-04-17T00:00:00Z",
  "player_info": {
    "name": "CeeDee Lamb",
    "school": "Oklahoma",
    "position": "WR"
  },
  "stats": {
    "receiving_and_rushing": [...],
    "punt_and_kick_returns": [...],
    "scoring": [...]
  }
}
```

All stat tables are automatically extracted, including tables hidden inside HTML comments (a common Sports Reference pattern).

This keeps the parsing logic consistent with the NFL scraper.

---

# Betting Splits (DraftKings Network)

Capture and track betting splits (handle % and tickets %) from DraftKings Network for any sport.

## Setup

Add to your `.env`:

```
SPLITS_S3_BUCKET=moneyline-splits
```

## Capture Splits

Run the capture script to fetch current active games:

```bash
python capture_betting_splits.py --league ufl
```

Supported leagues: `ufl`, `ufc`, `nfl`, `nba`, `mlb`

**Output:**
- Local: `splits/{league}/{YYYY-MM-DD}/{away}@{home}.json`
- S3: `s3://moneyline-splits/{league}/{YYYY-MM-DD}/{away}@{home}.json`

Each run updates the local file and uploads to S3. Historical arrays track market movement over time.

### Features

- **Market tracking**: Automatically maintains history arrays for handle %, bets %, and odds movement
- **Sharp detection**: Calculates sharp action deltas (handle % - bets % spread)
- **Mr Vegas flag**: Flags games where either participant has +180 or better odds (sticky flag persists once triggered)
- **Auto-plotting**: Generates `plots/{league}/{away}@{home}.png` showing market movement (requires matplotlib)

### Dry run

```bash
python capture_betting_splits.py --league ufl --dry-run
```

## Plot Market Movement

List available games:

```bash
python plot_splits.py --league ufl
```

Plot a specific matchup:

```bash
python plot_splits.py --league ufl --matchup "STL@HOU"
```

### Sync from S3

Prediction repos can pull the latest splits from S3:

```bash
python plot_splits.py --league ufl --sync-s3
```

This downloads all splits for the league from S3 before plotting. Useful for keeping prediction models in sync with the latest betting data.

---

# AWS Deployment

## 1 Build Docker Image

```
docker build -t pfr-scraper .
```

---

## 2 Create ECR Repository

```
aws ecr create-repository --repository-name pfr-scraper
```

Login to ECR:

```
aws ecr get-login-password --region <region> | \
 docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
```

Tag image:

```
docker tag pfr-scraper:latest <account>.dkr.ecr.<region>.amazonaws.com/pfr-scraper:latest
```

Push image:

```
docker push <account>.dkr.ecr.<region>.amazonaws.com/pfr-scraper:latest
```

---

# ECS Task Definitions

Located in:

```
aws/ecs/
```

Register tasks:

```
aws ecs register-task-definition \
--cli-input-json file://aws/ecs/flaresolverr-task.json

aws ecs register-task-definition \
--cli-input-json file://aws/ecs/scraper-task.json
```

---

# SQS Queue

Create queue:

```
aws sqs create-queue --queue-name pfr-player-urls
```

Example queue URL:

```
https://sqs.us-east-1.amazonaws.com/ACCOUNT/pfr-player-urls
```

---

# S3 Storage

Create bucket:

```
aws s3 mb s3://pfr-scraped-data
```

Output files stored as:

```
players/<player_id>.json
```

Example:

```
players/BradTo00.json
```

---

# Worker Environment Variables

Configure in ECS task definition.

```
SQS_QUEUE_URL=<queue_url>
S3_BUCKET=<bucket_name>
AWS_REGION=<region>
FLARESOLVERR_URL=http://flaresolverr:8191/v1
```

---

# Queue Player URLs

Helper script:

```
enqueue_players.py
```

Usage:

```
cat player_urls.txt | python enqueue_players.py $SQS_QUEUE_URL
```

Example file:

```
https://www.pro-football-reference.com/players/B/BradTo00.htm
https://www.pro-football-reference.com/players/M/MahoPa00.htm
```

---

# Scaling Strategy

Recommended configuration:

FlareSolverr containers:

```
3–10 instances
```

Scraper workers:

```
20–200 workers
```

Workers randomly connect to FlareSolverr endpoints to avoid Cloudflare throttling.

---

# Future Improvements

Recommended upgrades for a full production data pipeline:

• Automatic player discovery crawler
• Terraform infrastructure deployment
• Multi‑FlareSolverr load balancing
• Postgres warehouse for analytics
• Game log scraping
• Historical season scraping
• EventBridge scheduling

---

# Example Full Pipeline

Crawler
↓
SQS Player URL Queue
↓
ECS Fargate Workers
↓
FlareSolverr Pool
↓
S3 Data Lake

---

# License

MIT

---

# Team Scraper (Offensive Context Dataset)

The repository now includes a **team scraper** for Pro-Football-Reference team pages. This enables collecting team-level context metrics used to normalize player performance.

This data helps models distinguish between:

- good player on a weak offense
- average player on an elite offense

## Team Scraper File

```
team_scraper.py
```

## Team Page Format

Team pages follow the structure:

```
https://www.pro-football-reference.com/teams/{team}/{season}.htm
```

Example:

```
https://www.pro-football-reference.com/teams/dal/2023.htm
```

## Output Location

Team data is stored separately from players:

```
teams/{TEAM_ID}/{SEASON}.json
```

Example:

```
teams/DAL/2023.json
```

This keeps the dataset modular.

## Example Output

```
{
  "team_id": "DAL",
  "season": 2023,
  "record": {
    "wins": 12,
    "losses": 5,
    "ties": 0
  },
  "coach": "Mike McCarthy",
  "offense_context": {
    "points_rank": 5,
    "yards_rank": 3,
    "pass_yards_rank": 4,
    "rush_yards_rank": 14
  }
}
```

## Running Team Scraper

```
python team_scraper.py DAL 2023
```

This produces:

```
teams/DAL/2023.json
```

## Dataset Join Strategy

Player and team datasets can be joined using:

```
player_stats.team
player_stats.season
```

with

```
team_stats.team_id
team_stats.season
```

This enables features such as:

- player yardage share
- touchdown share
- offense-adjusted production

These features significantly improve player rating models.

# FlareSolverr Configuration (Important)

To reliably bypass Cloudflare Turnstile challenges on Pro‑Football‑Reference the scraper uses a persistent FlareSolverr session and several performance optimizations.

The scraper sends requests using:

```
{
  "cmd": "request.get",
  "url": url,
  "session": "pfr",
  "session_ttl_minutes": 60,
  "maxTimeout": 300000,
  "tabs_till_verify": 5,
  "disableMedia": true
}
```

Explanation of parameters:

session

Keeps a persistent browser instance so Cloudflare cookies are reused.

session_ttl_minutes

Automatically rotates the session after the TTL to prevent stale browser state.

maxTimeout

Maximum time allowed to solve Cloudflare challenges (milliseconds).

300000 = 5 minutes.

tabs_till_verify

Automatically presses TAB multiple times then SPACE to click the Cloudflare Turnstile checkbox.

This is required because Turnstile challenges do not automatically resolve without interaction.

disableMedia

Prevents images, fonts, and other heavy resources from loading.

Benefits:

• Faster page loads
• Lower Chrome CPU usage
• Reduced memory consumption

Example local FlareSolverr run:

```
docker run -d  --name=flaresolverr  -p 8191:8191  -e LOG_LEVEL=info  -e DISABLE_MEDIA=true  --restart unless-stopped  ghcr.io/flaresolverr/flaresolverr:latest
```

Recommended concurrency:

```
1 FlareSolverr instance
2 scraper workers
```

Running too many concurrent workers against a single FlareSolverr instance can cause Chrome timeouts.

## Recommended Local FlareSolverr Setup

Run FlareSolverr with media disabled (much faster for PFR):

```
docker run -d \
 --name flaresolverr \
 -p 8191:8191 \
 -e LOG_LEVEL=info \
 -e DISABLE_MEDIA=true \
 --restart unless-stopped \
 ghcr.io/flaresolverr/flaresolverr:latest
```

This disables images, fonts, and other heavy resources inside the browser which significantly speeds up navigation while still allowing Cloudflare challenges to execute.

---

# Betting Splits Capture

`capture_betting_splits.py` captures betting splits (handle % and bets %) from two independent sources via FlareSolverr. Run it manually or on a cron to build a market-movement timeline per game.

## Sources

| `--source`         | Data from                     | Bet types                 | Sports supported                                    |
| ------------------ | ----------------------------- | ------------------------- | --------------------------------------------------- |
| `draftkings`       | DraftKings Network (single-book) | Moneyline + Total + Spread | `ufl` `ufc` `nfl` `nba` `mlb`                      |
| `betmgm_caesars`   | ScoresAndOdds.com (BetMGM + Caesars combined) | Moneyline + Total + Spread/Runline/Puckline | `nfl` `nba` `mlb` `ncaaf` `ncaab` `nhl` `wnba` |

**Note:** ScoresAndOdds only shows games for the current day; DraftKings shows games up to 7 days ahead.

## Usage

Start FlareSolverr first:

```bash
docker start flaresolverr
```

Activate virtual environment:

```bash
source venv/bin/activate
```

**DraftKings (default):**

```bash
python capture_betting_splits.py --league ufl
python capture_betting_splits.py --league nfl
python capture_betting_splits.py --league ufl --dry-run
```

**BetMGM + Caesars (ScoresAndOdds):**

```bash
python capture_betting_splits.py --league mlb --source betmgm_caesars
python capture_betting_splits.py --league nhl --source betmgm_caesars
python capture_betting_splits.py --league nba --source betmgm_caesars --dry-run
```

## Output Structure

**DraftKings:** `splits/{league}/{YYYY-MM-DD}/{away}@{home}.json`
→ S3: `s3://moneyline-splits/{league}/{date}/{file}.json`

**BetMGM+Caesars:** `splits/betmgm_caesars/{league}/{YYYY-MM-DD}/{away}@{home}.json`
→ S3: `s3://moneyline-splits/betmgm_caesars/{league}/{date}/{file}.json`

## Data Structure

Each JSON file contains the full moneyline snapshot at the top level (backwards compatible) plus `total` and `spread` nested sections:

```json
{
  "matchup": "TOR @ ATL",
  "source": "betmgm_caesars",

  "participant1_bets_pct": 13,
  "participant2_bets_pct": 87,
  "participant1_handle_pct": 18,
  "participant2_handle_pct": 82,
  "participant1_odds": "+220",
  "participant2_odds": "-250",
  "p1_sharp_delta": -5,
  "p2_sharp_delta": 5,
  "mr_vegas_flag": false,

  "total": {
    "line": "7.5",
    "over_bets_pct": 94,
    "under_bets_pct": 6,
    "over_handle_pct": 84,
    "under_handle_pct": 16,
    "over_odds": "-120",
    "under_odds": "-115",
    "over_sharp_delta": -10
  },

  "spread": {
    "participant1_line": "+1.5",
    "participant2_line": "-1.5",
    "participant1_bets_pct": 11,
    "participant2_bets_pct": 89,
    "participant1_handle_pct": 11,
    "participant2_handle_pct": 89,
    "participant1_odds": "even",
    "participant2_odds": "-113",
    "p1_sharp_delta": 0,
    "p2_sharp_delta": 0
  },

  "participant1_handle_pct_history": [18],
  "participant2_handle_pct_history": [82],
  "participant1_bets_pct_history": [13],
  "participant2_bets_pct_history": [87],
  "participant1_odds_history": ["+220"],
  "participant2_odds_history": ["-250"],
  "p1_sharp_delta_history": [-5],
  "p2_sharp_delta_history": [5],
  "total_over_bets_pct_history": [94],
  "total_under_bets_pct_history": [6],
  "total_over_handle_pct_history": [84],
  "total_under_handle_pct_history": [16],
  "spread_participant1_bets_pct_history": [11],
  "spread_participant2_bets_pct_history": [89],
  "spread_participant1_handle_pct_history": [11],
  "spread_participant2_handle_pct_history": [89],
  "captured_at_history": ["2026-06-04T22:15:00Z"]
}
```

## Interpreting the Data

### Sharp Money Signals

`p2_sharp_delta = handle_pct - bets_pct` for participant 2 (home team / home side).

- **Positive sharp delta on P2**: Sharp money backing P2; public loves P1
- **Negative sharp delta on P2**: Sharps fading P2; public action on P2

Same logic applies to `total.over_sharp_delta` (over handle% − over bets%).

### Mr Vegas Theory

`mr_vegas_flag` triggers if either participant's moneyline odds ever reached **+180 or better**. Once triggered the flag is **sticky** — it stays `true` even if the line tightens, because the initial signal is what matters.

```json
{
  "matchup": "Dallas Renegades @ Louisville Kings",
  "participant1_odds": "+114",
  "mr_vegas_flag": true
}
```

→ DAL or LOU hit +180+ at some point — classic Mr Vegas setup.

## Plotting Market Movement

```bash
# List available games
python plot_splits.py --league ufl

# Plot a specific game (7 subplots: ML handle/bets/odds + total handle/bets + spread handle/bets)
python plot_splits.py --league ufl --matchup "STL@HOU"
python plot_splits.py --league betmgm_caesars/mlb --matchup "TOR@ATL"

# Sync latest from S3 first
python plot_splits.py --league ufl --sync-s3
```

The chart shows up to **7 subplots** when total and spread data are present:

1. Moneyline — Handle %
2. Moneyline — Bets %
3. Moneyline — Line Movement (odds)
4. Total — Handle % (Over vs Under)
5. Total — Bets % (Over vs Under)
6. Spread/Runline — Handle %
7. Spread/Runline — Bets %

## Recommended Cron

```cron
# DK splits every 30 min on game days
*/30 * * * * cd /path/to/pfr-player-scraper && source venv/bin/activate && python capture_betting_splits.py --league nfl

# BetMGM+Caesars MLB every hour on game days
0 * * * * cd /path/to/pfr-player-scraper && source venv/bin/activate && python capture_betting_splits.py --league mlb --source betmgm_caesars
```
