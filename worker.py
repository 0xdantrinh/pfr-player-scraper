import os
import json
import time
import logging
import random
import boto3
from scraper import fetch_page, parse_page
from cfb_scraper import parse_page as parse_cfb_page

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

SQS_URL = os.environ.get("SQS_QUEUE_URL")
S3_BUCKET = os.environ.get("S3_BUCKET")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Validate required environment variables
if not SQS_URL:
    raise ValueError("SQS_QUEUE_URL environment variable is required")
if not S3_BUCKET:
    raise ValueError("S3_BUCKET environment variable is required")

sqs = boto3.client("sqs", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)

def process_message(msg):
    # Message body may be a plain URL string (legacy enqueue_players.py) or a
    # JSON object produced by the enqueue-team Lambda:
    #   { "url": "...", "pfrId"?: "...", "cfbSlug"?: "...", "playerName"?: "...",
    #     "teamAbbr"?: "HOU", "seasonYear"?: 2026, "league"?: "ufl" }
    raw_body = msg["Body"]
    try:
        msg_meta = json.loads(raw_body)
        if isinstance(msg_meta, dict) and "url" in msg_meta:
            url = msg_meta["url"]
        else:
            # JSON but not our format (just a bare string wrapped in JSON)
            url = str(msg_meta)
            msg_meta = {}
    except (json.JSONDecodeError, TypeError):
        # Plain URL string — legacy format
        url = raw_body
        msg_meta = {}

    # Extract optional metadata that was embedded by enqueue-team
    pfr_player_id = msg_meta.get("pfrId")    # links CFB scrape to existing PFR record
    team_abbr     = msg_meta.get("teamAbbr") # e.g. "HOU" for UFL team
    season_year   = msg_meta.get("seasonYear")
    league        = msg_meta.get("league")
    player_name   = msg_meta.get("playerName")

    logging.info(f"Processing: {url} (player={player_name}, team={team_abbr}, year={season_year})")

    html = fetch_page(url)

    if "/cfb/players/" in url:
        data = parse_cfb_page(html, url)
        slug = data.get("player_id")
        key = f"college/{slug}.json"
    else:
        data = parse_page(html, url)
        player_id = data.get("player_id")
        key = f"players/{player_id}.json"

    # Embed SQS metadata into the stored JSON so transform-pfr-data Lambda can
    # use it to set the correct DynamoDB team GSI key for UFL players.
    # These fields are only present when enqueue-team sent them — they're absent
    # for legacy plain-URL messages.
    if pfr_player_id:
        data["pfr_player_id"] = pfr_player_id
    if team_abbr:
        data["team_abbr"] = team_abbr
    if season_year:
        data["season_year"] = int(season_year)
    if league:
        data["league"] = league

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(data).encode("utf-8"),
        ContentType="application/json"
    )

    logging.info(f"Uploaded {key}")


def loop():
    logging.info("Starting worker loop...")
    while True:
        resp = sqs.receive_message(
            QueueUrl=SQS_URL,
            MaxNumberOfMessages=2,
            WaitTimeSeconds=10
        )

        msgs = resp.get("Messages", [])
        
        if msgs:
            logging.info(f"Received {len(msgs)} message(s)")
        else:
            logging.debug("No messages in queue")

        for m in msgs:
            try:
                process_message(m)

                sqs.delete_message(
                    QueueUrl=SQS_URL,
                    ReceiptHandle=m["ReceiptHandle"]
                )

            except Exception as e:
                logging.error(f"Error processing message: {e}", exc_info=True)

        time.sleep(random.uniform(0.5,2.0))


if __name__ == "__main__":
    loop()
