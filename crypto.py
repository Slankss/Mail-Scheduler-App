"""Symmetric encryption for secrets stored in the database (e.g. SMTP password).

The app must send the real SMTP/IMAP password to the mail server, so a
one-way hash is not usable here - it could never be turned back into the
plaintext needed to log in. Encryption is the reversible alternative: the
password sits in the database as ciphertext, and is only decrypted in memory
right before it's used to connect.

Uses Fernet (AES-128-CBC + HMAC, via the `cryptography` package) with a key
derived from APP_SECRET_KEY in the environment.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()

_SECRET = os.environ.get("APP_SECRET_KEY", "")
if not _SECRET:
    raise RuntimeError(
        "APP_SECRET_KEY tanimli degil. .env dosyasina rastgele bir deger ekleyin, orn:\n"
        '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
    )

# Fernet requires a 32-byte urlsafe-base64 key; derive one from APP_SECRET_KEY
# so that variable can be any plain string.
_KEY = base64.urlsafe_b64encode(hashlib.sha256(_SECRET.encode()).digest())
_fernet = Fernet(_KEY)


def secret_material() -> bytes:
    """APP_SECRET_KEY'in ham hali.

    Baska anahtarlar (orn. oturum cookie imzasi) bundan turetilir; ayni sirrin
    iki farkli isde ayni bicimde kullanilmamasi icin turetim cagiran tarafta
    yapilir.
    """
    return _SECRET.encode()


def encrypt(value: str) -> str:
    """Encrypt a plaintext string for storage. Empty input stays empty."""
    if not value:
        return ""
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """Decrypt a value previously produced by encrypt().

    Returns the input unchanged if it isn't a valid Fernet token - this keeps
    passwords saved before encryption was introduced (plaintext) working: the
    app can still use them, and the next save re-encrypts them.
    """
    if not value:
        return ""
    try:
        return _fernet.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return value
