import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return base64.b64encode(salt + digest).decode()


def password_matches(password: str, saved: str) -> bool:
    try:
        value = base64.b64decode(saved, validate=True)
        return len(value) == 48 and hmac.compare_digest(
            value[16:], hashlib.pbkdf2_hmac("sha256", password.encode(), value[:16], 600_000)
        )
    except (ValueError, TypeError):
        return False


class Secrets:
    def __init__(self, key: str | None = None):
        self.key = key.encode() if key else Fernet.generate_key()
        self.fernet = Fernet(self.key)

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.fernet.decrypt(value.encode()).decode()
