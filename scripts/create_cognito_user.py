#!/usr/bin/env python3
"""
create_cognito_user.py — Crée un user admin dans Cognito + affiche les credentials

Usage:
    python scripts/create_cognito_user.py
    python scripts/create_cognito_user.py --email toi@example.com --password MonPassword1!
"""

import argparse
import os
import sys

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(env_path)

REGION        = os.getenv("AWS_REGION", "us-east-1")
USER_POOL_ID  = os.getenv("COGNITO_USER_POOL_ID", "").strip("'\"")
CLIENT_ID     = os.getenv("COGNITO_APP_CLIENT_ID", "").strip("'\"")
API_KEY       = os.getenv("EXTERNAL_BACKEND_SECRET_TOKEN", "").strip("'\"")
APP_URL       = "http://localhost:8000"


def create_user(email: str, password: str):
    cognito = boto3.client("cognito-idp", region_name=REGION)

    print("=" * 60)
    print("Création du user Cognito")
    print(f"  Pool   : {USER_POOL_ID}")
    print(f"  Email  : {email}")
    print("=" * 60)

    # 1. Crée le user (admin, pas besoin de vérification email)
    try:
        cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email",          "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            TemporaryPassword=password,
            MessageAction="SUPPRESS",   # pas d'email de bienvenue AWS
        )
        print(f"\n  ✚  User créé: {email}")
    except cognito.exceptions.UsernameExistsException:
        print(f"\n  ✔  User déjà existant: {email}")

    # 2. Set permanent password (évite le FORCE_CHANGE_PASSWORD au login)
    cognito.admin_set_user_password(
        UserPoolId=USER_POOL_ID,
        Username=email,
        Password=password,
        Permanent=True,
    )
    print("  ✔  Password permanent défini")

    # 3. Test — obtient un token pour vérifier que ça marche
    print("\n  Test login...")
    try:
        resp = cognito.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
            ClientId=CLIENT_ID,
        )
        token = resp["AuthenticationResult"]["IdToken"]
        access_token = resp["AuthenticationResult"]["AccessToken"]
        print("  ✔  Login OK — token obtenu")
    except ClientError as e:
        print(f"  ✗  Login failed: {e}")
        token = None
        access_token = None

    # 4. Résumé
    print("\n" + "=" * 60)
    print("✅  Credentials pour l'app")
    print("=" * 60)
    print(f"\n  URL         : {APP_URL}")
    print(f"  Login page  : {APP_URL}/login")
    print(f"\n  Email       : {email}")
    print(f"  Password    : {password}")
    print(f"\n  API Key     : {API_KEY}")
    print(f"  (header X-API-KEY pour appels machine-to-machine)")

    if token:
        print(f"\n  JWT Token (expire ~1h):")
        print(f"  {token[:80]}...")
        print(f"\n  Test rapide:")
        print(f'  curl -H "Authorization: Bearer {token[:40]}..." \\')
        print(f"       {APP_URL}/api/v1/health")

    print("\n" + "=" * 60)
    print("Swagger docs : http://localhost:8000/api/docs")
    print("  → Clic 'Authorize' → colle le JWT token")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email",    default="admin@jhbridgetranslation.com")
    parser.add_argument("--password", default="JHBridge2024!")
    args = parser.parse_args()

    if not USER_POOL_ID:
        print("❌  COGNITO_USER_POOL_ID manquant dans .env", file=sys.stderr)
        sys.exit(1)
    if not CLIENT_ID:
        print("❌  COGNITO_APP_CLIENT_ID manquant dans .env", file=sys.stderr)
        sys.exit(1)

    try:
        create_user(args.email, args.password)
    except ClientError as e:
        print(f"\n❌  Erreur AWS: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
