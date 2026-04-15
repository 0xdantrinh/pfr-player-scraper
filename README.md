# Pro Football Reference Player Scraper

Scrapes player statistics from Pro-Football-Reference using FlareSolverr to bypass Cloudflare protections.

## Features
- Fetch full player stat tables (passing, rushing, receiving, defense, etc.)
- Uses FlareSolverr Docker service
- Outputs structured JSON
- Designed for AWS hosting (EC2 / ECS)

## Stack
- Python 3.11
- requests
- BeautifulSoup
- FlareSolverr

## Setup

### 1. Run FlareSolverr

```bash
docker run -d -p 8191:8191 --name flaresolverr ghcr.io/flaresolverr/flaresolverr:latest
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run scraper

```bash
python scraper.py https://www.pro-football-reference.com/players/B/BradTo00.htm
```

## Output

JSON file with all parsed stat tables.

## AWS Deployment

Recommended:
- EC2 instance
- Docker for FlareSolverr
- Python service for scraping


## AWS Fargate Deployment

1. Build image
```
docker build -t pfr-scraper .
```

2. Push to ECR
```
aws ecr create-repository --repository-name pfr-scraper

aws ecr get-login-password --region <region> |
  docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com


docker tag pfr-scraper:latest <account>.dkr.ecr.<region>.amazonaws.com/pfr-scraper:latest

docker push <account>.dkr.ecr.<region>.amazonaws.com/pfr-scraper:latest
```

3. Register ECS tasks
```
aws ecs register-task-definition --cli-input-json file://aws/ecs/flaresolverr-task.json
aws ecs register-task-definition --cli-input-json file://aws/ecs/scraper-task.json
```

4. Run workers
```
aws ecs run-task --cluster scraper-cluster --launch-type FARGATE --task-definition pfr-scraper
```


## Queue + Storage Architecture

Scraping pipeline:

player URL
   ↓
SQS queue
   ↓
ECS scraper workers
   ↓
FlareSolverr
   ↓
S3 JSON storage

### Environment variables

Required for worker:

SQS_QUEUE_URL=<queue>
S3_BUCKET=<bucket>
AWS_REGION=<region>

### Example: enqueue players

```
cat player_urls.txt | python enqueue_players.py $SQS_QUEUE_URL
```

### S3 Output

Objects stored as:

```
players/<player_id>.json
```

Example:

```
players/BradTo00.json
```

