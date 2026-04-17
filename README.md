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
├── scraper.py
├── worker.py
├── enqueue_players.py
├── requirements.txt
├── Dockerfile
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

