import os
import json
import time
import logging
import random
import boto3
from scraper import fetch_page, parse_page

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

logging.info(f"Connected to SQS queue: {SQS_URL}")
logging.info(f"S3 bucket: {S3_BUCKET}")
logging.info(f"AWS region: {AWS_REGION}")

def process_message(msg):
    url = msg["Body"]
    logging.info(f"Processing message: {url}")

    html = fetch_page(url)
    data = parse_page(html, url)

    player_id = data.get("player_id")

    key = f"players/{player_id}.json"

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
    logging.info("PFR Player Scraper Worker starting...")
    try:
        loop()
    except KeyboardInterrupt:
        logging.info("Worker stopped by user")
