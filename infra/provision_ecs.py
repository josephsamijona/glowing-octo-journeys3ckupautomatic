#!/usr/bin/env python3
"""
Idempotent AWS ECS infrastructure provisioner for S3 Backup Flow.

Run once manually, or let the CI/CD pipeline call it on every deploy.
Every operation is safe to re-run: existing resources are detected and
left untouched; only missing resources are created.

Resources managed
─────────────────
  1.  ECR repository        (scan-on-push + lifecycle policy)
  2.  ECS cluster           (Fargate + Container Insights)
  3.  IAM execution role    (ECS → ECR + CloudWatch)
  4.  IAM task role         (least-privilege: S3 + DynamoDB)
  5.  OIDC CICD role        (self-heal: attach ELB + CloudFront policies)
  6.  CloudWatch log group  (30-day retention)
  7.  Security group        (ports 80 + 8000 inbound)
  8.  Application Load Balancer + Target Group + Listener
  9.  CloudFront distribution → ALB origin
  10. ECS task definitions + Fargate services (API wired to ALB)
  11. Cognito / DynamoDB / S3  (delegated to scripts/provision_aws.py)

Usage
─────
  export ECR_IMAGE=123456789012.dkr.ecr.us-east-1.amazonaws.com/jhbridge/s3-backup-flow:abc123
  export DB_URL=mysql://...
  python infra/provision_ecs.py
"""

import json
import logging
import os
import subprocess
import sys
import time

import boto3
from botocore.exceptions import ClientError

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("provision")

# ─── Configuration ────────────────────────────────────────────────────────────
REGION     = os.environ.get("AWS_REGION", "us-east-1")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")
ECR_REPO   = os.environ.get("ECR_REPOSITORY", "jhbridge/s3-backup-flow")
IMAGE_URI  = os.environ.get("ECR_IMAGE", "")
CLUSTER    = os.environ.get("ECS_CLUSTER", "jhbridge-backup")
LOG_GROUP  = "/ecs/jhbridge-backup"

ALB_NAME   = "jhbridge-backup-alb"
TG_NAME    = "jhbridge-backup-tg"
CF_COMMENT = "jhbridge-s3-backup-flow"

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


# ─── AWS clients ──────────────────────────────────────────────────────────────
def _clients():
    kw = {"region_name": REGION}
    return {
        "sts":        boto3.client("sts",        **kw),
        "ecr":        boto3.client("ecr",        **kw),
        "ecs":        boto3.client("ecs",        **kw),
        "iam":        boto3.client("iam",        **kw),
        "logs":       boto3.client("logs",       **kw),
        "ec2":        boto3.client("ec2",        **kw),
        "elb":        boto3.client("elbv2",      **kw),
        # CloudFront is a global service — always us-east-1
        "cloudfront": boto3.client("cloudfront", region_name="us-east-1"),
    }


def _env_pairs() -> list[dict]:
    return [
        {"name": k, "value": v}
        for k in _APP_ENV_KEYS
        if (v := os.environ.get(k, ""))
    ]


def _sg_allow(ec2, sg_id: str, port: int, desc: str):
    """Add an inbound TCP rule — silently skip if already exists."""
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": "tcp",
                "FromPort": port, "ToPort": port,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": desc}],
            }],
        )
        log.info("[EC2]    Opened port %d (%s)", port, desc)
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise
        log.info("[EC2]    Port %d already open", port)


# ═══════════════════════════════════════════════════════════════════════════════
# Step 1 — ECR repository
# ═══════════════════════════════════════════════════════════════════════════════
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
        log.info("[ECR] ✚  Created: %s", ECR_REPO)

    ecr.put_lifecycle_policy(
        repositoryName=ECR_REPO,
        lifecyclePolicyText=json.dumps({"rules": [
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
        ]}),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Step 2 — ECS cluster
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_cluster(ecs):
    resp = ecs.describe_clusters(clusters=[CLUSTER])
    if any(c["status"] == "ACTIVE" for c in resp["clusters"]):
        log.info("[ECS] ✔  Cluster %s already exists", CLUSTER)
        return
    ecs.create_cluster(
        clusterName=CLUSTER,
        capacityProviders=["FARGATE", "FARGATE_SPOT"],
        defaultCapacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1}],
        settings=[{"name": "containerInsights", "value": "enabled"}],
    )
    log.info("[ECS] ✚  Created cluster: %s", CLUSTER)


# ═══════════════════════════════════════════════════════════════════════════════
# Step 3 — IAM roles (ECS execution + task) and OIDC CICD role self-heal
# ═══════════════════════════════════════════════════════════════════════════════
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

    task_arn = _upsert_role(
        iam,
        name="jhbridge-backup-task-role",
        managed=[],
        inline={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "S3BackupBucket",
                    "Effect": "Allow",
                    "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket", "s3:HeadObject"],
                    "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
                },
                {
                    "Sid": "DynamoBackupTasks",
                    "Effect": "Allow",
                    "Action": [
                        "dynamodb:PutItem", "dynamodb:GetItem",
                        "dynamodb:UpdateItem", "dynamodb:Scan",
                    ],
                    "Resource": f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{table}",
                },
            ],
        },
    )
    return exec_arn, task_arn


def ensure_cicd_role_policies(iam):
    """
    Self-heal: attach ELB + CloudFront permissions to the OIDC CICD role
    so the pipeline can manage these resources without manual updates.
    """
    role_name = "jhbridge-github-oidc-role"
    needed = [
        "arn:aws:iam::aws:policy/ElasticLoadBalancingFullAccess",
        "arn:aws:iam::aws:policy/CloudFrontFullAccess",
    ]
    try:
        iam.get_role(RoleName=role_name)
    except iam.exceptions.NoSuchEntityException:
        log.warning("[IAM] OIDC role %s not found — skipping self-heal", role_name)
        return

    attached = {
        p["PolicyArn"]
        for p in iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
    }
    for policy_arn in needed:
        if policy_arn not in attached:
            iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
            log.info("[IAM] ✚  Attached to OIDC role: %s", policy_arn.split("/")[-1])
        else:
            log.info("[IAM] ✔  OIDC role already has: %s", policy_arn.split("/")[-1])


# ═══════════════════════════════════════════════════════════════════════════════
# Step 4 — CloudWatch log group
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_log_group(logs):
    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=30)
        log.info("[Logs] ✚  Created log group: %s (30d retention)", LOG_GROUP)
    except logs.exceptions.ResourceAlreadyExistsException:
        log.info("[Logs] ✔  Log group %s already exists", LOG_GROUP)


# ═══════════════════════════════════════════════════════════════════════════════
# Step 5 — Security group + VPC networking
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_networking(ec2) -> tuple[list[str], str, str]:
    """Returns (subnet_ids, sg_id, vpc_id)."""
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    vpc_id = vpcs["Vpcs"][0]["VpcId"]

    subnets = ec2.describe_subnets(Filters=[
        {"Name": "vpc-id",        "Values": [vpc_id]},
        {"Name": "defaultForAz",  "Values": ["true"]},
    ])
    subnet_ids = [s["SubnetId"] for s in subnets["Subnets"]][:2]

    sg_name = "jhbridge-backup-sg"
    existing = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [sg_name]},
        {"Name": "vpc-id",     "Values": [vpc_id]},
    ])["SecurityGroups"]

    if existing:
        sg_id = existing[0]["GroupId"]
        log.info("[EC2] ✔  Security group %s (%s)", sg_name, sg_id)
    else:
        sg_id = ec2.create_security_group(
            GroupName=sg_name,
            Description="JHBridge S3 Backup Flow — ALB + API",
            VpcId=vpc_id,
        )["GroupId"]
        log.info("[EC2] ✚  Created security group %s (%s)", sg_name, sg_id)

    # Ensure both ports are open (idempotent)
    _sg_allow(ec2, sg_id, 80,   "ALB HTTP")
    _sg_allow(ec2, sg_id, 8000, "ECS API")

    log.info("[EC2]    VPC: %s | Subnets: %s", vpc_id, subnet_ids)
    return subnet_ids, sg_id, vpc_id


# ═══════════════════════════════════════════════════════════════════════════════
# Step 6 — Application Load Balancer + Target Group + Listener
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_alb(elb, subnet_ids: list, vpc_id: str, sg_id: str) -> tuple[str, str, str]:
    """Returns (alb_arn, alb_dns, tg_arn)."""

    # ── ALB ───────────────────────────────────────────────────────────────────
    try:
        resp    = elb.describe_load_balancers(Names=[ALB_NAME])
        alb     = resp["LoadBalancers"][0]
        alb_arn = alb["LoadBalancerArn"]
        alb_dns = alb["DNSName"]
        log.info("[ALB] ✔  %s already exists → %s", ALB_NAME, alb_dns)
    except ClientError as e:
        if e.response["Error"]["Code"] != "LoadBalancerNotFound":
            raise
        resp    = elb.create_load_balancer(
            Name=ALB_NAME,
            Subnets=subnet_ids,
            SecurityGroups=[sg_id],
            Scheme="internet-facing",
            Type="application",
            IpAddressType="ipv4",
            Tags=[{"Key": "Project", "Value": CF_COMMENT}],
        )
        alb     = resp["LoadBalancers"][0]
        alb_arn = alb["LoadBalancerArn"]
        alb_dns = alb["DNSName"]
        log.info("[ALB] ✚  Created: %s → %s", ALB_NAME, alb_dns)

    # ── Target Group (type=ip, required for awsvpc) ────────────────────────────
    try:
        resp   = elb.describe_target_groups(Names=[TG_NAME])
        tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]
        log.info("[ALB] ✔  Target group %s already exists", TG_NAME)
    except ClientError as e:
        if e.response["Error"]["Code"] != "TargetGroupNotFound":
            raise
        resp   = elb.create_target_group(
            Name=TG_NAME,
            Protocol="HTTP",
            Port=8000,
            VpcId=vpc_id,
            TargetType="ip",
            HealthCheckProtocol="HTTP",
            HealthCheckPath="/api/v1/health",
            HealthCheckIntervalSeconds=30,
            HealthyThresholdCount=2,
            UnhealthyThresholdCount=3,
            Tags=[{"Key": "Project", "Value": CF_COMMENT}],
        )
        tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]
        log.info("[ALB] ✚  Created target group: %s", TG_NAME)

    # ── Listener port 80 → forward to target group ─────────────────────────────
    listeners  = elb.describe_listeners(LoadBalancerArn=alb_arn)["Listeners"]
    port_80    = [l for l in listeners if l["Port"] == 80]
    if port_80:
        log.info("[ALB] ✔  Listener port 80 already exists")
    else:
        elb.create_listener(
            LoadBalancerArn=alb_arn,
            Protocol="HTTP",
            Port=80,
            DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        )
        log.info("[ALB] ✚  Created listener port 80 → %s", TG_NAME)

    return alb_arn, alb_dns, tg_arn


# ═══════════════════════════════════════════════════════════════════════════════
# Step 7 — CloudFront distribution → ALB origin
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_cloudfront(cf, alb_dns: str) -> tuple[str, str]:
    """
    Returns (distribution_id, cloudfront_domain).
    Idempotent: finds existing distribution by ALB origin domain.
    """

    # ── Check existing distributions ──────────────────────────────────────────
    marker = ""
    while True:
        kwargs = {"MaxItems": "100"}
        if marker:
            kwargs["Marker"] = marker
        resp      = cf.list_distributions(**kwargs)
        dist_list = resp.get("DistributionList", {})

        for dist in dist_list.get("Items", []):
            for origin in dist.get("Origins", {}).get("Items", []):
                if origin.get("DomainName") == alb_dns:
                    log.info(
                        "[CF]  ✔  Distribution already exists: %s → %s",
                        dist["Id"], dist["DomainName"],
                    )
                    return dist["Id"], dist["DomainName"]

        if not dist_list.get("IsTruncated"):
            break
        marker = dist_list["NextMarker"]

    # ── Create distribution ───────────────────────────────────────────────────
    caller_ref = f"jhbridge-backup-{int(time.time())}"
    resp  = cf.create_distribution(
        DistributionConfig={
            "CallerReference": caller_ref,
            "Comment":         CF_COMMENT,
            "Enabled":         True,
            "HttpVersion":     "http2and3",
            "IsIPV6Enabled":   True,
            "PriceClass":      "PriceClass_100",   # US / EU / Canada only
            "Origins": {
                "Quantity": 1,
                "Items": [{
                    "Id":         "alb-origin",
                    "DomainName": alb_dns,
                    "CustomOriginConfig": {
                        "HTTPPort":               80,
                        "HTTPSPort":              443,
                        "OriginProtocolPolicy":   "http-only",
                        "OriginReadTimeout":       60,
                        "OriginKeepaliveTimeout":  60,
                    },
                }],
            },
            "DefaultCacheBehavior": {
                "TargetOriginId":      "alb-origin",
                "ViewerProtocolPolicy": "redirect-to-https",
                "AllowedMethods": {
                    "Quantity": 7,
                    "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                    "CachedMethods": {
                        "Quantity": 2,
                        "Items": ["GET", "HEAD"],
                    },
                },
                # AWS managed: CachingDisabled (dynamic API — no caching)
                "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
                # AWS managed: AllViewer (forward all headers/cookies/qs to origin)
                "OriginRequestPolicyId": "216adef6-5c7f-47e4-b989-5492eafa07d3",
                "Compress": True,
            },
        }
    )
    dist       = resp["Distribution"]
    dist_id    = dist["Id"]
    cf_domain  = dist["DomainName"]
    log.info("[CF]  ✚  Created distribution: %s", dist_id)
    log.info("[CF]     Domain  : https://%s", cf_domain)
    log.info("[CF]     Status  : Deploying (5-15 min to propagate globally)")
    return dist_id, cf_domain


# ═══════════════════════════════════════════════════════════════════════════════
# Step 8 — ECS task definitions + services
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_services(
    ecs,
    exec_arn: str,
    task_arn: str,
    subnet_ids: list,
    sg_id: str,
    tg_arn: str,
):
    env_pairs = _env_pairs()

    for svc_name, cfg in _SERVICES.items():
        is_api = cfg["public_port"]

        container: dict = {
            "name":        "app",
            "image":       IMAGE_URI,
            "essential":   True,
            "command":     cfg["cmd"],
            "environment": env_pairs,
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group":         LOG_GROUP,
                    "awslogs-region":        REGION,
                    "awslogs-stream-prefix": svc_name,
                },
            },
            "readonlyRootFilesystem": False,
            "privileged":             False,
        }

        if is_api:
            container["portMappings"] = [{"containerPort": 8000, "protocol": "tcp"}]
            container["healthCheck"] = {
                "command":     ["CMD-SHELL", "curl -sf http://localhost:8000/api/v1/health || exit 1"],
                "interval":    30,
                "timeout":     10,
                "retries":     3,
                "startPeriod": 60,
            }

        resp     = ecs.register_task_definition(
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
                "subnets":        subnet_ids,
                "securityGroups": [sg_id],
                "assignPublicIp": "ENABLED",
            }
        }

        existing = ecs.describe_services(cluster=CLUSTER, services=[svc_name])
        active   = [s for s in existing["services"] if s["status"] == "ACTIVE"]

        if active:
            svc       = active[0]
            has_lb    = bool(svc.get("loadBalancers"))

            # API service: if it was created without LB (first deploy before ALB),
            # drain it and recreate so CloudFront traffic flows through the ALB.
            if is_api and not has_lb:
                log.info("[ECS] ↻  API service has no LB — draining and recreating")
                ecs.update_service(cluster=CLUSTER, service=svc_name, desiredCount=0)
                ecs.delete_service(cluster=CLUSTER, service=svc_name, force=True)
                _create_service(ecs, svc_name, task_def, cfg, net_cfg, tg_arn if is_api else None)
            else:
                ecs.update_service(
                    cluster=CLUSTER,
                    service=svc_name,
                    taskDefinition=task_def,
                    desiredCount=cfg["desired"],
                    forceNewDeployment=True,
                )
                log.info("[ECS] ↻  Updated service: %s", svc_name)
        else:
            _create_service(ecs, svc_name, task_def, cfg, net_cfg, tg_arn if is_api else None)


def _create_service(ecs, svc_name: str, task_def: str, cfg: dict, net_cfg: dict, tg_arn: str | None):
    kwargs: dict = {
        "cluster":              CLUSTER,
        "serviceName":          svc_name,
        "taskDefinition":       task_def,
        "desiredCount":         cfg["desired"],
        "launchType":           "FARGATE",
        "networkConfiguration": net_cfg,
        "enableECSManagedTags": True,
        "propagateTags":        "SERVICE",
    }
    if tg_arn:
        kwargs["loadBalancers"] = [{
            "targetGroupArn": tg_arn,
            "containerName":  "app",
            "containerPort":  8000,
        }]
        kwargs["healthCheckGracePeriodSeconds"] = 120
    ecs.create_service(**kwargs)
    log.info("[ECS] ✚  Created service: %s%s", svc_name, " (wired to ALB)" if tg_arn else "")


# ═══════════════════════════════════════════════════════════════════════════════
# Step 9 — Cognito / DynamoDB / S3  (delegated)
# ═══════════════════════════════════════════════════════════════════════════════
def provision_aws_resources():
    script = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "scripts", "provision_aws.py")
    )
    if not os.path.exists(script):
        log.warning("[AWS] scripts/provision_aws.py not found — skipping")
        return
    result = subprocess.run([sys.executable, script], capture_output=True, text=True)
    if result.stdout:
        log.info("[AWS] %s", result.stdout.strip())
    if result.returncode != 0:
        log.warning("[AWS] provision_aws.py warnings: %s", result.stderr.strip())


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    global ACCOUNT_ID, IMAGE_URI

    c = _clients()

    if not ACCOUNT_ID:
        ACCOUNT_ID = c["sts"].get_caller_identity()["Account"]
    if not IMAGE_URI:
        IMAGE_URI = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}:latest"

    log.info("=" * 65)
    log.info("JHBridge S3 Backup Flow — Provisioner")
    log.info("  Account : %s", ACCOUNT_ID)
    log.info("  Region  : %s", REGION)
    log.info("  Image   : %s", IMAGE_URI)
    log.info("  Cluster : %s", CLUSTER)
    log.info("=" * 65)

    log.info("\n[1/9] ECR repository")
    ensure_ecr(c["ecr"])

    log.info("\n[2/9] ECS cluster")
    ensure_cluster(c["ecs"])

    log.info("\n[3/9] IAM roles + OIDC role self-heal")
    exec_arn, task_arn = ensure_iam(c["iam"])
    ensure_cicd_role_policies(c["iam"])

    log.info("\n[4/9] CloudWatch log group")
    ensure_log_group(c["logs"])

    log.info("\n[5/9] VPC networking + security group")
    subnet_ids, sg_id, vpc_id = ensure_networking(c["ec2"])

    log.info("\n[6/9] Application Load Balancer")
    alb_arn, alb_dns, tg_arn = ensure_alb(c["elb"], subnet_ids, vpc_id, sg_id)

    log.info("\n[7/9] CloudFront distribution")
    cf_dist_id, cf_domain = ensure_cloudfront(c["cloudfront"], alb_dns)

    log.info("\n[8/9] ECS task definitions + services")
    ensure_services(c["ecs"], exec_arn, task_arn, subnet_ids, sg_id, tg_arn)

    log.info("\n[9/9] Cognito / DynamoDB / S3 resources")
    provision_aws_resources()

    log.info("\n" + "=" * 65)
    log.info("✅  Provisioning complete.")
    log.info("   ALB     : http://%s", alb_dns)
    log.info("   CF Dist : %s", cf_dist_id)
    log.info("   CF URL  : https://%s", cf_domain)
    log.info("   (CloudFront propagation takes 5-15 min on first deploy)")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
