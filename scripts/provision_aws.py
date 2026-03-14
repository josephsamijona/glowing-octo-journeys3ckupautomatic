import boto3
import os
from dotenv import load_dotenv, set_key

# Load existing .env to get credentials
env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
load_dotenv(env_path)

def provision_resources():
    region = os.getenv('AWS_REGION', 'us-east-1')
    cognito = boto3.client('cognito-idp', region_name=region)
    dynamodb = boto3.client('dynamodb', region_name=region)

    print(f"Provisioning resources in {region}...")

    # 1. Provision Cognito User Pool
    try:
        pool_name = 's3-backup-user-pool'
        response = cognito.create_user_pool(
            PoolName=pool_name,
            Policies={
                'PasswordPolicy': {
                    'MinimumLength': 8,
                    'RequireUppercase': True,
                    'RequireLowercase': True,
                    'RequireNumbers': True,
                    'RequireSymbols': False
                }
            },
            AutoVerifiedAttributes=['email']
        )
        user_pool_id = response['UserPool']['Id']
        print(f"Created Cognito User Pool: {user_pool_id}")
    except Exception as e:
        print(f"Error creating User Pool (might already exist): {e}")
        # Try to find existing
        pools = cognito.list_user_pools(MaxResults=60)['UserPools']
        user_pool_id = next((p['Id'] for p in pools if p['Name'] == pool_name), None)

    # 2. Provision Cognito App Client
    if user_pool_id:
        try:
            client_name = 's3-backup-app-client'
            response = cognito.create_user_pool_client(
                UserPoolId=user_pool_id,
                ClientName=client_name,
                ExplicitAuthFlows=['ALLOW_USER_PASSWORD_AUTH', 'ALLOW_REFRESH_TOKEN_AUTH']
            )
            app_client_id = response['UserPoolClient']['ClientId']
            print(f"Created Cognito App Client: {app_client_id}")
        except Exception as e:
            print(f"Error creating App Client: {e}")
            clients = cognito.list_user_pool_clients(UserPoolId=user_pool_id)['UserPoolClients']
            app_client_id = next((c['ClientId'] for c in clients if c['ClientName'] == client_name), None)
    else:
        app_client_id = None

    # 3. Provision DynamoDB Table
    table_name = 'BackupTasks'
    try:
        dynamodb.describe_table(TableName=table_name)
        print(f"DynamoDB Table {table_name} already exists.")
    except dynamodb.exceptions.ResourceNotFoundException:
        try:
            response = dynamodb.create_table(
                TableName=table_name,
                KeySchema=[{'AttributeName': 'TaskId', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'TaskId', 'AttributeType': 'S'}],
                # Provisioned throughput 5 RCU/WCU is well within the 25 units allowed in the AWS Free Tier.
                ProvisionedThroughput={'ReadCapacityUnits': 5, 'WriteCapacityUnits': 5}
            )
            print(f"Provisioning DynamoDB Table: {table_name}")
            # Wait for table to be created
            waiter = dynamodb.get_waiter('table_exists')
            waiter.wait(TableName=table_name)
            print(f"DynamoDB Table {table_name} is active.")
        except Exception as e:
            print(f"Error creating DynamoDB table: {e}")
    except Exception as e:
        print(f"Error checking DynamoDB table: {e}")

    # 4. Verify S3 Bucket
    s3 = boto3.client('s3', region_name=region)
    bucket_name = 'jhbridge-mysql-backups'
    try:
        s3.head_bucket(Bucket=bucket_name)
        print(f"S3 Bucket {bucket_name} already exists.")
    except Exception as e:
        print(f"S3 Bucket {bucket_name} does not exist or is inaccessible. Creating it...")
        try:
            if region == 'us-east-1':
                s3.create_bucket(Bucket=bucket_name)
            else:
                s3.create_bucket(
                    Bucket=bucket_name,
                    CreateBucketConfiguration={'LocationConstraint': region}
                )
            print(f"Created S3 Bucket: {bucket_name}")
        except Exception as create_e:
            print(f"Error creating S3 bucket: {create_e}")

    # Update .env
    if user_pool_id:
        set_key(env_path, "COGNITO_USER_POOL_ID", user_pool_id)
    if app_client_id:
        set_key(env_path, "COGNITO_APP_CLIENT_ID", app_client_id)
    set_key(env_path, "DynamoDBtable", table_name)
    set_key(env_path, "s3_bucketbackupname", bucket_name)
    set_key(env_path, "S3_BUCKET_NAME", bucket_name) # Ensure compatibility with config.py
    
    print("\n.env file updated with new resource IDs.")

if __name__ == "__main__":
    provision_resources()
