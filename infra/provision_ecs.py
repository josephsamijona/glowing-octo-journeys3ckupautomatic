#!/usr/bin/env python3
"""
Idempotent AWS ECS infrastructure provisioner for S3 Backup Flow.

Run once manually, or let the CI/CD pipeline call it on every deploy.
Every operation is safe to re-run: existing resources are detected and
left untouched; only missing resources are created.

Resources managed
─────────────────
  1. ECR repository (with scan-on-push + lifecycle policy)
  2. ECS cluster (Fargate, Container Insights enabled)
  3. IAM execution role  (lets ECS pull images & write CloudWatch logs)
  4. IAM task role       (least-privilege: S3 bucket + DynamoDB table)
  5. CloudWatch log group (30-day retention)
  6. Security group      (port 8000 inbound, egress unrestricted)
  7. ECS task definitions — one per service (api / worker / beat)
  8. ECS Fargate services

Prerequisites
─────────────
  • AWS credentials with enough permissions (see OIDC_ROLE_ARN in deploy.yml)
  • All environment variables listed in _APP_ENV_KEYS must be set

Usage
─────
  export ECR_IMAGE=123456789012.dkr.ecr.us-east-1.amazonaws.com/jhbridge/s3-backup-flow:abc123
  export DB_URL=mysql://...
  ...
  python infra/provision_ecs.py
"""

import json
import logging
import os
import subprocess
import sys

import boto3
from botocore.exceptions import ClientError

# ─── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("provision")

# ─── Configuration (all from environment) ─────────────────────────────────
REGION = os.environ.get("AWS_REGION", "us-east-1")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")
ECR_REPO = os.environ.get("ECR_REPOSITORY", "jhbridge/s3-backup-flow")
IMAGE_URI = os.environ.get("ECR_IMAGE", "")
CLUSTER = os.environ.get("ECS_CLUSTER", "jhbridge-backup")
LOG_GROUP = "/ecs/jhbridge-backup"

# Per-service ECS task family names
_SERVICES = {
    "jhbridge-s3-backup-api": {
        "cmd": [
            "uvicorn", "app.main:app",
            "--host", "0.0.0.0", "--port", "8000",
            "--workers", "2", "--log-level", "info",
        ],
        "cpu": "512", "mem": "1024",
        "desired": 1, "public_port": True,
    },
    "jhbridge-s3-backup-worker": {
        "cmd": [
            "celery", "-A", "app.worker.celery_app.celery_app",
            "worker", "--loglevel=info", "--concurrency=2",
        ],
        "cpu": "512", "mem": "1024",
        "desired": 1, "public_port": False,
    },
    "jhbridge-s3-backup-beat": {
        "cmd": [
            "celery", "-A", "app.worker.celery_app.celery_app",
            "beat", "--loglevel=info",
        ],
        "cpu": "256", "mem": "512",
        "desired": 1, "public_port": False,
    },
}

# App env vars injected into every ECS task
_APP_ENV_KEYS = [
    "APP_ENV", "SECRET_KEY",
    "DB_URL", "REDIS_URL", "CELERY_BROKER_URL",
    "COGNITO_USER_POOL_ID", "COGNITO_APP_CLIENT_ID", "COGNITO_REGION",
    "EXTERNAL_BACKEND_SECRET_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION",
    "DYNAMODBTABLE", "S3_BUCKET_NAME",
    "RESEND_API_KEY", "EMAIL_FROM", "ADMIN_EMAIL",
    "ALLOWED_ORIGINS",
]

# ─── AWS clients ──────────────────────────────────────────────────────────
def _clients():
    kw = {"region_name": REGION}
    return {
        "sts":  boto3.client("sts",  **kw),
        "ecr":  boto3.client("ecr",  **kw),
        "ecs":  boto3.client("ecs",  **kw),
        "iam":  boto3.client("iam",  **kw),
        "logs": boto3.client("logs", **kw),
        "ec2":  boto3.client("ec2",  **kw),
    }


# ─── Helper ───────────────────────────────────────────────────────────────
def _env_pairs() -> list[dict]:
    """Return [{name, value}] for all non-empty app env vars."""
    return [
        {"name": k, "value": v}
        for k in _APP_ENV_KEYS
        if (v := os.environ.get(k, ""))
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Step 1 — ECR repository
# ═══════════════════════════════════════════════════════════════════════════
def ensure_ecr(ecr):
    try:
        ecr.describe_repositories(repositoryNames=[ECR_REPO])
        log.info("[ECR] ✔  %s already exists", ECR_REPO)
    except ecr.exceptions.RepositoryNotFoundException:
        ecr.create_repository(
            repositoryName=ECR_REPO,
            imageScanningConfiguration={"scanOnPush": True},
            encryptionConfiguration={"encryptionType": "AES256"},
        )
        log.info("[ECR] ✚  Created repository: %s", ECR_REPO)

    # Lifecycle policy: purge untagged after 1 day, keep ≤10 tagged images
    ecr.put_lifecycle_policy(
        repositoryName=ECR_REPO,
        lifecyclePolicyText=json.dumps({
            "rules": [
                {
                    "rulePriority": 1,
                    "description": "Delete untagged images after 1 day",
                    "selection": {
                        "tagStatus": "untagged",
                        "countType": "sinceImagePushed",
                        "countUnit": "days", "countNumber": 1,
                    },
                    "action": {"type": "expire"},
                },
                {
                    "rulePriority": 2,
                    "description": "Keep only last 10 images",
                    "selection": {
                        "tagStatus": "any",
                        "countType": "imageCountMoreThan",
                        "countNumber": 10,
                    },
                    "action": {"type": "expire"},
                },
            ]
        }),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Step 2 — ECS cluster
# ═══════════════════════════════════════════════════════════════════════════
def ensure_cluster(ecs):
    resp = ecs.describe_clusters(clusters=[CLUSTER])
    active = [c for c in resp["clusters"] if c["status"] == "ACTIVE"]
    if active:
        log.info("[ECS] ✔  Cluster %s already exists", CLUSTER)
        return
    ecs.create_cluster(
        clusterName=CLUSTER,
        capacityProviders=["FARGATE", "FARGATE_SPOT"],
        defaultCapacityProviderStrategy=[
            {"capacityProvider": "FARGATE", "weight": 1},
        ],
        settings=[{"name": "containerInsights", "value": "enabled"}],
    )
    log.info("[ECS] ✚  Created cluster: %s", CLUSTER)


# ═══════════════════════════════════════════════════════════════════════════
# Step 3 — IAM roles
# ═══════════════════════════════════════════════════════════════════════════
_TRUST = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
})


def _upsert_role(iam, name: str, managed: list[str], inline: dict | None = None) -> str:
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        log.info("[IAM] ✔  Role %s already exists", name)
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=_TRUST,
            Description="JHBridge S3 Backup Flow",
        )["Role"]["Arn"]
        log.info("[IAM] ✚  Created role: %s", name)

    for policy_arn in managed:
        try:
            iam.attach_role_policy(RoleName=name, PolicyArn=policy_arn)
        except ClientError:
            pass

    if inline:
        iam.put_role_policy(
            RoleName=name,
            PolicyName=f"{name}-inline",
            PolicyDocument=json.dumps(inline),
        )
    return arn


def ensure_iam(iam) -> tuple[str, str]:
    exec_arn = _upsert_role(
        iam,
        name="jhbridge-backup-exec-role",
        managed=["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"],
    )

    bucket = os.environ.get("S3_BUCKET_NAME", "jhbridge-mysql-backups")
    table  = os.environ.get("DYNAMODBTABLE", "BackupTasks")

    task_inline = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "S3BackupBucket",
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket", "s3:HeadObject"],
                "Resource": [
                    f"arn:aws:s3:::{bucket}",
                    f"arn:aws:s3:::{bucket}/*",
                ],
            },
            {
                "Sid": "DynamoBackupTasks",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:PutItem", "dynamodb:GetItem",
                    "dynamodb:UpdateItem", "dynamodb:Scan",
                ],
                "Resource": (
                    f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{table}"
                ),
            },
        ],
    }
    task_arn = _upsert_role(
        iam,
        name="jhbridge-backup-task-role",
        managed=[],
        inline=task_inline,
    )
    return exec_arn, task_arn


# ═══════════════════════════════════════════════════════════════════════════
# Step 4 — CloudWatch log group
# ═══════════════════════════════════════════════════════════════════════════
def ensure_log_group(logs):
    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=30)
        log.info("[Logs] ✚  Created log group: %s (30-day retention)", LOG_GROUP)
    except logs.exceptions.ResourceAlreadyExistsException:
        log.info("[Logs] ✔  Log group %s already exists", LOG_GROUP)


# ═══════════════════════════════════════════════════════════════════════════
# Step 5 — Security group + VPC networking
# ═══════════════════════════════════════════════════════════════════════════
def ensure_networking(ec2) -> tuple[list[str], str]:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    vpc_id = vpcs["Vpcs"][0]["VpcId"]

    subnets = ec2.describe_subnets(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "defaultForAz", "Values": ["true"]},
    ])
    subnet_ids = [s["SubnetId"] for s in subnets["Subnets"]][:2]

    sg_name = "jhbridge-backup-sg"
    existing = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [sg_name]},
        {"Name": "vpc-id", "Values": [vpc_id]},
    ])["SecurityGroups"]

    if existing:
        sg_id = existing[0]["GroupId"]
        log.info("[EC2] ✔  Security group %s (%s)", sg_name, sg_id)
    else:
        sg_id = ec2.create_security_group(
            GroupName=sg_name,
            Description="JHBridge S3 Backup Flow — API port",
            VpcId=vpc_id,
        )["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 8000, "ToPort": 8000,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTP API"}],
                },
            ],
        )
        log.info("[EC2] ✚  Created security group %s (%s)", sg_name, sg_id)

    log.info("[EC2]    Subnets: %s", subnet_ids)
    return subnet_ids, sg_id


# ═══════════════════════════════════════════════════════════════════════════
# Step 6 — Task definitions + ECS services
# ═══════════════════════════════════════════════════════════════════════════
def ensure_services(ecs, exec_arn: str, task_arn: str, subnet_ids: list, sg_id: str):
    env_pairs = _env_pairs()

    for svc_name, cfg in _SERVICES.items():
        container = {
            "name": "app",
            "image": IMAGE_URI,
            "essential": True,
            "command": cfg["cmd"],
            "environment": env_pairs,
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": LOG_GROUP,
                    "awslogs-region": REGION,
                    "awslogs-stream-prefix": svc_name,
                },
            },
            "readonlyRootFilesystem": False,
            "privileged": False,
        }

        # API gets a port mapping and a healthcheck
        if cfg["public_port"]:
            container["portMappings"] = [{"containerPort": 8000, "protocol": "tcp"}]
            container["healthCheck"] = {
                "command": [
                    "CMD-SHELL",
                    "curl -sf http://localhost:8000/api/v1/health || exit 1",
                ],
                "interval": 30,
                "timeout": 10,
                "retries": 3,
                "startPeriod": 60,
            }

        resp = ecs.register_task_definition(
            family=svc_name,
            taskRoleArn=task_arn,
            executionRoleArn=exec_arn,
            networkMode="awsvpc",
            containerDefinitions=[container],
            requiresCompatibilities=["FARGATE"],
            cpu=cfg["cpu"],
            memory=cfg["mem"],
        )
        task_def = f"{svc_name}:{resp['taskDefinition']['revision']}"
        log.info("[ECS] ✚  Registered task def: %s", task_def)

        net_cfg = {
            "awsvpcConfiguration": {
                "subnets": subnet_ids,
                "securityGroups": [sg_id],
                "assignPublicIp": "ENABLED",
            }
        }

        # Create or update the service
        existing = ecs.describe_services(cluster=CLUSTER, services=[svc_name])
        active = [s for s in existing["services"] if s["status"] == "ACTIVE"]

        if active:
            ecs.update_service(
                cluster=CLUSTER,
                service=svc_name,
                taskDefinition=task_def,
                desiredCount=cfg["desired"],
                forceNewDeployment=True,
            )
            log.info("[ECS] ↻  Updated service: %s", svc_name)
        else:
            ecs.create_service(
                cluster=CLUSTER,
                serviceName=svc_name,
                taskDefinition=task_def,
                desiredCount=cfg["desired"],
                launchType="FARGATE",
                networkConfiguration=net_cfg,
                enableECSManagedTags=True,
                propagateTags="SERVICE",
            )
            log.info("[ECS] ✚  Created service: %s", svc_name)


# ═══════════════════════════════════════════════════════════════════════════
# Step 7 — Delegate to existing provision_aws.py (Cognito / DynamoDB / S3)
# ═══════════════════════════════════════════════════════════════════════════
def provision_aws_resources():
    script = os.path.join(os.path.dirname(__file__), "..", "scripts", "provision_aws.py")
    script = os.path.normpath(script)
    if not os.path.exists(script):
        log.warning("[AWS] scripts/provision_aws.py not found — skipping Cognito/DDB/S3 setup")
        return
    result = subprocess.run([sys.executable, script], capture_output=True, text=True)
    if result.stdout:
        log.info("[AWS] %s", result.stdout.strip())
    if result.returncode != 0:
        log.warning("[AWS] provision_aws.py warnings: %s", result.stderr.strip())


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════
def main():
    global ACCOUNT_ID, IMAGE_URI

    c = _clients()

    if not ACCOUNT_ID:
        ACCOUNT_ID = c["sts"].get_caller_identity()["Account"]

    if not IMAGE_URI:
        IMAGE_URI = (
            f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}:latest"
        )

    log.info("=" * 60)
    log.info("JHBridge S3 Backup Flow — ECS Provisioner")
    log.info("  Account : %s", ACCOUNT_ID)
    log.info("  Region  : %s", REGION)
    log.info("  Image   : %s", IMAGE_URI)
    log.info("  Cluster : %s", CLUSTER)
    log.info("=" * 60)

    log.info("\n[1/7] ECR repository")
    ensure_ecr(c["ecr"])

    log.info("\n[2/7] ECS cluster")
    ensure_cluster(c["ecs"])

    log.info("\n[3/7] IAM roles")
    exec_arn, task_arn = ensure_iam(c["iam"])

    log.info("\n[4/7] CloudWatch log group")
    ensure_log_group(c["logs"])

    log.info("\n[5/7] VPC networking")
    subnet_ids, sg_id = ensure_networking(c["ec2"])

    log.info("\n[6/7] ECS task definitions + services")
    ensure_services(c["ecs"], exec_arn, task_arn, subnet_ids, sg_id)

    log.info("\n[7/7] Cognito / DynamoDB / S3 resources")
    provision_aws_resources()

    log.info("\n✅  Provisioning complete.")


if __name__ == "__main__":
    main()
