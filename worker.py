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

if not SQS_URL:
    raise ValueError("SQS_QUEUE_URL environment variable is required")
if not S3_BUCKET:
    raise ValueError("S3_BUCKET environment variable is required")

sqs = boto3.client("sqs", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)


def _name_matches_pfr_id(name: str, pfr_id: str) -> bool:
    """Check that a scraped player name is consistent with the pfrId encoding.

    PFR encodes: first-4-of-last + first-2-of-first + 2-digit-suffix.
    e.g. HerbJu00 → last starts "Herb", first starts "Ju".
    Returns True if both fragments appear in the name, or if the format
    is unrecognizable (fail open so we don't block legacy messages).
    """
    if not pfr_id or not name or len(pfr_id) < 6:
        return True
    last_frag  = pfr_id[:4].lower()
    first_frag = pfr_id[4:6].lower()
    name_lower = name.lower().replace("-", "").replace("'", "").replace(".", "")
    parts      = name_lower.split()
    last_ok    = any(p.startswith(last_frag) for p in parts)
    first_ok   = any(p.startswith(first_frag) for p in parts)
    return last_ok and first_ok


def process_message(msg):
    # Message body may be a plain URL string (legacy) or a JSON object:
    #   { "url": "...", "pfrId"?: "...", "teamAbbr"?: "HOU",
    #     "seasonYear"?: 2026, "league"?: "ufl" }
    raw_body = msg["Body"]
    try:
        msg_meta = json.loads(raw_body)
        if isinstance(msg_meta, dict) and "url" in msg_meta:
            url = msg_meta["url"]
        else:
            url = str(msg_meta)
            msg_meta = {}
    except (json.JSONDecodeError, TypeError):
        url = raw_body
        msg_meta = {}

    pfr_player_id = msg_meta.get("pfrId")
    team_abbr     = msg_meta.get("teamAbbr")
    season_year   = msg_meta.get("seasonYear")
    league        = msg_meta.get("league")
    player_name   = msg_meta.get("playerName")

    logging.info(f"Processing: {url} (player={player_name}, team={team_abbr}, year={season_year})")

    html = fetch_page(url)

    if "/cfb/players/" in url:
        data = parse_cfb_page(html, url)
        slug = data.get("player_id")
        key  = f"college/{slug}.json"

        # Guard: wrong CFB disambiguation returns a player with the same name
        # but a different position (e.g. DB named "Justin Fields" vs the QB).
        # Skip upload if the name doesn't match the pfrId or there are no
        # passing stats (which every QB page should have).
        if pfr_player_id:
            scraped_name  = data.get("player_info", {}).get("name", "")
            passing_rows  = data.get("stats", {}).get("passing_standard", [])
            if not _name_matches_pfr_id(scraped_name, pfr_player_id):
                logging.warning(
                    f"SKIP {key}: name '{scraped_name}' doesn't match pfrId {pfr_player_id}"
                )
                return
            if not passing_rows:
                logging.warning(
                    f"SKIP {key}: no passing stats — wrong CFB disambiguation for {pfr_player_id} (try a different -N suffix)"
                )
                return

    else:
        data      = parse_page(html, url)
        player_id = data.get("player_id")
        key       = f"players/{player_id}.json"

        # Guard: Cloudflare sometimes returns a cached page for a different player.
        # Skip upload if the scraped name doesn't match the pfrId.
        if pfr_player_id:
            scraped_name = data.get("player_info", {}).get("name", "")
            if scraped_name and not _name_matches_pfr_id(scraped_name, pfr_player_id):
                logging.warning(
                    f"SKIP {key}: name '{scraped_name}' doesn't match pfrId {pfr_player_id} — wrong page, will retry"
                )
                return

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

        time.sleep(random.uniform(0.5, 2.0))


if __name__ == "__main__":
    loop()
