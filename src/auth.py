import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

AUTH_PATH = Path(__file__).resolve().parent.parent / "auth.json"
PBKDF2_ITERATIONS = 600_000
DEFAULT_LOGIN = "cogitans"
DEFAULT_PASSWORD = "kilo123"


def _hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def ensure_auth_file() -> dict[str, Any]:
    data: dict[str, Any] = {}
    if AUTH_PATH.exists():
        try:
            loaded = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    valid = (
        isinstance(data.get("login"), str)
        and isinstance(data.get("salt"), str)
        and isinstance(data.get("hash"), str)
    )
    if not valid:
        salt = secrets.token_bytes(16)
        data = {
            "login": DEFAULT_LOGIN,
            "salt": salt.hex(),
            "iterations": PBKDF2_ITERATIONS,
            "hash": _hash_password(DEFAULT_PASSWORD, salt, PBKDF2_ITERATIONS).hex(),
        }
        try:
            AUTH_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
    return data


def verify_credentials(login: str, password: str) -> bool:
    data = ensure_auth_file()
    if login.strip().lower() != str(data.get("login", "")).strip().lower():
        return False
    try:
        salt = bytes.fromhex(str(data["salt"]))
        iterations = int(data.get("iterations") or PBKDF2_ITERATIONS)
        expected = bytes.fromhex(str(data["hash"]))
    except (KeyError, ValueError, TypeError):
        return False
    return secrets.compare_digest(_hash_password(password, salt, iterations), expected)


def change_password(current: str, new: str) -> bool:
    data = ensure_auth_file()
    try:
        salt = bytes.fromhex(str(data["salt"]))
        iterations = int(data.get("iterations") or PBKDF2_ITERATIONS)
        expected = bytes.fromhex(str(data["hash"]))
    except (KeyError, ValueError, TypeError):
        return False
    if not secrets.compare_digest(_hash_password(current, salt, iterations), expected):
        return False
    new_salt = secrets.token_bytes(16)
    data["salt"] = new_salt.hex()
    data["iterations"] = PBKDF2_ITERATIONS
    data["hash"] = _hash_password(new, new_salt, PBKDF2_ITERATIONS).hex()
    try:
        AUTH_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return False
    return True
