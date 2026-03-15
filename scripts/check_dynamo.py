"""Check if the BackupTasks DynamoDB table exists and is accessible."""
import os

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from dotenv import load_dotenv

load_dotenv()

TABLE_NAME = os.getenv("DYNAMODBTABLE", "BackupTasks")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

dynamodb = boto3.client(
    "dynamodb",
    region_name=AWS_REGION,
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
)

try:
    resp = dynamodb.describe_table(TableName=TABLE_NAME)
    table = resp["Table"]
    print(f"✓ Table '{TABLE_NAME}' EXISTS")
    print(f"  Status      : {table['TableStatus']}")
    print(f"  Item count  : {table.get('ItemCount', 0)}")
    print(f"  Size (bytes): {table.get('TableSizeBytes', 0)}")
    print(f"  Region      : {AWS_REGION}")
    keys = [f"{k['AttributeName']} ({k['KeyType']})" for k in table["KeySchema"]]
    print(f"  Keys        : {', '.join(keys)}")

except ClientError as e:
    code = e.response["Error"]["Code"]
    if code == "ResourceNotFoundException":
        print(f"✗ Table '{TABLE_NAME}' does NOT exist in {AWS_REGION}")
        print("  → You need to create it on AWS DynamoDB console or via IaC")
    elif code in ("AccessDeniedException", "AuthFailure"):
        print(f"✗ Access denied — check AWS credentials")
        print(f"  Error: {e}")
    else:
        print(f"✗ AWS error: {code} — {e}")

except NoCredentialsError:
    print("✗ No AWS credentials found")
