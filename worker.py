import os
import json
import time
import boto3
from scraper import scrape_player, fetch_page, parse_tables

SQS_URL = os.environ.get("SQS_QUEUE_URL")
S3_BUCKET = os.environ.get("S3_BUCKET")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

sqs = boto3.client("sqs", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)

def process_message(msg):
    url = msg["Body"]

    html = fetch_page(url)
    data = parse_tables(html)

    player_id = url.split("/")[-1].replace(".htm","")

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
            MaxNumberOfMessages=5,
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

        time.sleep(1)


if __name__ == "__main__":
    loop()
