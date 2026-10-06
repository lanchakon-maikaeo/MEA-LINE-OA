import os
import secrets
from pathlib import Path

path = Path(".env")
if not path.exists():
    password = secrets.token_urlsafe(32)
    content = (
        "APP_ENV=local\n"
        f"DJANGO_SECRET_KEY={secrets.token_urlsafe(64)}\n"
        f"DATABASE_URL=postgresql://mea_demo:{password}@127.0.0.1:55432/mea_demo\n"
        f"POSTGRES_PASSWORD={password}\n"
        "ALLOWED_HOSTS=localhost,127.0.0.1\n"
        "CSRF_TRUSTED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000\n"
        "AI_ENABLED=false\nOUTBOUND_ENABLED=false\n"
    )
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(content)
    print("Created private local .env (credentials are not printed).")
else:
    print("Keeping existing .env.")
