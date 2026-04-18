import os
import json
import time
import random
import boto3
from scraper import fetch_page, parse_page
from cfb_scraper import parse_page as parse_cfb_page

SQS_URL = os.environ.get("SQS_QUEUE_URL")
S3_BUCKET = os.environ.get("S3_BUCKET")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

sqs = boto3.client("sqs", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")
SESSION_ID = "pfr"


def ensure_session():
    try:
        import requests
        payload = {"cmd": "sessions.create", "session": SESSION_ID}
        r = requests.post(FLARESOLVERR_URL, json=payload, timeout=30)
        if r.status_code == 200:
            print("FlareSolverr session ensured:", SESSION_ID)
    except Exception as e:
        print("session create error", e)


def destroy_session():
    try:
        import requests
        payload = {"cmd": "sessions.destroy", "session": SESSION_ID}
        requests.post(FLARESOLVERR_URL, json=payload, timeout=30)
        print("FlareSolverr session destroyed:", SESSION_ID)
    except Exception as e:
        print("session destroy error", e)


def process_message(msg):
    url = msg["Body"]

    html = fetch_page(url)

    if "/cfb/players/" in url:
        data = parse_cfb_page(html, url)
        slug = data.get("player_id")
        key = f"college/{slug}.json"
    else:
        data = parse_page(html, url)
        player_id = data.get("player_id")
        key = f"players/{player_id}.json"

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(data).encode("utf-8"),
        ContentType="application/json"
    )

    print("uploaded", key)


def loop():
    while True:
        resp = sqs.receive_message(
            QueueUrl=SQS_URL,
            MaxNumberOfMessages=2,
            WaitTimeSeconds=10
        )

        msgs = resp.get("Messages", [])

        for m in msgs:
            try:
                process_message(m)

                sqs.delete_message(
                    QueueUrl=SQS_URL,
                    ReceiptHandle=m["ReceiptHandle"]
                )

            except Exception as e:
                print("error", e)

        time.sleep(random.uniform(0.5,2.0))


if __name__ == "__main__":
    ensure_session()
    try:
        loop()
    finally:
        destroy_session()
