# ═══════════════════════════════════════════════════════════════════════════════
#  LIMIT COMMANDS — main.py mein integrate karne ke liye
#
#  HOW TO USE:
#  1. limit_system.py aur limit_commands.py apne bot folder mein copy karein
#  2. main.py ke top mein (config/verify imports ke baad) yeh line add karein:
#       import limit_system
#  3. Neeche diye gaye functions ko main.py mein copy karein
#  4. start() function mein chhota change karein (neeche dekhen)
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import requests as _req
import limit_system
import config
import verify
from pyrogram import Client, filters
from pyrogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)

# ── Shrinkme.io shortener (verify link ke liye) ───────────────────────────────
_SHRINKME_API = "9503d9bf87c90aa9e0aab35d4dec7d1ce24c0a23"
_SHRINKME_URL = "https://shrinkme.io/api?api={api}&url={url}"


def _shrink(long_url: str):
    """shrinkme.io se short link banao. Fail ho to None return karo."""
    try:
        api_url = _SHRINKME_URL.format(api=_SHRINKME_API, url=long_url)
        resp    = _req.get(api_url, timeout=10)
        result  = resp.json()
        if result.get("status") == "success":
            short = result.get("shortenedUrl", "")
            if short:
                return short
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  /limit  — Har user apni current rec limit dekh sakta hai
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("limit"))
async def limit_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    text = limit_system.format_limit_message(user_id)
    limit_system.mark_seen(user_id)
    await message.reply_text(
        text,
        reply_markup=build_main_keyboard(),
        disable_web_page_preview=True
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /verify  — Do situations:
#    A: Verify chances bache hain → link button dikhao
#    B: Verify limit 0 hai → locked message, koi button nahi
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("verify"))
async def verify_limit_cmd(client: Client, message: Message):
    user_id  = message.from_user.id
    args     = message.command[1:]

    # ── Token confirm karna (deep-link se aaya token) ─────────────────────
    # Agar /verify TOKEN format mein aaya hai to existing flow chalao
    if args and len(args[0]) == 32:
        token = args[0]
        if verify.confirm_token(user_id, token):
            remaining = verify.time_remaining(user_id)
            ok, bonus_msg = limit_system.apply_verify_bonus(user_id)
            bonus_line = f"\n🎁 **Rec Bonus:** {bonus_msg}" if ok else ""
            await message.reply_text(
                f"✅ **Verified!** You have access for **{remaining}**."
                f"{bonus_line}\n\nType /start to use the bot.",
                reply_markup=build_main_keyboard()
            )
        else:
            await message.reply_text(
                "❌ **Invalid or expired token.**\n\nDobara /verify karein.",
                reply_markup=build_main_keyboard()
            )
        return

    # ── Situation check: verify_left kitna bacha hai ──────────────────────
    user_data  = limit_system.get_user(user_id)
    verify_left = user_data.get("verify_left", 0)

    # ── SITUATION B: Limit puri khatam — LOCKED ───────────────────────────
    if verify_left <= 0:
        await message.reply_text(
            "🚫 **ACCESS LOCKED (Limit 0)** 🚫\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "❌ आपकी आज की सभी 'Verify' और 'Rec' लिमिट समाप्त हो चुकी हैं।\n\n"
            "/Verify आप इसका दोबारा उपयोग नहीं कर सकते — आज की लिमिट खत्म\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=build_main_keyboard()
        )
        return

    # ── SITUATION A: Verify chances hain — link button dikhao ────────────
    token      = verify.create_token(user_id)
    bot_me     = await client.get_me()
    verify_url = f"https://t.me/{bot_me.username}?start=verify_{token}"

    short_url = _shrink(verify_url)

    if not short_url:
        await message.reply_text(
            "⚠️ **Link generate nahi ho saka.**\n\n"
            "Shortener service abhi available nahi hai. Thodi der baad dobara try karein.",
            reply_markup=build_main_keyboard()
        )
        return

    next_step  = user_data.get("verify_done", 0)
    rec_reward = "+Rec 5" if next_step == 0 else ("Rec 4" if next_step == 1 else "Rec 3")

    await message.reply_text(
        "🔐 **Verification Required**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"आगे बोट का इस्तेमाल करने और **{rec_reward}** का कोटा अनलॉक करने के लिए "
        "नीचे दिए गए बटन पर क्लिक करके वेरिफिकेशन पूरा करें।\n\n"
        f"🆓 **Remaining Verify Chances:** {verify_left}\n\n"
        "⚠️ *Note: वेरिफिकेशन पूरा करते ही आपकी 'Verify Limit' चालू हो जाएगी।*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Verify Now", url=short_url)]
        ]),
        disable_web_page_preview=True
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /setlimit  — Owner sirf yeh command use kar sakta hai
#
#  Usage:
#    /setlimit 123456789 10     → user ka limit exactly 10 set karo
#    /setlimit 123456789 +5     → user ko 5 aur rec do
#    /setlimit 123456789 -3     → user se 3 rec kato
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("setlimit") & filters.user(config.OWNER_ID))
async def setlimit_cmd(client: Client, message: Message):
    args = message.command[1:]

    if len(args) < 2:
        return await message.reply_text(
            "❌ **Galat format!**\n\n"
            "📌 **Usage:**\n"
            "```\n"
            "/setlimit USER_ID 10     → Exactly 10 Rec set\n"
            "/setlimit USER_ID +5     → 5 Rec aur add karo\n"
            "/setlimit USER_ID -3     → 3 Rec kato\n"
            "```\n\n"
            "**Example:**\n"
            "• `/setlimit 123456789 9`\n"
            "• `/setlimit 123456789 +5`\n"
            "• `/setlimit 123456789 -2`"
        )

    try:
        target_id = int(args[0])
        val_str   = args[1].strip()
    except (ValueError, IndexError):
        return await message.reply_text("❌ Invalid USER_ID.")

    try:
        if val_str.startswith("+"):
            amount = int(val_str[1:])
            limit_system.add_rec(target_id, amount)
            action_text = f"➕ Added +{amount} Rec"
        elif val_str.startswith("-"):
            amount = int(val_str[1:])
            limit_system.add_rec(target_id, -amount)
            action_text = f"➖ Removed -{amount} Rec"
        else:
            amount = int(val_str)
            limit_system.set_rec(target_id, amount)
            action_text = f"🔧 Set to Rec {amount}"
    except ValueError:
        return await message.reply_text("❌ Invalid value. Number hona chahiye (jaise 10, +5, -3).")

    user_data = limit_system.get_user(target_id)
    new_rec   = user_data["rec_limit"]

    await message.reply_text(
        f"✅ **Limit Updated!**\n\n"
        f"👤 **User ID:** `{target_id}`\n"
        f"🔧 **Action:** {action_text}\n"
        f"📊 **New Rec Limit:** Rec {new_rec}\n\n"
        f"_User `/limit` command se apni nayi limit dekh sakta hai._"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Background task — Har 12 ghante mein sab users ka limit refresh
#  (Bot startup mein run karna hai — neeche dekhen)
# ─────────────────────────────────────────────────────────────────────────────

async def _daily_refresh_loop():
    """Har 12 ghante mein automatically sab users ka rec limit reset karta hai."""
    while True:
        await asyncio.sleep(12 * 3600)
        limit_system.daily_refresh_all()
        LOG.info("✅ 12-hour rec limit refresh complete — sab users reset ho gaye.")


# ─────────────────────────────────────────────────────────────────────────────
#  Bot Online Notification — startup par owner ko English message bhejo
# ─────────────────────────────────────────────────────────────────────────────

async def send_startup_message(client: Client):
    """
    Bot start hone ke turant baad owner ko English mein notification bhejta hai.
    main.py mein app.start() ke baad call karo:
        await send_startup_message(app)
    """
    import platform
    import psutil
    from datetime import datetime
    import pytz

    tz      = pytz.timezone(config.TIMEZONE)
    now_str = datetime.now(tz).strftime("%d %b %Y  %I:%M:%S %p")

    try:
        cpu   = psutil.cpu_percent(interval=1)
        ram   = psutil.virtual_memory()
        disk  = psutil.disk_usage("/")
        ram_used  = f"{ram.used  / (1024**3):.1f} GB"
        ram_total = f"{ram.total / (1024**3):.1f} GB"
        disk_free = f"{disk.free / (1024**3):.1f} GB"
    except Exception:
        cpu = ram_used = ram_total = disk_free = "N/A"

    try:
        users_count = len(limit_system._load())
    except Exception:
        users_count = 0

    text = (
        "🟢 **Bot is Now Online!**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 **Bot:** OTT Recorder Bot\n"
        f"🕒 **Started At:** `{now_str}`\n"
        f"🌍 **Timezone:** `{config.TIMEZONE}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 **System Info:**\n"
        f"  🖥 CPU Usage   : `{cpu}%`\n"
        f"  🧠 RAM Used    : `{ram_used}` / `{ram_total}`\n"
        f"  💾 Disk Free   : `{disk_free}`\n"
        f"  🐍 Python      : `{platform.python_version()}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ **Bot Settings:**\n"
        f"  👥 Auth Users  : `{len(config.AUTH_USERS)}`\n"
        f"  📁 Max Slots   : `3 per user`\n"
        f"  ⏰ Auto Refresh: `Every 12 hours`\n"
        f"  🎲 Lucky Ratio : `1 in 5.8 users`\n"
        f"  👤 Total Users : `{users_count}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ All systems are running normally.\n"
        "📢 Use /status to check active tasks."
    )

    for owner_id in config.OWNER_ID:
        try:
            await client.send_message(owner_id, text)
        except Exception as e:
            LOG.warning(f"Startup message send fail (owner {owner_id}): {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN.PY MEIN 4 CHHOTE CHANGES KARNE HAIN:
# ═══════════════════════════════════════════════════════════════════════════════
#
#  ── CHANGE 1: import section ke baad yeh line add karein ──────────────────
#
#      import limit_system
#
#
#  ── CHANGE 2: start() function mein — naye user ko welcome message dikhao ─
#
#  Existing start() function ka top:
#      @app.on_message(filters.command("start"))
#      async def start(client, message: Message):
#          user_id = message.from_user.id
#          if user_id in config.AUTH_USERS or verify.is_verified(...):
#              await message.reply_text("🎬 Welcome ...", ...)
#
#  Badal kar yeh karo (user_id line ke TURANT BAAD yeh block add karo):
#
#      @app.on_message(filters.command("start"))
#      async def start(client, message: Message):
#          user_id = message.from_user.id
#
#          # ── New user welcome ──────────────────────────────────────────────
#          if limit_system.is_new_user(user_id):
#              limit_system.get_user(user_id)          # record banana
#              await message.reply_text(
#                  limit_system.NEW_USER_WELCOME,
#                  reply_markup=build_main_keyboard()
#              )
#          # ─────────────────────────────────────────────────────────────────
#
#          if user_id in config.AUTH_USERS or verify.is_verified(...):
#              # ... existing code unchanged ...
#
#
#  ── CHANGE 3: verify_cmd mein confirm_token ke baad bonus apply karo ──────
#
#  Existing:
#      if verify.confirm_token(user_id, token):
#          remaining = verify.time_remaining(user_id)
#          await message.reply_text(
#              f"✅ **Verified!** You have access for **{remaining}**.\n\n"
#              "Type /start to use the bot.",
#              reply_markup=build_main_keyboard()
#          )
#
#  Badal kar:
#      if verify.confirm_token(user_id, token):
#          remaining = verify.time_remaining(user_id)
#          ok, bonus_msg = limit_system.apply_verify_bonus(user_id)
#          bonus_line = f"\n🎁 **Rec Bonus:** {bonus_msg}" if ok else ""
#          await message.reply_text(
#              f"✅ **Verified!** You have access for **{remaining}**."
#              f"{bonus_line}\n\nType /start to use the bot.",
#              reply_markup=build_main_keyboard()
#          )
#
#
#  ── CHANGE 4: if __name__ == "__main__": mein app.start() ke baad ─────────
#
#  Existing:
#      app.start()
#      print("🤖 OTT Recorder Bot is Live!")
#      idle()
#
#  Badal kar:
#      app.start()
#      loop = asyncio.get_event_loop()
#      loop.run_until_complete(send_startup_message(app))   ← owner ko notification
#      loop.create_task(_daily_refresh_loop())              ← 12h refresh
#      print("⏰ 12-hour auto-refresh scheduled.")
#      print("🤖 OTT Recorder Bot is Live!")
#      idle()
#
# ═══════════════════════════════════════════════════════════════════════════════
