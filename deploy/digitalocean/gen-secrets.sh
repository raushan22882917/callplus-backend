#!/usr/bin/env bash
# Prints fresh values for the generated secrets in .env. Needs python3 with `cryptography`
# for the Fernet key; falls back to openssl-based random bytes in the right format.
set -euo pipefail
rand() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
fernet() {
  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null \
    || openssl rand -base64 32 | tr '+/' '-_'
}
echo "POSTGRES_PASSWORD=$(rand 24)"
echo "REDIS_PASSWORD=$(rand 24)"
echo "JWT_SECRET_KEY=$(rand 48)"
echo "EXOTEL_WEBHOOK_SECRET=$(rand 32)"
echo "INTERNAL_SERVICE_TOKEN=$(rand 32)"
echo "ENCRYPTION_KEY=$(fernet)"
