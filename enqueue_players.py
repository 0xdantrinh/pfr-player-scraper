import boto3
import sys

queue_url = sys.argv[1]

sqs = boto3.client("sqs")

for line in sys.stdin:
    url = line.strip()
    if not url:
        continue

    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=url
    )

    print("queued", url)
