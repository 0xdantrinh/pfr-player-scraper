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
- S3: `s3://moneyline-splits/betting-splits/{league}/{YYYY-MM-DD}/{away}@{home}.json`

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

# DraftKings Betting Splits Capture

`capture_betting_splits.py` scrapes the [DK Network betting splits page](https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/) via FlareSolverr and saves a local JSON snapshot per game. Run it manually or via cron whenever you want fresh splits.

## Supported Sports

| League | Event Group | Notes          |
| ------ | ----------- | -------------- |
| `ufl`  | 212333      | UFL football   |
| `ufc`  | 9034        | UFC fights     |
| `nfl`  | 88808       | NFL football   |
| `nba`  | 42648       | NBA basketball |
| `mlb`  | 84240       | MLB baseball   |

Find additional event group IDs in the `tb_eg=` URL parameter on the DK Network page.

## Usage

Start FlareSolverr first:

```bash
docker start flaresolverr
```

Activate virtual environment:

```bash
source venv/bin/activate
```

Capture all upcoming games for a league:

```bash
# Capture all upcoming UFL games
python capture_betting_splits.py --league ufl

# Capture UFC card
python capture_betting_splits.py --league ufc

# Dry run (print without saving)
python capture_betting_splits.py --league ufl --dry-run
```

## Data Structure

Files saved to `splits/{league}/{YYYY-MM-DD}/{participant1}@{participant2}.json`:

```
splits/ufl/2026-05-22/DC@ORL.json
splits/ufc/2026-05-16/tokkos-tuco-vs-erslan-ivan.json
```

Each JSON includes:

**Current Snapshot:**

- `participant1/2_odds` — Current moneyline odds
- `participant1/2_handle_pct` — % of money wagered on each side
- `participant1/2_bets_pct` — % of tickets (public) on each side
- `p1/p2_sharp_delta` — handle% minus bets% (positive = sharp money on that side)
- `mr_vegas_flag` — true if either participant hit +180 odds (Mr Vegas Theory indicator)

**Historical Arrays** (for plotting):

- `participant1/2_odds_history` — Array of odds over time
- `participant1/2_handle_pct_history` — Array of handle percentages
- `participant1/2_bets_pct_history` — Array of public bet percentages
- `p1/p2_sharp_delta_history` — Array of sharp deltas
- `captured_at_history` — Array of capture timestamps (for x-axis)

Example:

```json
{
  "matchup": "St. Louis Battlehawks @ Houston Gamblers",
  "participant1": "St. Louis Battlehawks",
  "participant2": "Houston Gamblers",
  "participant1_odds": "-162",
  "participant2_odds": "+136",
  "participant1_handle_pct": 92,
  "participant2_handle_pct": 8,
  "participant1_bets_pct": 77,
  "participant2_bets_pct": 23,
  "p1_sharp_delta": 15,
  "p2_sharp_delta": -15,
  "mr_vegas_flag": false,
  "participant1_odds_history": ["-162", "-165", "-162"],
  "participant2_odds_history": ["+136", "+140", "+136"],
  "participant1_handle_pct_history": [94, 93, 92],
  "participant2_handle_pct_history": [6, 7, 8],
  "participant1_bets_pct_history": [85, 80, 77],
  "participant2_bets_pct_history": [15, 20, 23],
  "captured_at_history": [
    "2026-05-18T17:00:07.419397+00:00",
    "2026-05-18T21:30:12.805621+00:00",
    "2026-05-19T16:52:45.592317+00:00"
  ]
}
```

## Interpreting the Data

### Sharp Money Signals

**Positive `p1_sharp_delta`:**

- Handle % on P1 > Bets % on P1
- Example: 79% handle vs 43% bets = 36% sharp delta
- Meaning: Sharp money is backing P1, public favors P2

**Negative `p1_sharp_delta`:**

- Handle % on P1 < Bets % on P1
- Sharp money is opposing P1, public favors P1

### Mr Vegas Theory

The `mr_vegas_flag` triggers if either participant's odds ever reached **+180 or better**.

**Why it matters:**

- Vegas initially sets the line to balance action
- If a dog hits +180+, it signals Vegas's initial lean toward that underdog
- Once triggered, the flag remains `true` even if odds tighten (validates the theory)

Example:

```json
{
  "matchup": "Dallas Renegades @ Louisville Kings",
  "participant1_odds": "+114",
  "participant2_odds": "-135",
  "mr_vegas_flag": true
}
```

→ Either DAL or LOU hit +180+ at some point. This matchup is a Mr Vegas candidate.

## Plotting Market Movement

Use `plot_splits.py` to visualize how betting splits and odds have moved over time.

### Installation

Make sure matplotlib is installed:

```bash
pip install matplotlib
```

### Commands

**List all games for a league:**

```bash
python plot_splits.py --league ufl
```

Output:

```
Available games for ufl:
  D.C. Defenders @ Orlando Storm (-108 / -112)
  Birmingham Stallions @ Columbus Aviators (93 / 7)
  Dallas Renegades @ Louisville Kings (+114 / -135) [MR VEGAS]
  St. Louis Battlehawks @ Houston Gamblers (-162 / +136)

To plot a game, use: python plot_splits.py --league ufl --matchup <name>
```

**Plot a specific game:**

```bash
python plot_splits.py --league ufl --matchup "STL@HOU"
python plot_splits.py --league ufl --matchup "DAL@LOU"
```

**Plot all games in one figure (one per window):**

```bash
python plot_splits.py --league ufl --all
```

### Output

The plot shows 3 subplots over time:

1. **Handle % (Money Wagered)** — Shows which side professionals and public are backing
2. **Bets % (Public Tickets)** — Shows where casual bettors are placing tickets
3. **Line Movement** — Shows how odds have shifted (indicates market adjustment)

**Reading the chart:**

- Diverging lines in Handle vs Bets = sharp/public split
- Sharp handle increasing = smart money adjusting
- Line moving toward underdog = market correcting

## Sharp Signal Example

```json
{
  "matchup": "Dallas Renegades @ Louisville Kings",
  "participant1_handle_pct": 73,
  "participant2_handle_pct": 27,
  "participant1_bets_pct": 25,
  "participant2_bets_pct": 75,
  "p1_sharp_delta": 48,
  "mr_vegas_flag": true
}
```

**Interpretation:**

- 73% of money on DAL (underdog at +114)
- Only 25% of tickets on DAL
- 48-point sharp delta = massive sharp divergence
- Mr Vegas flag = DAL hit +180+ at some point
- **Signal:** Sophisticated money is heavily backing DAL despite public loving LOU

## Recommended Cron

Run before key sports kickoff times:

```cron
# Run at 5pm and 7pm every Saturday and Sunday (before UFL/UFC)
0 17,19 * * 6,7 cd /path/to/pfr-player-scraper && source venv/bin/activate && python capture_betting_splits.py --league ufl

# Or for UFC:
0 17,19 * * 6,7 cd /path/to/pfr-player-scraper && source venv/bin/activate && python capture_betting_splits.py --league ufc
```

This captures snapshots throughout the day, building a complete market movement history for analysis.
