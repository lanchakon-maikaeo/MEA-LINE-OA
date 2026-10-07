import base64
import os
import re
import secrets
from pathlib import Path

path = Path(".env")
# Secrets added by later modules; generated once and never printed.
GENERATED = {
    "ORCHESTRATOR_TOKEN": lambda: secrets.token_urlsafe(48),
    "N8N_DB_PASSWORD": lambda: secrets.token_urlsafe(32),
    "N8N_ENCRYPTION_KEY": lambda: secrets.token_urlsafe(48),
    # Fernet key that encrypts AI provider API keys stored in the database (M05).
    "AI_SECRET_KEY": lambda: base64.urlsafe_b64encode(os.urandom(32)).decode(),
}
if not path.exists():
    password = secrets.token_urlsafe(32)
    content = (
        "APP_ENV=local\n"
        f"DJANGO_SECRET_KEY={secrets.token_urlsafe(64)}\n"
        f"DATABASE_URL=postgresql://mea_demo:{password}@127.0.0.1:55432/mea_demo\n"
        f"POSTGRES_PASSWORD={password}\n"
        "ALLOWED_HOSTS=localhost,127.0.0.1,web\n"
        "CSRF_TRUSTED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000\n"
        "AI_ENABLED=false\nOUTBOUND_ENABLED=false\n"
    )
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(content)
    print("Created private local .env (credentials are not printed).")
else:
    print("Keeping existing .env.")
text = path.read_text()
missing = [key for key in GENERATED if not re.search(rf"^{key}=\S", text, re.M)]
if missing:
    text = re.sub(rf"^({'|'.join(missing)})=\s*$\n?", "", text, flags=re.M)
    text = text.rstrip("\n") + "\n" + "".join(f"{k}={GENERATED[k]()}\n" for k in missing)
    path.write_text(text)
    print(f"Added generated values for: {', '.join(missing)}")
if not re.search(r"^ALLOWED_HOSTS=.*\bweb\b", text, re.M):
    # n8n reaches Django inside the Compose network as http://web:8000.
    path.write_text(re.sub(r"^(ALLOWED_HOSTS=.*)$", r"\1,web", text, count=1, flags=re.M))
    print("Added 'web' to ALLOWED_HOSTS.")
