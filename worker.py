import os
from dotenv import load_dotenv
import json
import time
import logging
import random
import subprocess
import requests
import boto3
from scraper import fetch_page, parse_page, PageNotFoundError, FLARESOLVERR_URL, PFR_SESSION, CFB_SESSION
from cfb_scraper import parse_page as parse_cfb_page

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

SQS_URL = os.environ.get("SQS_QUEUE_URL")
S3_BUCKET = os.environ.get("S3_BUCKET")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Rotate FlareSolverr sessions every N successful scrapes to prevent Cloudflare
# cookie poisoning (accumulated cf_clearance cookies get flagged after ~100-200 requests).
SESSION_ROTATE_EVERY   = int(os.environ.get("SESSION_ROTATE_EVERY", "150"))
FLARESOLVERR_CONTAINER = os.environ.get("FLARESOLVERR_CONTAINER", "flaresolverr")
# After a docker restart, sleep this many seconds before the next request.
# Gives Cloudflare's IP rate-limit time to relax between recovery attempts.
POST_RESTART_SLEEP = int(os.environ.get("POST_RESTART_SLEEP", "3"))
# Normal inter-request delay range (seconds). Increase if hitting rate limits.
DELAY_MIN = float(os.environ.get("DELAY_MIN", "3.0"))
DELAY_MAX = float(os.environ.get("DELAY_MAX", "8.0"))

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
    # UUID-format IDs (sportradarId) are not name-encoded — skip the check
    if '-' in pfr_id:
        return True
    last_frag  = pfr_id[:4].lower()
    first_frag = pfr_id[4:6].lower().replace(".", "")  # C.J./T.J. → "c"/"t" not "c."/"t."
    # Replace hyphen with space so "Jones-Drew" → ["jones", "drew"], not ["jonesdrew"]
    name_lower = name.lower().replace("'", "").replace(".", "")
    name_lower = name_lower.replace("-", " ")
    parts      = name_lower.split()
    # last_frag check: also handle x-padded short last names (Cox→Coxx, Nix→Nixx)
    # p.startswith(last_frag) handles normal case; last_frag.startswith(p) handles padding
    last_ok    = any(p.startswith(last_frag) or last_frag.startswith(p) for p in parts)
    first_ok   = any(p.startswith(first_frag) for p in parts)
    # Accept if EITHER fragment matches — handles legal name changes (Davis→Williams)
    # and nicknames (Beanie Wells). The pfr_id URL is already the unique key; this check
    # is only a guard against Cloudflare serving a cached wrong-player page.
    return last_ok or first_ok


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

        # Guard: only skip if the scraped name doesn't match the pfrId.
        # Never filter by position or stats presence — a TE with only receiving
        # stats, a blocker with no stats, or any other edge case is still valid.
        if pfr_player_id:
            scraped_name = data.get("player_info", {}).get("name", "")
            if not _name_matches_pfr_id(scraped_name, pfr_player_id):
                logging.warning(
                    f"SKIP {key}: name '{scraped_name}' doesn't match pfrId {pfr_player_id}"
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


def _restart_flaresolverr():
    """Restart the FlareSolverr container — kills Chrome process tree, fresh fingerprint.

    Called immediately on the first FlareSolverr failure. Each challenge timeout wastes
    120 seconds, so there's no value in retrying before restarting.
    """
    logging.warning("FlareSolverr error — restarting container '%s' ...", FLARESOLVERR_CONTAINER)
    try:
        subprocess.run(
            ["docker", "restart", FLARESOLVERR_CONTAINER],
            check=True, capture_output=True, timeout=30,
        )
        logging.info("FlareSolverr restarted — sleeping %ds before next request", POST_RESTART_SLEEP)
        time.sleep(POST_RESTART_SLEEP)
    except Exception as e:
        logging.error("docker restart failed (non-fatal): %s", e)


def loop():
    logging.info("Starting worker loop... (session rotate every %d ok, restart on first error)", SESSION_ROTATE_EVERY)
    messages_processed = 0

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

                messages_processed += 1
                if messages_processed % SESSION_ROTATE_EVERY == 0:
                    logging.info("Rotating sessions after %d messages", messages_processed)
                    for session in (PFR_SESSION, CFB_SESSION):
                        try:
                            requests.post(FLARESOLVERR_URL,
                                          json={"cmd": "sessions.destroy", "session": session},
                                          timeout=15)
                        except Exception:
                            pass

            except PageNotFoundError as e:
                # 404s are expected — discard quietly without restarting
                logging.debug(f"404 skip: {e}")
                sqs.delete_message(
                    QueueUrl=SQS_URL,
                    ReceiptHandle=m["ReceiptHandle"]
                )

            except Exception as e:
                logging.error(f"Error processing message: {e}", exc_info=True)
                _restart_flaresolverr()

        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


if __name__ == "__main__":
    loop()
