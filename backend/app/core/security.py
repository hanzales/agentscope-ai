import hashlib
import hmac
import secrets


def issue_api_key() -> str:
    return f"asai_{secrets.token_urlsafe(32)}"


def hash_api_key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def verify_api_key(value: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(value), expected_hash)
