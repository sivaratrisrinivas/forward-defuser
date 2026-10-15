"""Print fresh secrets for .env / Secret Manager."""

import secrets

from cryptography.fernet import Fernet

print(f"DATA_KEY={Fernet.generate_key().decode()}")
print(f"HASH_PEPPER={secrets.token_urlsafe(32)}")
print(f"TELEGRAM_WEBHOOK_SECRET={secrets.token_urlsafe(32)}")
print(f"WHATSAPP_VERIFY_TOKEN={secrets.token_urlsafe(24)}")
print(f"ADMIN_TOKEN={secrets.token_urlsafe(32)}")
