"""Sealing personal data with the server's PII key (15 ต.ค. 2569 Q16, Q36).

Roster e-mails (to send a one-time code) and roster files waiting for their import are sealed
with PII_SECRET_KEY, a Fernet key that lives on the server and its standby only. A copy of the
database or a backup without that key does not show them. Staging and production must set the
key; without it nothing new is sealed and the e-mail code is not offered. Local and CI derive a
key from SECRET_KEY so the demo works.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class SealingUnavailable(Exception):
    """No PII key on this server."""


def _fernet():
    key = settings.PII_SECRET_KEY
    if not key:
        if settings.APP_ENV in {"staging", "production"}:
            raise SealingUnavailable("PII_SECRET_KEY is not set on this server.")
        digest = hashlib.sha256(f"pii-seal:{settings.SECRET_KEY}".encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def available():
    try:
        _fernet()
    except (SealingUnavailable, ValueError):
        return False
    return True


def seal(text):
    return _fernet().encrypt(text.encode()).decode()


def unseal(token):
    """The text, or "" when it cannot be opened (another key, or damaged)."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, SealingUnavailable, ValueError):
        return ""
