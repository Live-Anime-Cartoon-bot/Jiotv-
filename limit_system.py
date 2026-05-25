import time
import json
import random
import os

LIMIT_FILE = "user_limits.json"

DEFAULT_REC_LIMIT   = 1
DEFAULT_VERIFY_LEFT = 3
LUCKY_RATIO         = 5.8
REFRESH_SECONDS     = 12 * 3600

VERIFY_STEPS = [
    {"rec_delta": +5, "result_rec": None,  "msg": "Aapko milenge +Rec 5"},
    {"rec_delta": -2, "result_rec": 4,     "msg": "Aapki limit ghatkar hogi: Rec 4"},
    {"rec_delta": -1, "result_rec": 3,     "msg": "Aapki limit aur ghatkar hogi: Rec 3"},
]

NEW_USER_WELCOME = (
    "👋 **Welcome to the Bot!**\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "🚀 बोट में आपका स्वागत है! आपका अकाउंट सफ़लपूर्वक एक्टिवेट कर दिया गया है।\n\n"
    f"🎁 नए यूज़र के तौर पर आपको **Rec {DEFAULT_REC_LIMIT}** का ट्रायल बैलेंस "
    f"और **{DEFAULT_VERIFY_LEFT} Verification** चांस मिले हैं।\n\n"
    "📊 अपनी पूरी लिमिट देखने के लिए अभी टाइप करें: /limit\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━"
)


def is_new_user(user_id: int) -> bool:
    data = _load()
    uid  = str(user_id)
    return uid not in data


def _load() -> dict:
    try:
        with open(LIMIT_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    with open(LIMIT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _new_user_record() -> dict:
    is_lucky = random.random() < (1.0 / LUCKY_RATIO)
    return {
        "rec_limit":       DEFAULT_REC_LIMIT,
        "verify_left":     DEFAULT_VERIFY_LEFT,
        "verify_done":     0,
        "is_lucky":        is_lucky,
        "last_refresh":    time.time(),
        "first_time":      True,
    }


def get_user(user_id: int) -> dict:
    data = _load()
    uid = str(user_id)
    if uid not in data:
        data[uid] = _new_user_record()
        _save(data)
    return data[uid]


def mark_seen(user_id: int):
    data = _load()
    uid = str(user_id)
    if uid in data:
        data[uid]["first_time"] = False
        _save(data)


def is_unlimited(user_id: int, owner_ids: list = None, auth_users: list = None) -> bool:
    """Returns True if this user should bypass all limits."""
    if owner_ids and user_id in owner_ids:
        return True
    if auth_users and user_id in auth_users:
        return True
    return False


def use_rec(user_id: int, unlimited: bool = False) -> tuple:
    if unlimited:
        return True, "✅ Unlimited access."
    data = _load()
    uid = str(user_id)
    if uid not in data:
        data[uid] = _new_user_record()
    user = data[uid]
    if user["rec_limit"] <= 0:
        return False, "❌ Rec limit khatam ho gayi! /limit check karein ya verify karein."
    user["rec_limit"] -= 1
    user["first_time"] = False
    data[uid] = user
    _save(data)
    return True, f"✅ 1 Rec use hua. Bacha: Rec {user['rec_limit']}"


def apply_verify_bonus(user_id: int) -> tuple:
    data = _load()
    uid = str(user_id)
    if uid not in data:
        data[uid] = _new_user_record()
    user = data[uid]

    if user["verify_left"] <= 0:
        return False, "🚫 Aaj ke liye sab verifications lock ho gaye! Kal tak wait karein."

    step_idx = user["verify_done"]
    if step_idx >= len(VERIFY_STEPS):
        return False, "🚫 Verify limit expire ho gayi!"

    step = VERIFY_STEPS[step_idx]
    if step["result_rec"] is not None:
        user["rec_limit"] = step["result_rec"]
    else:
        user["rec_limit"] = max(0, user["rec_limit"] + step["rec_delta"])

    user["verify_left"]  = max(0, user["verify_left"] - 1)
    user["verify_done"] += 1
    user["first_time"]   = False
    data[uid] = user
    _save(data)
    return True, step["msg"]


def daily_refresh_all():
    data = _load()
    now = time.time()
    for uid, user in data.items():
        if user.get("is_lucky"):
            user["rec_limit"] = 3
        else:
            user["rec_limit"] = 0
        user["verify_left"]  = DEFAULT_VERIFY_LEFT
        user["verify_done"]  = 0
        user["last_refresh"] = now
    _save(data)


def set_rec(user_id: int, count: int):
    data = _load()
    uid = str(user_id)
    if uid not in data:
        data[uid] = _new_user_record()
    data[uid]["rec_limit"] = count
    data[uid]["first_time"] = False
    _save(data)


def add_rec(user_id: int, count: int):
    data = _load()
    uid = str(user_id)
    if uid not in data:
        data[uid] = _new_user_record()
    data[uid]["rec_limit"] = max(0, data[uid]["rec_limit"] + count)
    data[uid]["first_time"] = False
    _save(data)


def format_limit_message(user_id: int) -> str:
    user     = get_user(user_id)
    rec      = user["rec_limit"]
    v_left   = user["verify_left"]
    v_done   = user["verify_done"]
    is_lucky = user.get("is_lucky", False)
    is_first = user.get("first_time", False)
    is_locked = v_left <= 0

    last_refresh = user.get("last_refresh", time.time())
    elapsed      = time.time() - last_refresh
    remaining_s  = max(REFRESH_SECONDS - elapsed, 0)
    rh = int(remaining_s // 3600)
    rm = int((remaining_s % 3600) // 60)
    refresh_str  = f"{rh}h {rm}m" if remaining_s > 0 else "Abhi refresh hoga!"

    if is_locked:
        verify_line = "⚠️ VERIFY NO USE (यह लिमिट अभी लॉक है)"
    elif is_first:
        verify_line = "👉 Pehli baar verify karne par aapka quota unlock ho jayega!"
    else:
        verify_line = "👉 Verify karein aur aur Rec paaein!"

    lucky_line = ""
    if is_lucky:
        lucky_line = "⭐ **Lucky User:** Refresh ke baad Rec 3 milega!\n"

    step_labels = [
        ("1️⃣", "First Use  ➔ Verify 2", "(Aapko milenge +Rec 5)"),
        ("2️⃣", "Second Use ➔ Verify 1", "(Aapki limit ghatkar hogi: Rec 4)"),
        ("3️⃣", "Dobara Use ➔ Verify 1", "(Aapki limit aur ghatkar hogi: Rec 3)"),
        ("4️⃣", "Third Use  ➔ Verify 0", "(Lock 🚫 Today Limit Expired)"),
    ]

    flow_lines = []
    for i, (num, action, reward) in enumerate(step_labels):
        if i < v_done:
            prefix = "✅"
        elif i == v_done and not is_locked:
            prefix = "▶️"
        else:
            prefix = num
        flow_lines.append(f"{prefix} {action} {reward}")

    flow_text = "\n".join(flow_lines)

    return (
        "📊 **BOT VERIFICATION STATUS** 📊\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **Your Current Limit:** Rec {rec}\n"
        "Aap iska use kar sakte hain:\n"
        "👉 `/REC LINK 00:00:30 Filename`\n"
        f"🆓 **Remaining Verify Limit:** {v_left} Verification\n"
        f"{verify_line}\n"
        f"{lucky_line}"
        "🔢 **Countdown Flow & Rewards:**\n"
        f"{flow_text}\n\n"
        "🌅 **SURPRISE GIFT (Lucky User):**\n"
        "Every 5.8 users mein se 1 lucky user ko extra badal-badal kar rewards milenge!\n\n"
        f"⏱️ **Daily Refresh Timer:** {refresh_str}\n"
        "🔄 Har 12 ghante me system fresh ho jayega. "
        "Normal users ka Rec 0 hoga, par Lucky User ka balance Rec 3 rahega!"
    )
