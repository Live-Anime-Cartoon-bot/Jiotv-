import time
import secrets
import logging

logger = logging.getLogger(__name__)

VERIFY_HOURS = 4

verified_users = {}
pending_tokens = {}


def is_verified(user_id: int, owner_ids: list, auth_users: list) -> bool:
    if user_id in owner_ids or user_id in auth_users:
        return True
    if user_id in verified_users:
        if time.time() < verified_users[user_id]:
            return True
        else:
            del verified_users[user_id]
    return False


def create_token(user_id: int) -> str:
    token = secrets.token_hex(16)
    pending_tokens[user_id] = token
    return token


def confirm_token(user_id: int, token: str) -> bool:
    if user_id in pending_tokens and pending_tokens[user_id] == token:
        expiry = time.time() + (VERIFY_HOURS * 3600)
        verified_users[user_id] = expiry
        del pending_tokens[user_id]
        return True
    return False


def add_validity(user_id: int, seconds: int):
    current = verified_users.get(user_id, time.time())
    if current < time.time():
        current = time.time()
    verified_users[user_id] = current + seconds


def time_remaining(user_id: int) -> str:
    if user_id not in verified_users:
        return "0h 0m"
    remaining = int(verified_users[user_id] - time.time())
    if remaining <= 0:
        return "0h 0m"
    h = remaining // 3600
    m = (remaining % 3600) // 60
    return f"{h}h {m}m"
