import hashlib
import hmac
import secrets
from typing import Optional


PBKDF2_ITERATIONS = 390000
DEFAULT_ADMIN_SALT = "makerhub_default_admin"


def hash_password(password: str, salt: Optional[str] = None) -> str:
    secret = str(password or "")
    salt_value = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt_value.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt_value}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    raw_hash = str(stored_hash or "").strip()
    if not raw_hash:
        return False

    try:
        algorithm, iteration_raw, salt_value, digest = raw_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iteration_raw)
    except ValueError:
        return False

    calculated = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        salt_value.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(calculated, digest)


def default_admin_password_hash() -> str:
    return hash_password("admin", salt=DEFAULT_ADMIN_SALT)


def hash_api_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def generate_api_token(prefix: str = "mht") -> str:
    clean_prefix = "".join(char for char in str(prefix or "mht").lower() if char.isalnum())[:8] or "mht"
    return f"{clean_prefix}_{secrets.token_urlsafe(24)}"


def generate_session_id() -> str:
    return secrets.token_urlsafe(32)
