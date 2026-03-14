#!/usr/bin/env python3
"""
setup_iam.py — Étape 1 & 3 du setup JHBridge S3 Backup Flow

  Étape 1 : OIDC Identity Provider GitHub + IAM Role pour GitHub Actions
  Étape 3 : IAM User runtime pour les containers ECS (S3 + DynamoDB)

Résultats sauvegardés dans .env automatiquement.

Usage:
    python scripts/setup_iam.py
"""

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv, set_key

# ─── Config ───────────────────────────────────────────────────────────────────
GITHUB_ORG  = "synapsbranch-ux"
GITHUB_REPO = "automatics3backup"

OIDC_ROLE_NAME = "jhbridge-github-oidc-role"
APP_USER_NAME  = "jhbridge-backup-app-user"

# Permissions pour le rôle GitHub Actions (CI/CD)
CICD_MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess",
    "arn:aws:iam::aws:policy/AmazonECS_FullAccess",
    "arn:aws:iam::aws:policy/IAMFullAccess",
    "arn:aws:iam::aws:policy/CloudWatchLogsFullAccess",
    "arn:aws:iam::aws:policy/AmazonEC2FullAccess",
    "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
    "arn:aws:iam::aws:policy/AmazonS3FullAccess",
    "arn:aws:iam::aws:policy/AmazonCognitoPowerUser",
]

# Permissions pour le user runtime des containers ECS
APP_USER_MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/AmazonS3FullAccess",
    "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
    "arn:aws:iam::aws:policy/AmazonSESFullAccess",
]

# ─── Load .env ────────────────────────────────────────────────────────────────
env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(env_path)

REGION     = os.getenv("AWS_REGION", "us-east-1")
ACCOUNT_ID = ""  # résolu via STS


# ─── Helpers ──────────────────────────────────────────────────────────────────
def log(msg: str):
    print(msg)


def save_env(key: str, value: str):
    set_key(env_path, key, value)
    log(f"  → .env updated: {key}")


# ═══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 1A — OIDC Identity Provider GitHub
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_oidc_provider(iam) -> str:
    """Crée le provider OIDC GitHub si absent. Retourne l'ARN."""
    provider_url = "https://token.actions.githubusercontent.com"
    expected_arn = f"arn:aws:iam::{ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"

    log("\n[1A] OIDC Identity Provider GitHub Actions")

    # Vérifie si déjà existant
    try:
        iam.get_open_id_connect_provider(OpenIDConnectProviderArn=expected_arn)
        log(f"  ✔  Provider déjà existant: {expected_arn}")
        return expected_arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    # Récupère le thumbprint du certificat GitHub
    import urllib.request
    import ssl
    import hashlib

    hostname = "token.actions.githubusercontent.com"
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(f"https://{hostname}/.well-known/openid-configuration", context=ctx) as _:
        pass

    conn = ctx.wrap_socket(
        __import__("socket").create_connection((hostname, 443)),
        server_hostname=hostname,
    )
    cert_der = conn.getpeercert(binary_form=True)
    conn.close()
    thumbprint = hashlib.sha1(cert_der).hexdigest()  # noqa: S324

    log(f"  Thumbprint certificat: {thumbprint}")

    resp = iam.create_open_id_connect_provider(
        Url=provider_url,
        ClientIDList=["sts.amazonaws.com"],
        ThumbprintList=[thumbprint],
    )
    arn = resp["OpenIDConnectProviderArn"]
    log(f"  ✚  Créé: {arn}")
    return arn


# ═══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 1B — IAM Role pour GitHub Actions (OIDC)
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_oidc_role(iam, provider_arn: str) -> str:
    """Crée le rôle OIDC pour GitHub Actions. Retourne l'ARN."""
    log(f"\n[1B] IAM Role GitHub OIDC → {OIDC_ROLE_NAME}")

    trust_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Federated": provider_arn,
                },
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                    },
                    "StringLike": {
                        "token.actions.githubusercontent.com:sub": (
                            f"repo:{GITHUB_ORG}/{GITHUB_REPO}:*"
                        ),
                    },
                },
            }
        ],
    })

    # Crée ou récupère le rôle
    try:
        role_arn = iam.get_role(RoleName=OIDC_ROLE_NAME)["Role"]["Arn"]
        log(f"  ✔  Rôle déjà existant: {role_arn}")
        # Met à jour la trust policy au cas où
        iam.update_assume_role_policy(
            RoleName=OIDC_ROLE_NAME,
            PolicyDocument=trust_policy,
        )
        log("  ↻  Trust policy mise à jour")
    except iam.exceptions.NoSuchEntityException:
        role_arn = iam.create_role(
            RoleName=OIDC_ROLE_NAME,
            AssumeRolePolicyDocument=trust_policy,
            Description="GitHub Actions OIDC role pour JHBridge S3 Backup Flow",
            MaxSessionDuration=3600,
        )["Role"]["Arn"]
        log(f"  ✚  Rôle créé: {role_arn}")

    # Attache les managed policies
    attached = {
        p["PolicyArn"]
        for p in iam.list_attached_role_policies(RoleName=OIDC_ROLE_NAME)["AttachedPolicies"]
    }
    for policy_arn in CICD_MANAGED_POLICIES:
        if policy_arn not in attached:
            iam.attach_role_policy(RoleName=OIDC_ROLE_NAME, PolicyArn=policy_arn)
            log(f"  + Politique attachée: {policy_arn.split('/')[-1]}")
        else:
            log(f"  ✔  Déjà attachée: {policy_arn.split('/')[-1]}")

    return role_arn


# ═══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 3 — IAM User runtime pour les containers ECS
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_app_user(iam) -> tuple[str, str]:
    """Crée le user IAM runtime + access keys. Retourne (access_key, secret_key)."""
    log(f"\n[3] IAM User runtime → {APP_USER_NAME}")

    # Crée ou récupère le user
    try:
        iam.get_user(UserName=APP_USER_NAME)
        log(f"  ✔  User déjà existant: {APP_USER_NAME}")
    except iam.exceptions.NoSuchEntityException:
        iam.create_user(
            UserName=APP_USER_NAME,
            Tags=[{"Key": "Project", "Value": "jhbridge-s3-backup-flow"}],
        )
        log(f"  ✚  User créé: {APP_USER_NAME}")

    # Attache les managed policies
    attached = {
        p["PolicyArn"]
        for p in iam.list_attached_user_policies(UserName=APP_USER_NAME)["AttachedPolicies"]
    }
    for policy_arn in APP_USER_MANAGED_POLICIES:
        if policy_arn not in attached:
            iam.attach_user_policy(UserName=APP_USER_NAME, PolicyArn=policy_arn)
            log(f"  + Politique attachée: {policy_arn.split('/')[-1]}")
        else:
            log(f"  ✔  Déjà attachée: {policy_arn.split('/')[-1]}")

    # Gère les access keys — max 2 par user
    existing_keys = iam.list_access_keys(UserName=APP_USER_NAME)["AccessKeyMetadata"]

    if len(existing_keys) >= 2:
        # Supprime la plus ancienne
        oldest = min(existing_keys, key=lambda k: k["CreateDate"])
        iam.delete_access_key(UserName=APP_USER_NAME, AccessKeyId=oldest["AccessKeyId"])
        log(f"  ↻  Ancienne clé supprimée: {oldest['AccessKeyId']}")
        existing_keys = [k for k in existing_keys if k["AccessKeyId"] != oldest["AccessKeyId"]]

    if existing_keys:
        log(f"  ⚠  Une clé existante trouvée ({existing_keys[0]['AccessKeyId']})")
        log("     Impossible de récupérer le secret d'une clé existante.")
        log("     Suppression et recréation...")
        iam.delete_access_key(
            UserName=APP_USER_NAME,
            AccessKeyId=existing_keys[0]["AccessKeyId"],
        )

    # Crée une nouvelle paire de clés
    key = iam.create_access_key(UserName=APP_USER_NAME)["AccessKey"]
    access_key = key["AccessKeyId"]
    secret_key = key["SecretAccessKey"]
    log(f"  ✚  Nouvelles clés créées: {access_key}")

    return access_key, secret_key


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    global ACCOUNT_ID

    log("=" * 60)
    log("JHBridge — Setup IAM (Étapes 1 & 3)")
    log(f"  Repo  : {GITHUB_ORG}/{GITHUB_REPO}")
    log(f"  Region: {REGION}")
    log("=" * 60)

    iam = boto3.client("iam", region_name=REGION)
    sts = boto3.client("sts", region_name=REGION)

    # Résout l'account ID
    ACCOUNT_ID = sts.get_caller_identity()["Account"]
    log(f"\n  AWS Account: {ACCOUNT_ID}")

    # ── Étape 1 ────────────────────────────────────────────────────────────────
    provider_arn = ensure_oidc_provider(iam)
    oidc_role_arn = ensure_oidc_role(iam, provider_arn)

    # ── Étape 3 ────────────────────────────────────────────────────────────────
    app_access_key, app_secret_key = ensure_app_user(iam)

    # ── Sauvegarde dans .env ───────────────────────────────────────────────────
    log("\n[.env] Mise à jour")
    save_env("OIDC_ROLE_ARN", oidc_role_arn)
    save_env("AWS_ACCOUNT_ID", ACCOUNT_ID)
    save_env("AWS_APP_ACCESS_KEY_ID", app_access_key)
    save_env("AWS_APP_SECRET_ACCESS_KEY", app_secret_key)

    # ── Résumé ─────────────────────────────────────────────────────────────────
    log("\n" + "=" * 60)
    log("✅  Setup terminé !")
    log("")
    log("GitHub Secrets à ajouter (Settings → Secrets → Actions):")
    log(f"  OIDC_ROLE_ARN              = {oidc_role_arn}")
    log(f"  AWS_ACCOUNT_ID             = {ACCOUNT_ID}")
    log(f"  AWS_APP_ACCESS_KEY_ID      = {app_access_key}")
    log(f"  AWS_APP_SECRET_ACCESS_KEY  = {app_secret_key}")
    log("")
    log("Ces valeurs sont aussi sauvegardées dans .env")
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except ClientError as e:
        print(f"\n❌  Erreur AWS: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAnnulé.", file=sys.stderr)
        sys.exit(1)
