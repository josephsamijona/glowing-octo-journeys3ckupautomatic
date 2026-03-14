#!/usr/bin/env python3
"""
push_github_secrets.py — Pousse tous les secrets + variables dans GitHub Actions

Usage:
    pip install requests PyNaCl python-dotenv
    python scripts/push_github_secrets.py --token ghp_xxxxxxxxxxxx

Le token doit avoir le scope: repo
Génère-le ici: https://github.com/settings/tokens/new
"""

import argparse
import base64
import os
import sys

import requests
from dotenv import load_dotenv
from nacl import encoding, public

# ─── Config ───────────────────────────────────────────────────────────────────
GITHUB_ORG  = "synapsbranch-ux"
GITHUB_REPO = "automatics3backup"

env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(env_path)

def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip("'\"")

# Mapping: GitHub Secret name → valeur depuis .env
SECRETS = {
    "OIDC_ROLE_ARN":                  _env("OIDC_ROLE_ARN"),
    "AWS_ACCOUNT_ID":                 _env("AWS_ACCOUNT_ID"),
    "APP_SECRET_KEY":                 _env("SECRET_KEY"),
    "DB_URL":                         _env("DB_URL"),
    "REDIS_URL":                      _env("REDIS_URL"),
    "CELERY_BROKER_URL":              _env("CELERY_BROKER_URL"),
    "COGNITO_USER_POOL_ID":           _env("COGNITO_USER_POOL_ID"),
    "COGNITO_APP_CLIENT_ID":          _env("COGNITO_APP_CLIENT_ID"),
    "EXTERNAL_BACKEND_SECRET_TOKEN":  _env("EXTERNAL_BACKEND_SECRET_TOKEN"),
    "AWS_APP_ACCESS_KEY_ID":          _env("AWS_APP_ACCESS_KEY_ID"),
    "AWS_APP_SECRET_ACCESS_KEY":      _env("AWS_APP_SECRET_ACCESS_KEY"),
    "DYNAMODBTABLE":                  _env("DynamoDBtable", "BackupTasks"),
    "S3_BUCKET_NAME":                 _env("S3_BUCKET_NAME", "jhbridge-mysql-backups"),
    "RESEND_API_KEY":                 _env("RESEND_API_KEY"),
    "EMAIL_FROM":                     _env("EMAIL_FROM"),
    "ADMIN_EMAIL":                    _env("ADMIN_EMAIL"),
}

# Mapping: GitHub Variable name → valeur
VARIABLES = {
    "AWS_REGION":       _env("AWS_REGION", "us-east-1"),
    "ALLOWED_ORIGINS":  _env("ALLOWED_ORIGINS", "*"),
}


# ─── GitHub API helpers ────────────────────────────────────────────────────────
def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _base_url() -> str:
    return f"https://api.github.com/repos/{GITHUB_ORG}/{GITHUB_REPO}"


def get_repo_public_key(token: str) -> tuple[str, str]:
    resp = requests.get(
        f"{_base_url()}/actions/secrets/public-key",
        headers=_headers(token),
    )
    resp.raise_for_status()
    data = resp.json()
    return data["key_id"], data["key"]


def encrypt_secret(public_key_b64: str, secret_value: str) -> str:
    """Chiffre la valeur avec la clé publique du repo (libsodium sealed box)."""
    pub_key = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder)
    sealed_box = public.SealedBox(pub_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def put_secret(token: str, key_id: str, pub_key: str, name: str, value: str):
    if not value:
        print(f"  ⚠  {name} — valeur vide, ignoré")
        return

    encrypted = encrypt_secret(pub_key, value)
    resp = requests.put(
        f"{_base_url()}/actions/secrets/{name}",
        headers=_headers(token),
        json={"encrypted_value": encrypted, "key_id": key_id},
    )
    if resp.status_code in (201, 204):
        print(f"  ✔  Secret: {name}")
    else:
        print(f"  ✗  Secret: {name} — {resp.status_code} {resp.text}")


def put_variable(token: str, name: str, value: str):
    if not value:
        print(f"  ⚠  {name} — valeur vide, ignoré")
        return

    # Tente un PATCH (update) d'abord, sinon POST (create)
    resp = requests.patch(
        f"{_base_url()}/actions/variables/{name}",
        headers=_headers(token),
        json={"name": name, "value": value},
    )
    if resp.status_code == 204:
        print(f"  ✔  Variable (update): {name}")
        return

    resp = requests.post(
        f"{_base_url()}/actions/variables",
        headers=_headers(token),
        json={"name": name, "value": value},
    )
    if resp.status_code == 201:
        print(f"  ✔  Variable (create): {name}")
    else:
        print(f"  ✗  Variable: {name} — {resp.status_code} {resp.text}")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True, help="GitHub PAT (scope: repo)")
    args = parser.parse_args()

    token = args.token

    print("=" * 60)
    print(f"Push GitHub Secrets → {GITHUB_ORG}/{GITHUB_REPO}")
    print("=" * 60)

    # Vérifie l'accès au repo
    resp = requests.get(f"{_base_url()}", headers=_headers(token))
    if resp.status_code == 401:
        print("❌  Token invalide ou expiré", file=sys.stderr)
        sys.exit(1)
    if resp.status_code == 404:
        print("❌  Repo introuvable ou token sans accès", file=sys.stderr)
        sys.exit(1)
    resp.raise_for_status()

    # Récupère la clé publique pour chiffrer les secrets
    key_id, pub_key = get_repo_public_key(token)

    # Pousse les secrets
    print("\n[Secrets]")
    for name, value in SECRETS.items():
        put_secret(token, key_id, pub_key, name, value)

    # Pousse les variables
    print("\n[Variables]")
    for name, value in VARIABLES.items():
        put_variable(token, name, value)

    # Vérifie les secrets vides
    missing = [k for k, v in SECRETS.items() if not v]
    if missing:
        print(f"\n⚠  Secrets non renseignés dans .env:")
        for m in missing:
            print(f"   - {m}")
        print("   Ajoute-les dans .env et relance le script.")

    print("\n✅  Done! Vérifie sur GitHub:")
    print(f"   https://github.com/{GITHUB_ORG}/{GITHUB_REPO}/settings/secrets/actions")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAnnulé.")
        sys.exit(1)
    except requests.HTTPError as e:
        print(f"\n❌  Erreur HTTP: {e}", file=sys.stderr)
        sys.exit(1)
