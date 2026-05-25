import os
import time
import json
import logging
import random
import shlex
import shutil
import asyncio
import psutil
from typing import List, Dict, Optional, Tuple
from os.path import join
from pyrogram import Client, filters, idle, enums
from pyrogram.types import (
    Message,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from datetime import datetime, timedelta
import config
import pytz
import verify
import limit_system
import playlist_manager

tz = pytz.timezone(config.TIMEZONE)

def tz_time(*args):
    return datetime.now(tz).timetuple()

logging.Formatter.converter = tz_time
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%d-%m-%Y %I:%M:%S %p " + tz.tzname(datetime.now())
)
LOG = logging.getLogger(__name__)

app = Client("recorder", bot_token=config.BOT_TOKEN, api_id=config.API_ID, api_hash=config.API_HASH)

# ── Remove reply-quoting from ALL bot messages (no quote bubble) ──────────────
_orig_reply_text = Message.reply_text
async def _reply_no_quote(self, text, quote: bool = False, **kw):
    return await _orig_reply_text(self, text, quote=quote, **kw)
Message.reply_text = _reply_no_quote  # type: ignore[method-assign]


def _is_allowed(_, __, message) -> bool:
    uid = message.from_user.id if message.from_user else None
    if uid is None:
        return False
    if uid in config.OWNER_ID or uid in config.AUTH_USERS:
        return True
    if verify.is_verified(uid, config.OWNER_ID, config.AUTH_USERS):
        return True
    # Normal user: rec_limit > 0 hai to commands allow karo
    try:
        user_data = limit_system.get_user(uid)
        if user_data.get("rec_limit", 0) > 0:
            return True
    except Exception:
        pass
    return False

allowed = filters.create(_is_allowed)

MAX_CONCURRENT = 3

user_tasks:       Dict[int, Dict[str, float]] = {}
user_status:      Dict[int, Dict[str, dict]]  = {}
user_ffmpeg_pids: Dict[int, Dict[str, int]]   = {}
progress_tasks:   Dict[int, Dict[str, object]] = {}
cancelled_jobs: set = set()
scheduled_jobs: Dict[int, Dict[str, dict]] = {}   # user_id → {sch_id → job_info}
_sch_counter: Dict[int, int] = {}                  # user_id → next schedule id

# ─── History log ──────────────────────────────────────────────────────────────
# Each entry: {type, status, user_id, username, filename, duration_s, size_mb,
#              url, ts, res_label, audio_label}
history_log: List[dict] = []
MAX_HISTORY = 500   # keep last N entries in memory

user_setup: Dict[int, dict] = {}

LANG_MAP = {
    "hin": "HIN", "hi": "HIN",
    "kan": "KAN", "kn": "KAN",
    "tel": "TEL", "te": "TEL",
    "tam": "TAM", "ta": "TAM",
    "mal": "MAL", "ml": "MAL",
    "ben": "BEN", "bn": "BEN",
    "mar": "MAR", "mr": "MAR",
    "eng": "ENG", "en": "ENG",
    "pun": "PUN", "pa": "PUN",
    "guj": "GUJ", "gu": "GUJ",
    "ori": "ORI", "or": "ORI",
    "urd": "URD", "ur": "URD",
}

LANG_FULL = {
    "hin": "Hindi",     "hi":  "Hindi",
    "kan": "Kannada",   "kn":  "Kannada",
    "tel": "Telugu",    "te":  "Telugu",
    "tam": "Tamil",     "ta":  "Tamil",
    "mal": "Malayalam", "ml":  "Malayalam",
    "ben": "Bengali",   "bn":  "Bengali",
    "mar": "Marathi",   "mr":  "Marathi",
    "eng": "English",   "en":  "English",
    "pun": "Punjabi",   "pa":  "Punjabi",
    "guj": "Gujarati",  "gu":  "Gujarati",
    "ori": "Odia",      "or":  "Odia",
    "urd": "Urdu",      "ur":  "Urdu",
}

WM_POSITIONS = {
    "top_left":     ("10", "10"),
    "top_right":    ("w-tw-10", "10"),
    "center":       ("(w-tw)/2", "(h-th)/2"),
    "bottom_left":  ("10", "h-th-10"),
    "bottom_right": ("w-tw-10", "h-th-10"),
}

WM_LABEL = {
    "top_left":     "↖ Top-Left",
    "top_right":    "↗ Top-Right",
    "center":       "⊙ Center",
    "bottom_left":  "↙ Bottom-Left",
    "bottom_right": "↘ Bottom-Right",
}

WM_LABEL_TO_KEY = {v: k for k, v in WM_LABEL.items()}

VIDEO_SIZES = {
    "size1":    {
        "label": "📺 Size 1 — 720×396",
        "desc":  "16:9 Widescreen",
        "vf":    "scale=720:396:force_original_aspect_ratio=decrease,pad=720:396:(ow-iw)/2:(oh-ih)/2",
    },
    "size2":    {
        "label": "📺 Size 2 — 720×540",
        "desc":  "4:3 Black bars",
        "vf":    "scale=720:540:force_original_aspect_ratio=decrease,pad=720:540:(ow-iw)/2:(oh-ih)/2",
    },
    "size3":    {
        "label": "📺 Size 3 — 720×405",
        "desc":  "16:9 Border all sides",
        "vf":    "scale=700:394:force_original_aspect_ratio=decrease,pad=720:405:10:5",
    },
    "bars_169": {
        "label": "◼ 16:9 Bars — 720×576",
        "desc":  "Letterbox",
        "vf":    "scale=720:576:force_original_aspect_ratio=decrease,pad=720:576:(ow-iw)/2:(oh-ih)/2",
    },
    "bars_43":  {
        "label": "◼ 4:3 Bars — 720×540",
        "desc":  "Pillarbox",
        "vf":    "scale=-2:540:force_original_aspect_ratio=decrease,pad=720:540:(ow-iw)/2:(oh-ih)/2",
    },
    "480p": {
        "label": "📺 480p — 854×480",
        "desc":  "Standard 480p (channel default)",
        "vf":    "scale=854:480:force_original_aspect_ratio=decrease,pad=854:480:(ow-iw)/2:(oh-ih)/2:black",
    },
    "original": {
        "label": "🔓 Original Size",
        "desc":  "No scaling",
        "vf":    None,
    },
}

SIZE_LABEL_TO_KEY = {v["label"]: k for k, v in VIDEO_SIZES.items()}

SLOT_EMOJI = ["1️⃣", "2️⃣", "3️⃣"]

COMPRESS_PRESETS = {
    "🔵 High Quality":   ("-c:v libx264 -crf 23 -preset fast -c:a aac -b:a 128k", "High (good quality, moderate size)"),
    "🟡 Medium Quality": ("-c:v libx264 -crf 28 -preset fast -c:a aac -b:a 96k",  "Medium (balanced)"),
    "🔴 Low (Smallest)": ("-c:v libx264 -crf 32 -preset fast -c:a aac -b:a 64k",  "Low (small size, lower quality)"),
}

compress_pending: Dict[int, int] = {}

PROGRESS_FILLED = '<emoji id="5915540975987462465">▰</emoji>'
PROGRESS_EMPTY  = '<emoji id="6217587660634989068">▱</emoji>'

OTT_RES_LABEL_TO_FMT: Dict[str, str] = {}   # populated only as fallback
OTT_AUDIO_LANGS:     Dict[str, Optional[str]] = {"🌐 Multi": None}  # fallback

_HEIGHT_LABEL: Dict[int, str] = {
    144: "📺 140p",  240: "📺 240p",  360: "📺 360p",
    480: "📺 480p",  576: "📺 576p",  640: "📺 640p",
    720: "📺 720p",  1080: "🔵 1080p", 1440: "🔶 2K",
    2160: "🔶 4K",
}
_HEIGHT_FMT: Dict[int, str] = {
    h: f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
    for h in [144, 240, 360, 480, 576, 640, 720, 1080, 1440, 2160]
}
_LANG_CODE_TO_LABEL: Dict[str, str] = {
    "hin": "🇮🇳 Hindi",    "tam": "🎬 Tamil",
    "tel": "🎭 Telugu",    "mal": "🌴 Malayalam",
    "kan": "🌸 Kannada",   "mar": "🎪 Marathi",
    "ben": "🇧🇩 Bengali",  "pun": "🎵 Punjabi",
    "eng": "🇬🇧 English",  "urd": "🕌 Urdu",
    "guj": "🎶 Gujarati",  "ori": "🌸 Odia",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_job_key(user_id: int, job_id: str) -> str:
    return f"{user_id}:{job_id}"


def next_job_id(user_id: int) -> Optional[str]:
    used = set(user_tasks.get(user_id, {}).keys())
    for slot in ["slot1", "slot2", "slot3"]:
        if slot not in used:
            return slot
    return None


def slot_number(job_id: str) -> int:
    return int(job_id.replace("slot", ""))


async def runcmd(cmd: str, timeout: int = 120) -> Tuple[int, str, str]:
    args = shlex.split(cmd)
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except Exception:
            pass
        return -1, "", f"Command timed out after {timeout}s"
    return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def time_to_seconds(time_str: str) -> int:
    try:
        h, m, s = time_str.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except Exception:
        return 0


def TimeFormatter(milliseconds: int) -> str:
    seconds, _ = divmod(milliseconds, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, min_ = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02}:{min_:02}:{sec:02}"
    return f"{min_:02}:{sec:02}"


async def get_duration_ffmpeg(input_file: str) -> int:
    try:
        cmd = (
            f'ffprobe -v error -show_entries format=duration '
            f'-of default=noprint_wrappers=1:nokey=1 "{input_file}"'
        )
        retcode, out, _ = await runcmd(cmd)
        if retcode == 0:
            return int(float(out.strip()))
    except Exception as e:
        LOG.warning(f"FFprobe duration failed: {e}")
    return 0


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _add_history(entry: dict):
    """Append one completed/cancelled/failed entry to the global history_log."""
    entry.setdefault("ts", time.time())
    history_log.append(entry)
    if len(history_log) > MAX_HISTORY:
        del history_log[0]


def build_metadata_args(tracks: list, selected_tracks: set, channel_name: str) -> str:
    if not channel_name or not tracks or not selected_tracks:
        return ""
    selected = [t for t in tracks if t["index"] in selected_tracks]
    parts = []
    for out_idx, track in enumerate(selected):
        lang  = track.get("language", "")
        iso   = lang[:3] if lang else ""
        label = LANG_FULL.get(lang, track.get("display", f"Audio {out_idx + 1}"))
        title = f"{channel_name} {label}".strip()
        safe  = title.replace('"', '\\"')
        parts += [
            f'-metadata:s:a:{out_idx} title="{safe}"',
            f'-metadata:s:a:{out_idx} handler_name="{safe}"',
        ]
        if iso:
            parts.append(f'-metadata:s:a:{out_idx} language={iso}')
    return " ".join(parts)


def http_opts(url: str) -> str:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}/"
    return (
        f'-user_agent "{_UA}" '
        f'-headers "Referer: {origin}\\r\\n"'
    )


def get_video_media(msg):
    if not msg:
        return None
    return msg.video or msg.document or None


# ─────────────────────────────────────────────────────────────────────────────
#  Stream detection
# ─────────────────────────────────────────────────────────────────────────────

async def detect_stream_info(url: str) -> dict:
    cmd = (
        f'ffprobe -v quiet -timeout 15000000 {http_opts(url)} -print_format json '
        f'-show_streams "{url}"'
    )
    retcode, out, _ = await runcmd(cmd, timeout=25)
    result = {"video": None, "tracks": []}
    if retcode != 0 or not out.strip():
        return result
    try:
        streams   = json.loads(out).get("streams", [])
        audio_idx = 0
        for s in streams:
            ctype = s.get("codec_type", "")
            if ctype == "video" and result["video"] is None:
                w   = s.get("width",  0)
                h   = s.get("height", 0)
                fps_raw = s.get("r_frame_rate", "0/1")
                try:
                    num, den = fps_raw.split("/")
                    fps = round(int(num) / int(den), 2) if int(den) else 0
                except Exception:
                    fps = 0
                br = int(s.get("bit_rate", 0) or 0) // 1000
                result["video"] = {
                    "width": w, "height": h,
                    "codec": s.get("codec_name", "").upper(),
                    "bitrate_kbps": br, "fps": fps,
                }
            elif ctype == "audio":
                lang_tag = (
                    s.get("tags", {}).get("language", "")
                    or s.get("tags", {}).get("LANGUAGE", "")
                ).lower()
                codec   = s.get("codec_name", "audio").upper()
                display = LANG_MAP.get(lang_tag, lang_tag.upper() if lang_tag else f"Track {audio_idx + 1}")
                result["tracks"].append({
                    "index":        audio_idx,
                    "stream_index": s.get("index", audio_idx),
                    "language":     lang_tag,
                    "codec":        codec,
                    "label":        f"{display} ({codec})",
                    "display":      display,
                })
                audio_idx += 1
    except Exception as e:
        LOG.warning(f"Stream info parse error: {e}")
    return result


def format_quality_line(video: dict | None) -> str:
    if not video or not video.get("width"):
        return "Unknown"
    parts = [f"{video['width']}×{video['height']}"]
    if video.get("codec"):
        parts.append(video["codec"])
    if video.get("bitrate_kbps"):
        parts.append(f"{video['bitrate_kbps']}kbps")
    if video.get("fps"):
        parts.append(f"{video['fps']}fps")
    return " | ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
#  ReplyKeyboard builders
# ─────────────────────────────────────────────────────────────────────────────

def build_main_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom menu — always visible after /start."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🎥 Record"),       KeyboardButton("📥 Download")],
            [KeyboardButton("🌐 OTT Download"), KeyboardButton("📊 Status")],
            [KeyboardButton("🗜 Compress"),      KeyboardButton("📸 Screenshot")],
            [KeyboardButton("🍪 Cookies"),       KeyboardButton("📖 Help")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def build_audio_keyboard(tracks: List[dict], selected: set) -> ReplyKeyboardMarkup:
    rows = []
    for i in range(0, len(tracks), 2):
        row = []
        for t in tracks[i: i + 2]:
            check = "✅" if t["index"] in selected else "❌"
            row.append(KeyboardButton(f"{check} {t['label']}"))
        rows.append(row)
    rows.append([KeyboardButton("🔁 Select All Tracks")])
    rows.append([KeyboardButton("◀️ Back"),  KeyboardButton("✅ Next: Watermark")])
    rows.append([KeyboardButton("❌ Cancel Setup")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_watermark_keyboard(setup: dict) -> ReplyKeyboardMarkup:
    pos  = setup.get("watermark_pos")
    auto = setup.get("auto_mode", False)
    mode = setup.get("mode", "record")

    def lbl(key):
        base = WM_LABEL[key]
        return ("✅ " if pos == key else "") + base

    rows = [
        [KeyboardButton(lbl("top_left")),    KeyboardButton(lbl("top_right"))],
        [KeyboardButton(lbl("center"))],
        [KeyboardButton(lbl("bottom_left")), KeyboardButton(lbl("bottom_right"))],
        [KeyboardButton(("✅ " if pos is None else "") + "🚫 Watermark OFF")],
        [KeyboardButton("✏️ Change Watermark Text")],
    ]
    if mode == "record":
        rows.append([KeyboardButton(
            ("✅ " if auto else "") + "⏱️ Auto: First+Last 1min"
        )])
    if mode == "download":
        rows.append([KeyboardButton("📥 START DOWNLOAD")])
    else:
        rows.append([KeyboardButton("📐 Next: Video Size →")])
    rows.append([KeyboardButton("❌ Cancel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_size_keyboard(selected: str = "original") -> ReplyKeyboardMarkup:
    rows = []
    for key, val in VIDEO_SIZES.items():
        check = "✅ " if selected == key else ""
        rows.append([KeyboardButton(f"{check}{val['label']}")])
    rows.append([KeyboardButton("◀️ Back to Watermark")])
    rows.append([KeyboardButton("▶️ Start Recording"), KeyboardButton("❌ Cancel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_cancel_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    jobs = user_status.get(user_id, {})
    rows = []
    for job_id, info in sorted(jobs.items()):
        n = slot_number(job_id)
        emoji = SLOT_EMOJI[n - 1]
        rows.append([KeyboardButton(f"{emoji} Cancel Slot {n}: {info['filename']}")])
    rows.append([KeyboardButton("❌ Cancel ALL")])
    rows.append([KeyboardButton("◀️ Close Menu")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_compress_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔵 High Quality")],
            [KeyboardButton("🟡 Medium Quality")],
            [KeyboardButton("🔴 Low (Smallest)")],
            [KeyboardButton("❌ Cancel Compress")],
        ],
        resize_keyboard=True,
    )


def build_ott_resolution_keyboard(selected: str = "") -> ReplyKeyboardMarkup:
    rows = []
    labels = [label for label, _ in OTT_RESOLUTIONS]
    for i in range(0, len(labels), 3):
        row = []
        for label in labels[i: i + 3]:
            check = "✅ " if selected == label else ""
            row.append(KeyboardButton(f"{check}{label}"))
        rows.append(row)
    rows.append([KeyboardButton("❌ Cancel OTT")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_ott_audio_keyboard(selected: str = "") -> ReplyKeyboardMarkup:
    rows = []
    for lang_label in OTT_AUDIO_LANGS:
        check = "✅ " if selected == lang_label else ""
        rows.append([KeyboardButton(f"{check}{lang_label}")])
    rows.append([KeyboardButton("◀️ Back to Resolution")])
    rows.append([KeyboardButton("❌ Cancel OTT")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_ott_resolution_keyboard_dynamic(res_map: dict, selected: str = "") -> ReplyKeyboardMarkup:
    labels = list(res_map.keys())
    rows = []
    for i in range(0, len(labels), 3):
        row = []
        for lbl in labels[i: i + 3]:
            check = "✅ " if selected == lbl else ""
            row.append(KeyboardButton(f"{check}{lbl}"))
        rows.append(row)
    rows.append([KeyboardButton("❌ Cancel OTT")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_ott_audio_keyboard_dynamic(audio_map: dict, selected: str = "") -> ReplyKeyboardMarkup:
    rows = []
    for lbl in audio_map:
        check = "✅ " if selected == lbl else ""
        rows.append([KeyboardButton(f"{check}{lbl}")])
    rows.append([KeyboardButton("◀️ Back to Resolution")])
    rows.append([KeyboardButton("❌ Cancel OTT")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def setup_summary_text(setup: dict) -> str:
    tracks   = setup.get("tracks", [])
    selected = setup.get("selected_tracks", set())
    sel_labels = [t["label"] for t in tracks if t["index"] in selected] or ["All"]
    pos      = setup.get("watermark_pos")
    wm_text  = setup.get("watermark_text", config.DEFAULT_FILENAME)
    auto     = setup.get("auto_mode", False)
    mode     = setup.get("mode", "record")
    wm_desc  = "OFF" if pos is None else f"{WM_LABEL.get(pos, pos)} → `{wm_text}`"
    size_key = setup.get("video_size", "original")
    size_lbl = VIDEO_SIZES.get(size_key, VIDEO_SIZES["original"])["label"]

    if mode == "download":
        header        = "📥 **Download Setup**"
        duration_line = ""
    else:
        header        = "🎛️ **Recording Setup**"
        duration_line = f"⏱ **Duration:** `{setup.get('timestamp', '—')}`\n"
        duration_line += f"⏩ **Auto Mode:** `{'✅ First+Last 1min' if auto else '❌ Off'}`\n"

    return (
        f"{header}\n\n"
        f"🔗 **URL:** `{setup['url'][:60]}...`\n"
        f"{duration_line}"
        f"📁 **Filename:** `{setup['filename']}`\n"
        f"🎵 **Audio:** `{', '.join(sel_labels)}`\n"
        f"🖼 **Watermark:** `{wm_desc}`\n"
        f"📐 **Size:** `{size_lbl}`\n\n"
        f"👇 Choose an option:"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /start  — open to everyone; verified users get full menu, others get verify prompt
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("start"))
async def start(client, message: Message):
    user_id = message.from_user.id

    # Verify deep-link click karne par /start verify_TOKEN aata hai.
    # Agar yeh hai to is handler ko skip karo — start_verify_deeplink handle karega.
    if len(message.command) > 1 and message.command[1].startswith("verify_"):
        return

    if limit_system.is_new_user(user_id):
        limit_system.get_user(user_id)
        await message.reply_text(
            limit_system.NEW_USER_WELCOME,
            reply_markup=build_main_keyboard()
        )

    # Owner / hardcoded AUTH_USERS always pass
    if user_id in config.AUTH_USERS or verify.is_verified(user_id, config.OWNER_ID, config.AUTH_USERS):
        await message.reply_text(
            "🎬 **Welcome to Video Bot!**\n\n"
            "🎥 **Record:** `/rec http://link 00:00:00 Filename`\n"
            "📥 **Download:** `/download http://link Filename`\n"
            "🌐 **OTT/YouTube:** `/ott_download https://youtube.com/... Name`\n"
            "⏰ **Schedule:** `/schedule HH:MM URL 00:00:00 Filename`\n"
            "🗜 **Compress:** Reply to video + `/compress`\n"
            "📸 **Screenshots:** Reply to video + `/screenshot [1-30]`\n\n"
            f"📢 Channel: {config.CHANNEL_NAME}\n\n"
            "👇 Use the menu buttons below or type /help",
            reply_markup=build_main_keyboard()
        )
    else:
        # Generate a one-time token and send verification link
        token = verify.create_token(user_id)
        verify_url = f"https://t.me/{(await client.get_me()).username}?start=verify_{token}"
        short_url  = verify_url  # shortener optional
        await message.reply_text(
            "🔒 **Access Restricted**\n\n"
            "This bot is private. To get **4 hours** of access, verify yourself:\n\n"
            f"👉 [Click here to verify]({short_url})\n\n"
            "_Or send_ `/verify {token}` _directly._",
            disable_web_page_preview=True,
        )


@app.on_message(filters.command("start") & filters.regex(r"^/start verify_(.+)$"))
async def start_verify_deeplink(client, message: Message):
    """Handles t.me/bot?start=verify_TOKEN deep-link."""
    user_id = message.from_user.id
    token   = message.command[1].replace("verify_", "", 1)
    if verify.confirm_token(user_id, token):
        remaining = verify.time_remaining(user_id)
        await message.reply_text(
            f"✅ **Verified!** You have access for **{remaining}**.\n\n"
            "Type /start to use the bot.",
        )
    else:
        await message.reply_text("❌ Invalid or expired token. Send /start to get a new one.")


# ─────────────────────────────────────────────────────────────────────────────
#  /verify  — token confirm OR show verify button with limit system
# ─────────────────────────────────────────────────────────────────────────────

_SHRINKME_API = "9503d9bf87c90aa9e0aab35d4dec7d1ce24c0a23"

def _shrink(long_url: str):
    import requests as _req
    try:
        resp   = _req.get(f"https://shrinkme.io/api?api={_SHRINKME_API}&url={long_url}", timeout=10)
        result = resp.json()
        if result.get("status") == "success":
            short = result.get("shortenedUrl", "")
            if short:
                return short
    except Exception:
        pass
    return None


@app.on_message(filters.command("verify"))
async def verify_cmd(client, message: Message):
    user_id = message.from_user.id
    args    = message.command[1:]

    # ── Owner / AUTH_USERS — no verification needed ───────────────────────
    if user_id in config.OWNER_ID or user_id in config.AUTH_USERS:
        return await message.reply_text(
            "✅ **Aap Owner/Admin hain — verification ki zaroorat nahi!**\n\n"
            "Seedha /start use karein.",
            reply_markup=build_main_keyboard()
        )

    # ── Token confirm (deep-link ya manual /verify TOKEN) ─────────────────
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

    # ── Check verify_left ─────────────────────────────────────────────────
    user_data   = limit_system.get_user(user_id)
    verify_left = user_data.get("verify_left", 0)

    if verify_left <= 0:
        return await message.reply_text(
            "🚫 **ACCESS LOCKED (Limit 0)** 🚫\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "❌ Aapki aaj ki saari Verify aur Rec limit khatam ho gayi hai.\n\n"
            "🔄 Kal tak wait karein — system 12 ghante mein reset hoga.",
            reply_markup=build_main_keyboard()
        )

    # ── Generate verify link ──────────────────────────────────────────────
    token      = verify.create_token(user_id)
    bot_me     = await client.get_me()
    verify_url = f"https://t.me/{bot_me.username}?start=verify_{token}"
    short_url  = _shrink(verify_url) or verify_url   # fallback to direct URL

    next_step  = user_data.get("verify_done", 0)
    rec_reward = "+Rec 5" if next_step == 0 else ("Rec 4" if next_step == 1 else "Rec 3")

    await message.reply_text(
        "🔐 **Verification Required**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Aage bot ka istemal karne aur **{rec_reward}** ka quota unlock karne ke liye "
        "neeche diye gaye button par click karke verification poora karein.\n\n"
        f"🆓 **Remaining Verify Chances:** {verify_left}\n\n"
        "⚠️ _Note: Verification poora karte hi aapki 'Verify Limit' chalu ho jayegi._\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Verify Now", url=short_url)]
        ]),
        disable_web_page_preview=True
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /limit  — user apni rec limit dekhe
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("limit"))
async def limit_cmd(client, message: Message):
    user_id = message.from_user.id
    if user_id in config.OWNER_ID or user_id in config.AUTH_USERS:
        await message.reply_text(
            "♾️ **Aapki Limit: UNLIMITED**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👑 **Owner / Admin** hain aap — koi bhi limit nahi hai!\n\n"
            "✅ Rec: **∞ Unlimited**\n"
            "✅ Download: **∞ Unlimited**\n"
            "✅ Verify: **Not required**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=build_main_keyboard()
        )
        return
    text = limit_system.format_limit_message(user_id)
    limit_system.mark_seen(user_id)
    await message.reply_text(text, reply_markup=build_main_keyboard(), disable_web_page_preview=True)


# ─────────────────────────────────────────────────────────────────────────────
#  /setlimit  — owner sirf use kar sakta hai
#  Usage: /setlimit USER_ID 10   |  /setlimit USER_ID +5  |  /setlimit USER_ID -3
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("setlimit") & filters.user(config.OWNER_ID))
async def setlimit_cmd(client, message: Message):
    args = message.command[1:]
    if len(args) < 2:
        return await message.reply_text(
            "❌ **Galat format!**\n\n"
            "📌 **Usage:**\n"
            "```\n/setlimit USER_ID 10\n/setlimit USER_ID +5\n/setlimit USER_ID -3\n```"
        )
    try:
        target_id = int(args[0])
        val_str   = args[1].strip()
    except (ValueError, IndexError):
        return await message.reply_text("❌ Invalid USER_ID.")
    try:
        if val_str.startswith("+"):
            limit_system.add_rec(target_id, int(val_str[1:]))
            action_text = f"➕ Added +{val_str[1:]} Rec"
        elif val_str.startswith("-"):
            limit_system.add_rec(target_id, -int(val_str[1:]))
            action_text = f"➖ Removed {val_str} Rec"
        else:
            limit_system.set_rec(target_id, int(val_str))
            action_text = f"🔧 Set to Rec {val_str}"
    except ValueError:
        return await message.reply_text("❌ Invalid value. Jaise: 10, +5, -3")
    new_rec = limit_system.get_user(target_id)["rec_limit"]
    await message.reply_text(
        f"✅ **Limit Updated!**\n\n"
        f"👤 **User ID:** `{target_id}`\n"
        f"🔧 **Action:** {action_text}\n"
        f"📊 **New Rec Limit:** Rec {new_rec}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Owner-only: /grant_access USER_ID HOURS  — extend someone's access
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("grant_access") & filters.user(config.OWNER_ID))
async def grant_access_cmd(client, message: Message):
    args = message.command[1:]
    if len(args) < 1:
        return await message.reply_text("Usage: `/grant_access USER_ID [HOURS]`\nDefault hours: 24")
    try:
        target_id = int(args[0])
        hours     = float(args[1]) if len(args) > 1 else 24
    except ValueError:
        return await message.reply_text("❌ Invalid user ID or hours.")

    verify.add_validity(target_id, int(hours * 3600))
    remaining = verify.time_remaining(target_id)
    await message.reply_text(
        f"✅ **Access granted!**\n\n"
        f"👤 User: `{target_id}`\n"
        f"⏳ Valid for: **{remaining}**"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /alive
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("alive"))
async def alive_cmd(client, message: Message):
    await message.reply_text(
        "✅ **Bot working, you can use it!**",
        reply_markup=build_main_keyboard()
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /help
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("help") & allowed)
async def help_cmd(client, message: Message):
    await message.reply_text(
        "🛠 **Bot Help Menu**\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎥 **RECORDING**\n"
        "```\n/rec http://link 00:00:00 Filename\n```\n"
        "📥 **STREAM DOWNLOAD**\n"
        "```\n/download http://link Filename\n```\n"
        "🌐 **OTT / YouTube DOWNLOAD**\n"
        "```\n/ott_download https://youtube.com/... Filename\n```\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ **All Commands:**\n"
        "• 🎥 `/rec` — Record stream with duration\n"
        "• 📥 `/download` — Download full stream\n"
        "• 🌐 `/ott_download` — OTT/YouTube download\n"
        "• ⏰ `/schedule` — Pre-schedule a recording\n"
        "• 📋 `/schedules` — List pending schedules\n"
        "• 🗑 `/cancel_schedule` — Remove a schedule\n"
        "• 🗜 `/compress` — Compress video _(reply to video)_\n"
        "• 📸 `/screenshot [1-30]` — Screenshots _(reply to video)_\n"
        "• 🛑 `/cancel` — Stop active task\n"
        "• 📊 `/status` — All active tasks\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⏰ **Scheduling:**\n"
        "```\n"
        "/schedule 21:00 http://link 01:30:00 ShowName\n"
        "/schedule 09:30 dl http://vod.m3u8 Movie\n"
        "/schedule 18:00 ott https://yt/... Film\n"
        "```\n\n"
        "🍪 **Cookies:**\n"
        "• `/cookies_add` — Upload cookies.txt\n"
        "• `/cookies_status` — Check cookie info\n"
        "• `/del_cookies` — Delete cookies\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 **Features:**\n"
        "• 🎵 Multi audio track selection\n"
        "• 🖼 Watermark (5 positions + custom text)\n"
        "• ⏩ Auto mode: First+Last 1min _(rec only)_\n"
        "• 🔢 Up to **3 simultaneous** tasks\n\n"
        f"🔸 Default filename: `{config.DEFAULT_FILENAME}`",
        reply_markup=build_main_keyboard(),
        disable_web_page_preview=True
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /status
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("status") & allowed)
async def status_cmd(client, message: Message):
    uid  = message.from_user.id
    jobs = user_status.get(uid, {})
    if not jobs:
        return await message.reply(
            "📭 No active recording tasks found.",
            reply_markup=build_main_keyboard()
        )
    lines = [f"📊 **Active Recordings ({len(jobs)}/{MAX_CONCURRENT})**\n"]
    for job_id, status in sorted(jobs.items()):
        n        = slot_number(job_id)
        emoji    = SLOT_EMOJI[n - 1]
        start_dt = datetime.fromtimestamp(status["id"], tz=tz).strftime("%I:%M:%S %p")
        target_s = time_to_seconds(status["target"]) if status["target"] != "∞" else 0
        prog_s   = time_to_seconds(status["progress"])
        remaining = max(target_s - prog_s, 0)
        eta      = TimeFormatter(remaining * 1000) if target_s else "—"
        lines.append(
            f"{emoji} **Slot {n}**\n"
            f"  📁 `{status['filename']}`\n"
            f"  ⏱ `{status['progress']}` / `{status['target']}`\n"
            f"  ⏳ ETA: `{eta}`  🕒 Started: `{start_dt}`\n"
        )
    lines.append("🛑 Use /cancel to stop a recording")
    await message.reply_text("\n".join(lines), reply_markup=build_main_keyboard())


# ─────────────────────────────────────────────────────────────────────────────
#  /history
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("history") & allowed)
async def history_cmd(client: Client, message: Message):
    user_id   = message.from_user.id
    is_owner  = user_id in config.OWNER_ID
    args      = message.command[1:]

    # -- parse optional flags --------------------------------------------------
    # /history            → my last 10 entries
    # /history all        → my all entries (owner: global all)
    # /history stats      → aggregated stats
    # /history @user      → owner: filter by username
    show_all   = "all"   in args
    show_stats = "stats" in args
    filter_u   = next((a for a in args if a.startswith("@")), None)

    # -- select entries --------------------------------------------------------
    if is_owner and (show_all or filter_u):
        entries = list(history_log)
    else:
        entries = [e for e in history_log if e["user_id"] == user_id]

    if filter_u:
        fname = filter_u.lstrip("@").lower()
        entries = [e for e in entries if fname in (e.get("username") or "").lower()]

    if not entries:
        return await message.reply_text(
            "📭 **No history yet.**\n\nActivities appear here after recordings/downloads complete.",
            reply_markup=build_main_keyboard()
        )

    # -- stats view ------------------------------------------------------------
    if show_stats:
        total   = len(history_log) if is_owner else len(entries)
        done    = sum(1 for e in entries if e["status"] == "done")
        canc    = sum(1 for e in entries if e["status"] == "cancelled")
        failed  = sum(1 for e in entries if e["status"] == "failed")
        recs    = sum(1 for e in entries if e["type"] == "rec")
        dls     = sum(1 for e in entries if e["type"] == "download")
        otts    = sum(1 for e in entries if e["type"] == "ott")
        tot_dur = sum(e.get("duration_s", 0) for e in entries)
        tot_mb  = sum(e.get("size_mb", 0) for e in entries)

        # per-user breakdown (owner only)
        user_block = ""
        if is_owner:
            from collections import Counter
            uc = Counter(f"{e.get('username','?')} ({e['user_id']})" for e in history_log)
            top5 = uc.most_common(5)
            user_block = (
                "\n━━━━━━━━━━━━━━━━━━━━\n"
                "👤 **Top Users:**\n" +
                "\n".join(f"  {i+1}. `{u}` — {c} tasks" for i, (u, c) in enumerate(top5))
            )

        await message.reply_text(
            f"📊 **History Stats**{'  (Global)' if is_owner else ''}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 **Total activities:** `{total}`\n"
            f"✅ **Completed:**        `{done}`\n"
            f"⚠️ **Cancelled:**        `{canc}`\n"
            f"❌ **Failed:**           `{failed}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎥 **Recordings:**  `{recs}`\n"
            f"📥 **Downloads:**   `{dls}`\n"
            f"🌐 **OTT/YouTube:** `{otts}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ **Total duration:** `{TimeFormatter(tot_dur * 1000)}`\n"
            f"💾 **Total size:**    `{tot_mb:.1f} MB`"
            f"{user_block}",
            reply_markup=build_main_keyboard()
        )
        return

    # -- list view -------------------------------------------------------------
    limit   = len(entries) if show_all else min(15, len(entries))
    recent  = entries[-limit:][::-1]   # newest first

    TYPE_EMOJI   = {"rec": "🎥", "download": "📥", "ott": "🌐"}
    STATUS_EMOJI = {"done": "✅", "cancelled": "⚠️", "failed": "❌"}

    lines = [f"📋 **Activity History** ({'Global · ' if is_owner and show_all else ''}last {len(recent)})\n"]
    for e in recent:
        dt      = datetime.fromtimestamp(e["ts"], tz).strftime("%d %b %I:%M %p")
        t_emoji = TYPE_EMOJI.get(e["type"], "📁")
        s_emoji = STATUS_EMOJI.get(e["status"], "❓")
        dur_str = TimeFormatter(e.get("duration_s", 0) * 1000) if e.get("duration_s") else "—"
        mb_str  = f"{e['size_mb']} MB" if e.get("size_mb") else "—"
        user_tag = f" · `@{e['username']}`" if is_owner else ""

        extra = ""
        if e["type"] == "ott" and e.get("res_label"):
            extra = f" · `{e['res_label']}` `{e.get('audio_label','')}`"

        lines.append(
            f"{t_emoji}{s_emoji} **{e['filename']}**{user_tag}\n"
            f"   ⏱ `{dur_str}` · 💾 `{mb_str}` · 🕒 `{dt}`{extra}\n"
        )

    if len(entries) > limit:
        lines.append(f"\n_…{len(entries) - limit} more. Use /history all to see everything._")

    lines.append("\n📊 /history stats — aggregated totals")
    await message.reply_text("\n".join(lines), reply_markup=build_main_keyboard())


# ─────────────────────────────────────────────────────────────────────────────
#  Schedule helpers
# ─────────────────────────────────────────────────────────────────────────────

def _next_sch_id(user_id: int) -> str:
    _sch_counter[user_id] = _sch_counter.get(user_id, 0) + 1
    return f"S{_sch_counter[user_id]}"


def _parse_schedule_time(time_str: str) -> Optional[datetime]:
    """Parse HH:MM or HH:MM:SS into the next upcoming IST datetime (today or tomorrow)."""
    now = datetime.now(tz)
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(time_str, fmt)
            target = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target
        except ValueError:
            continue
    return None


def _format_wait(seconds: float) -> str:
    """Human-readable countdown string."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


async def _schedule_waiter(client: Client, user_id: int, chat_id: int,
                            sch_id: str, job: dict):
    """Waits until target_dt then fires the recording/download."""
    now    = datetime.now(tz)
    wait_s = max((job["target_dt"] - now).total_seconds(), 0)
    await asyncio.sleep(wait_s)

    # Check if still in the dict (not cancelled)
    if sch_id not in scheduled_jobs.get(user_id, {}):
        return

    scheduled_jobs.get(user_id, {}).pop(sch_id, None)

    kind     = job["kind"]          # "rec" | "download" | "ott_download"
    url      = job["url"]
    filename = job["filename"]
    duration = job.get("duration", "")

    fire_time = datetime.now(tz).strftime("%I:%M:%S %p")

    await client.send_message(
        chat_id,
        f"⏰ **Schedule {sch_id} Fired!**\n\n"
        f"🕒 **Time:** `{fire_time} IST`\n"
        f"📁 **File:** `{filename}`\n"
        f"🔗 **URL:** `{url[:60]}{'…' if len(url) > 60 else ''}`\n\n"
        f"🚀 Starting `/{kind}` now…",
        reply_markup=build_main_keyboard(),
    )

    # Synthesise a fake Message-like object to reuse existing command handlers
    # by dispatching a real bot message instead
    cmd_text = {
        "rec":          f"/rec {url} {duration} {filename}",
        "download":     f"/download {url} {filename}",
        "ott_download": f"/ott_download {url} {filename}",
    }.get(kind, f"/rec {url} {duration} {filename}")

    await client.send_message(chat_id, cmd_text)


# ─────────────────────────────────────────────────────────────────────────────
#  /schedule HH:MM url DURATION filename   (duration required only for /rec)
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("schedule") & allowed)
async def schedule_cmd(client: Client, message: Message):
    """
    Usage:
      /schedule 21:00     http://link 01:00:00 Filename   ← rec (with duration)
      /schedule 21:00 dl  http://link          Filename   ← download (no dur)
      /schedule 21:00 ott https://yt/...       Filename   ← ott_download (no dur)
    """
    args = message.command[1:]   # everything after /schedule
    user_id = message.from_user.id

    def _usage():
        return message.reply_text(
            "❌ **Invalid format.**\n\n"
            "📌 **Usage:**\n"
            "```\n"
            "/schedule HH:MM URL 00:00:00 Filename\n"
            "/schedule HH:MM dl URL Filename\n"
            "/schedule HH:MM ott https://... Filename\n"
            "```\n\n"
            "Examples:\n"
            "• `/schedule 21:00 http://stream 01:30:00 NightShow`\n"
            "• `/schedule 09:30 dl http://vod.m3u8 Morning`\n"
            "• `/schedule 18:00 ott https://youtube.com/... Movie`",
            reply_markup=build_main_keyboard(),
            disable_web_page_preview=True,
        )

    if len(args) < 3:
        return await _usage()

    time_str = args[0]
    target_dt = _parse_schedule_time(time_str)
    if not target_dt:
        return await message.reply_text(
            "❌ Invalid time format. Use **HH:MM** or **HH:MM:SS** (24-hour IST).",
            reply_markup=build_main_keyboard()
        )

    # Detect kind keyword
    kind = "rec"
    rest = args[1:]
    if rest[0].lower() in ("dl", "download"):
        kind = "download"
        rest = rest[1:]
    elif rest[0].lower() in ("ott", "ott_download"):
        kind = "ott_download"
        rest = rest[1:]

    if not rest:
        return await _usage()

    url = rest[0]
    rest = rest[1:]

    duration = ""
    if kind == "rec":
        if not rest:
            return await _usage()
        # Next token is duration if it looks like HH:MM:SS
        if rest[0].count(":") >= 1:
            duration = rest[0]
            rest = rest[1:]
        else:
            return await _usage()

    filename = " ".join(rest).strip() if rest else config.DEFAULT_FILENAME

    sch_id = _next_sch_id(user_id)
    job = {
        "kind":      kind,
        "url":       url,
        "filename":  filename,
        "duration":  duration,
        "time_str":  time_str,
        "target_dt": target_dt,
    }

    scheduled_jobs.setdefault(user_id, {})[sch_id] = job
    job["task"] = asyncio.create_task(
        _schedule_waiter(client, user_id, message.chat.id, sch_id, job)
    )

    wait_s     = (target_dt - datetime.now(tz)).total_seconds()
    fire_label = target_dt.strftime("%I:%M %p")
    day_label  = "today" if target_dt.date() == datetime.now(tz).date() else "tomorrow"
    kind_emoji = {"rec": "🎥", "download": "📥", "ott_download": "🌐"}.get(kind, "🎥")

    dur_line = f"⏱ **Duration:** `{duration}`\n" if duration else ""
    await message.reply_text(
        f"✅ **Schedule {sch_id} Created!**\n\n"
        f"{kind_emoji} **Type:** `/{kind}`\n"
        f"🕒 **Fire at:** `{fire_label} IST` ({day_label})\n"
        f"⏳ **In:** `{_format_wait(wait_s)}`\n"
        f"{dur_line}"
        f"📁 **File:** `{filename}`\n"
        f"🔗 **URL:** `{url[:60]}{'…' if len(url) > 60 else ''}`\n\n"
        f"📋 Use /schedules to see all · /cancel_schedule {sch_id} to remove",
        reply_markup=build_main_keyboard(),
        disable_web_page_preview=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /schedules  — list all pending scheduled jobs for the user
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("schedules") & allowed)
async def schedules_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    jobs    = scheduled_jobs.get(user_id, {})
    if not jobs:
        return await message.reply_text(
            "📭 **No pending schedules.**\n\n"
            "Use /schedule to create one.",
            reply_markup=build_main_keyboard()
        )

    now   = datetime.now(tz)
    lines = [f"📋 **Pending Schedules ({len(jobs)})**\n"]
    kind_emoji = {"rec": "🎥", "download": "📥", "ott_download": "🌐"}

    for sid, job in sorted(jobs.items()):
        wait_s    = max((job["target_dt"] - now).total_seconds(), 0)
        fire_time = job["target_dt"].strftime("%I:%M %p")
        day_label = "today" if job["target_dt"].date() == now.date() else "tomorrow"
        k_emoji   = kind_emoji.get(job["kind"], "🎥")
        dur_part  = f" · `{job['duration']}`" if job.get("duration") else ""
        lines.append(
            f"{k_emoji} **{sid}** — fires `{fire_time}` {day_label} _(in {_format_wait(wait_s)})_\n"
            f"   📁 `{job['filename']}`{dur_part}\n"
            f"   🔗 `{job['url'][:50]}{'…' if len(job['url']) > 50 else ''}`\n"
        )

    lines.append("🗑 /cancel_schedule <ID> to remove one")
    await message.reply_text("\n".join(lines), reply_markup=build_main_keyboard())


# ─────────────────────────────────────────────────────────────────────────────
#  /cancel_schedule <ID>
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("cancel_schedule") & allowed)
async def cancel_schedule_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    args    = message.command[1:]

    if not args:
        # Show list if no ID given
        jobs = scheduled_jobs.get(user_id, {})
        if not jobs:
            return await message.reply_text(
                "📭 No pending schedules to cancel.",
                reply_markup=build_main_keyboard()
            )
        ids = ", ".join(sorted(jobs.keys()))
        return await message.reply_text(
            f"❓ **Which schedule to cancel?**\n\n"
            f"Pending: `{ids}`\n\n"
            f"Usage: `/cancel_schedule S1`",
            reply_markup=build_main_keyboard()
        )

    sch_id  = args[0].upper()
    user_js = scheduled_jobs.get(user_id, {})

    if sch_id not in user_js:
        return await message.reply_text(
            f"❌ Schedule `{sch_id}` not found.\n"
            f"Use /schedules to see pending ones.",
            reply_markup=build_main_keyboard()
        )

    job = user_js.pop(sch_id)
    task: asyncio.Task = job.get("task")
    if task and not task.done():
        task.cancel()

    await message.reply_text(
        f"✅ **Schedule {sch_id} cancelled.**\n\n"
        f"📁 `{job['filename']}` @ `{job['time_str']} IST`",
        reply_markup=build_main_keyboard()
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /cancel
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("cancel") & allowed)
async def cancel_command(client, message: Message):
    user_id = message.from_user.id

    if user_id in user_setup:
        user_setup.pop(user_id, None)
        return await message.reply_text(
            "❌ **Setup cancelled.**",
            reply_markup=build_main_keyboard()
        )

    jobs = user_tasks.get(user_id, {})
    if not jobs:
        return await message.reply_text(
            "❌ **No active recording to cancel!**",
            reply_markup=build_main_keyboard()
        )

    if len(jobs) == 1:
        job_id = list(jobs.keys())[0]
        await do_cancel_job(user_id, job_id, message)
        await message.reply_text("✅ Done.", reply_markup=build_main_keyboard())
    else:
        user_setup.setdefault(user_id, {})["step"] = "cancel"
        await message.reply_text(
            f"📋 **You have {len(jobs)} active recordings.**\nWhich one to cancel?",
            reply_markup=build_cancel_keyboard(user_id)
        )


async def do_cancel_job(user_id: int, job_id: str, ref_message: Message):
    job_key = make_job_key(user_id, job_id)
    cancelled_jobs.add(job_key)

    if user_id in progress_tasks and job_id in progress_tasks[user_id]:
        progress_tasks[user_id][job_id].cancel()
        del progress_tasks[user_id][job_id]

    if user_id in user_ffmpeg_pids and job_id in user_ffmpeg_pids[user_id]:
        pid = user_ffmpeg_pids[user_id][job_id]
        try:
            parent   = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except Exception:
                    pass
            parent.kill()
            psutil.wait_procs([parent] + children, timeout=3)
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            LOG.error(f"Kill FFmpeg error: {e}")
        del user_ffmpeg_pids[user_id][job_id]

    info     = user_status.get(user_id, {}).get(job_id, {})
    filename = info.get("filename", "Unknown")
    n        = slot_number(job_id)
    emoji    = SLOT_EMOJI[n - 1]

    await ref_message.reply_text(
        f"✅ **Recording Cancelled!**\n\n"
        f"{emoji} **Slot {n}:** `{filename}`\n"
        f"🛑 Stopped — uploading recorded portion..."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /rec
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("rec") & allowed)
async def rec_command(client, message: Message):
    if len(message.command) < 3:
        return await message.reply_text(
            "❌ **Invalid Format!**\n\n"
            "📌 **Usage:**\n"
            "```\n/rec http://link 00:00:00 filename\n```",
            reply_markup=build_main_keyboard()
        )
    user_id = message.from_user.id
    if len(user_tasks.get(user_id, {})) >= MAX_CONCURRENT:
        return await message.reply_text(
            f"❌ **Maximum {MAX_CONCURRENT} simultaneous recordings reached!**\n"
            f"📊 /status  |  🛑 /cancel",
            reply_markup=build_main_keyboard()
        )
    params       = " ".join(message.command[1:])
    parts        = params.split(" ", 2)
    url          = parts[0]
    timestamp    = parts[1]
    raw_filename = parts[2].strip() if len(parts) > 2 else config.DEFAULT_FILENAME

    msg  = await message.reply_text("🔍 **Detecting stream quality... Please wait.**")
    try:
        info = await detect_stream_info(url)
    except Exception as e:
        LOG.error(f"rec detect_stream_info error: {e}")
        await msg.edit_text(
            f"❌ **Stream detection failed!**\n\n`{e}`\n\n"
            "Stream URL check karein aur dobara try karein."
        )
        return

    tracks   = info["tracks"]
    video    = info["video"]
    selected = set(t["index"] for t in tracks)

    user_setup[user_id] = {
        "mode": "record",
        "step": "audio" if tracks else "watermark",
        "url": url, "timestamp": timestamp,
        "filename": raw_filename,
        "tracks": tracks, "selected_tracks": selected,
        "watermark_pos": None,
        "watermark_text": config.DEFAULT_FILENAME,
        "auto_mode": False, "video_size": "original",
        "chat_id": message.chat.id, "reply_to": message.id,
        "video_info": video,
    }

    quality_line = format_quality_line(video)
    audio_line   = ", ".join(t["label"] for t in tracks) if tracks else "No audio detected"

    if tracks:
        text = (
            f"✅ **Stream Detected!**\n\n"
            f"📺 **Quality:** `{quality_line}`\n"
            f"🎵 **Audio:** `{audio_line}`\n"
            f"⏱ **Duration:** `{timestamp}`\n"
            f"📁 **File:** `{raw_filename}`\n\n"
            f"👇 Select audio tracks to include:"
        )
        kb = build_audio_keyboard(tracks, selected)
    else:
        text = (
            f"✅ **Stream Detected!**\n\n"
            f"📺 **Quality:** `{quality_line}`\n"
            f"🎵 **Audio:** No tracks — will auto-select\n\n"
        ) + setup_summary_text(user_setup[user_id])
        kb = build_watermark_keyboard(user_setup[user_id])

    try:
        await msg.delete()
    except Exception:
        pass
    await message.reply_text(text, reply_markup=kb)


# ─────────────────────────────────────────────────────────────────────────────
#  /download
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("download") & allowed)
async def download_command(client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "❌ **Invalid Format!**\n\n"
            "📌 **Usage:**\n"
            "```\n/download http://link filename\n```",
            reply_markup=build_main_keyboard()
        )
    user_id = message.from_user.id
    if len(user_tasks.get(user_id, {})) >= MAX_CONCURRENT:
        return await message.reply_text(
            f"❌ **Maximum {MAX_CONCURRENT} simultaneous tasks reached!**",
            reply_markup=build_main_keyboard()
        )
    params       = " ".join(message.command[1:])
    parts        = params.split(" ", 1)
    url          = parts[0]
    raw_filename = parts[1].strip() if len(parts) > 1 else config.DEFAULT_FILENAME

    msg  = await message.reply_text("🔍 **Detecting stream quality... Please wait.**")
    try:
        info = await detect_stream_info(url)
    except Exception as e:
        LOG.error(f"download detect_stream_info error: {e}")
        await msg.edit_text(
            f"❌ **Stream detection failed!**\n\n`{e}`\n\n"
            "URL check karein aur dobara try karein."
        )
        return

    tracks   = info["tracks"]
    video    = info["video"]
    selected = set(t["index"] for t in tracks)

    user_setup[user_id] = {
        "mode": "download",
        "step": "audio" if tracks else "watermark",
        "url": url, "timestamp": None,
        "filename": raw_filename,
        "tracks": tracks, "selected_tracks": selected,
        "watermark_pos": None,
        "watermark_text": config.DEFAULT_FILENAME,
        "auto_mode": False, "video_size": "original",
        "chat_id": message.chat.id, "reply_to": message.id,
        "video_info": video,
    }

    quality_line = format_quality_line(video)
    audio_line   = ", ".join(t["label"] for t in tracks) if tracks else "No audio detected"

    if tracks:
        text = (
            f"✅ **Stream Detected!**\n\n"
            f"📺 **Quality:** `{quality_line}`\n"
            f"🎵 **Audio:** `{audio_line}`\n"
            f"📁 **File:** `{raw_filename}`\n\n"
            f"👇 Select audio tracks to include:"
        )
        kb = build_audio_keyboard(tracks, selected)
    else:
        text = (
            f"✅ **Stream Detected!**\n\n"
            f"📺 **Quality:** `{quality_line}`\n"
            f"🎵 **Audio:** No tracks — will auto-select\n\n"
        ) + setup_summary_text(user_setup[user_id])
        kb = build_watermark_keyboard(user_setup[user_id])

    try:
        await msg.delete()
    except Exception:
        pass
    await message.reply_text(text, reply_markup=kb)


# ─────────────────────────────────────────────────────────────────────────────
#  Main text router — handles ALL ReplyKeyboard button presses
# ─────────────────────────────────────────────────────────────────────────────

_COMMANDS = [
    "start", "alive", "help", "status", "cancel", "rec", "download",
    "ott_download", "compress", "screenshot",
    "cookies_add", "cookies_status", "del_cookies",
    "schedule", "schedules", "cancel_schedule",
    "verify", "history",
]


@app.on_message(filters.text & allowed & ~filters.command(_COMMANDS))
async def text_router(client: Client, message: Message):
    user_id = message.from_user.id
    text    = message.text.strip()
    setup   = user_setup.get(user_id, {})
    step    = setup.get("step", "")

    # ── Main menu shortcuts ─────────────────────────────────────────────────
    if text == "📖 Help":
        return await help_cmd(client, message)
    if text == "📊 Status":
        return await status_cmd(client, message)
    if text in ("🎥 Record", "📥 Download", "🌐 OTT Download",
                "🗜 Compress", "📸 Screenshot", "🍪 Cookies"):
        hints = {
            "🎥 Record":       "📌 Usage:\n`/rec http://link 00:00:00 Filename`",
            "📥 Download":     "📌 Usage:\n`/download http://link Filename`",
            "🌐 OTT Download": "📌 Usage:\n`/ott_download https://youtube.com/... Filename`",
            "🗜 Compress":     "📌 Reply to a video and send `/compress`",
            "📸 Screenshot":   "📌 Reply to a video and send `/screenshot [1-30]`",
            "🍪 Cookies":      "📌 Use `/cookies_add` to upload, `/cookies_status` to check, `/del_cookies` to remove",
        }
        return await message.reply_text(hints[text], reply_markup=build_main_keyboard())

    # ── Audio track selection ───────────────────────────────────────────────
    if step == "audio":
        return await _handle_audio(client, message, text, setup, user_id)

    # ── Watermark setup ─────────────────────────────────────────────────────
    if step == "watermark":
        return await _handle_watermark(client, message, text, setup, user_id)

    # ── Watermark text input ────────────────────────────────────────────────
    if step == "wm_text_input":
        setup["watermark_text"] = text
        setup["step"] = "watermark"
        return await message.reply_text(
            f"✅ **Watermark text set to:** `{text}`\n\n" + setup_summary_text(setup),
            reply_markup=build_watermark_keyboard(setup)
        )

    # ── Video size selection ────────────────────────────────────────────────
    if step == "size":
        return await _handle_size(client, message, text, setup, user_id)

    # ── Cancel job selection ────────────────────────────────────────────────
    if step == "cancel":
        return await _handle_cancel(client, message, text, user_id)

    # ── Compress quality selection ──────────────────────────────────────────
    if step == "compress":
        return await _handle_compress(client, message, text, setup, user_id)

    # ── OTT resolution selection ────────────────────────────────────────────
    if step == "ott_resolution":
        return await _handle_ott_resolution(client, message, text, setup, user_id)

    # ── OTT audio language selection ────────────────────────────────────────
    if step == "ott_audio":
        return await _handle_ott_audio(client, message, text, setup, user_id)


# ─────────────────────────────────────────────────────────────────────────────
#  Step handlers
# ─────────────────────────────────────────────────────────────────────────────

async def _handle_audio(client, message: Message, text: str, setup: dict, user_id: int):
    tracks         = setup.get("tracks", [])
    selected: set  = setup.get("selected_tracks", set())

    # Strip ✅/❌ prefix to match track label
    clean = text.lstrip("✅❌ ").strip()
    matched = next((t for t in tracks if t["label"] == clean), None)

    if matched:
        idx = matched["index"]
        selected.discard(idx) if idx in selected else selected.add(idx)
        setup["selected_tracks"] = selected
        sel_count = len(selected)
        return await message.reply_text(
            f"🎵 **Audio Tracks** — {sel_count}/{len(tracks)} selected\n"
            f"Selected: `{', '.join(t['label'] for t in tracks if t['index'] in selected) or 'None'}`",
            reply_markup=build_audio_keyboard(tracks, selected)
        )

    if text == "🔁 Select All Tracks":
        setup["selected_tracks"] = set() if len(selected) == len(tracks) else set(t["index"] for t in tracks)
        label = "all deselected" if not setup["selected_tracks"] else "all selected"
        return await message.reply_text(
            f"🔁 Tracks {label}.",
            reply_markup=build_audio_keyboard(tracks, setup["selected_tracks"])
        )

    if text == "◀️ Back":
        user_setup.pop(user_id, None)
        return await message.reply_text("↩️ Setup cancelled.", reply_markup=build_main_keyboard())

    if text == "✅ Next: Watermark":
        setup["step"] = "watermark"
        return await message.reply_text(
            setup_summary_text(setup),
            reply_markup=build_watermark_keyboard(setup)
        )

    if text == "❌ Cancel Setup":
        user_setup.pop(user_id, None)
        return await message.reply_text("❌ Setup cancelled.", reply_markup=build_main_keyboard())


async def _handle_watermark(client, message: Message, text: str, setup: dict, user_id: int):
    mode  = setup.get("mode", "record")
    clean = text.lstrip("✅ ").strip()

    # Position buttons
    if clean in WM_LABEL_TO_KEY:
        setup["watermark_pos"] = WM_LABEL_TO_KEY[clean]
        return await message.reply_text(
            f"✅ **Watermark position:** {clean}\n\n" + setup_summary_text(setup),
            reply_markup=build_watermark_keyboard(setup)
        )

    if "Watermark OFF" in text:
        setup["watermark_pos"] = None
        return await message.reply_text(
            "🚫 **Watermark disabled.**\n\n" + setup_summary_text(setup),
            reply_markup=build_watermark_keyboard(setup)
        )

    if text == "✏️ Change Watermark Text":
        setup["step"] = "wm_text_input"
        return await message.reply_text(
            "✏️ **Type the new watermark text and send it:**",
            reply_markup=ReplyKeyboardRemove()
        )

    if "Auto: First+Last" in text:
        setup["auto_mode"] = not setup.get("auto_mode", False)
        s = "✅ ON" if setup["auto_mode"] else "❌ OFF"
        return await message.reply_text(
            f"⏱️ **Auto Mode:** {s}\n\n" + setup_summary_text(setup),
            reply_markup=build_watermark_keyboard(setup)
        )

    if text == "📐 Next: Video Size →":
        setup["step"] = "size"
        return await message.reply_text(
            "📐 **Select Video Size:**",
            reply_markup=build_size_keyboard(setup.get("video_size", "original"))
        )

    if text == "📥 START DOWNLOAD":
        setup["step"] = "running"
        await message.reply_text("📥 **Starting download...**", reply_markup=build_main_keyboard())
        s = user_setup.pop(user_id)
        asyncio.create_task(handle_record(client, message, s, user_id))
        return

    if text == "❌ Cancel":
        user_setup.pop(user_id, None)
        return await message.reply_text("❌ Setup cancelled.", reply_markup=build_main_keyboard())


async def _handle_size(client, message: Message, text: str, setup: dict, user_id: int):
    clean = text.lstrip("✅ ").strip()

    if clean in SIZE_LABEL_TO_KEY:
        setup["video_size"] = SIZE_LABEL_TO_KEY[clean]
        return await message.reply_text(
            f"✅ **Size selected:** {clean}\n\n" + setup_summary_text(setup),
            reply_markup=build_size_keyboard(setup["video_size"])
        )

    if text == "◀️ Back to Watermark":
        setup["step"] = "watermark"
        return await message.reply_text(
            setup_summary_text(setup),
            reply_markup=build_watermark_keyboard(setup)
        )

    if text == "▶️ Start Recording":
        # Owners / AUTH_USERS ka unlimited access hai — normal users ka rec_limit check karo
        is_unlimited = user_id in config.OWNER_ID or user_id in config.AUTH_USERS
        ok, use_msg = limit_system.use_rec(user_id, unlimited=is_unlimited)
        if not ok:
            user_setup.pop(user_id, None)
            return await message.reply_text(
                f"❌ **Rec Limit Khatam!**\n\n{use_msg}\n\n"
                "📊 /limit — apni limit dekhen\n"
                "🔐 /verify — aur Rec unlock karein",
                reply_markup=build_main_keyboard()
            )
        setup["step"] = "running"
        await message.reply_text("🎬 **Starting recording...**", reply_markup=build_main_keyboard())
        s = user_setup.pop(user_id)
        asyncio.create_task(handle_record(client, message, s, user_id))
        return

    if text == "❌ Cancel":
        user_setup.pop(user_id, None)
        return await message.reply_text("❌ Setup cancelled.", reply_markup=build_main_keyboard())


async def _handle_cancel(client, message: Message, text: str, user_id: int):
    if text == "❌ Cancel ALL":
        jobs = list(user_tasks.get(user_id, {}).keys())
        for job_id in jobs:
            await do_cancel_job(user_id, job_id, message)
        user_setup.pop(user_id, None)
        return await message.reply_text("✅ **All recordings cancelled.**", reply_markup=build_main_keyboard())

    if text == "◀️ Close Menu":
        user_setup.pop(user_id, None)
        return await message.reply_text("↩️ Menu closed.", reply_markup=build_main_keyboard())

    for job_id, info in list(user_status.get(user_id, {}).items()):
        n = slot_number(job_id)
        if f"Cancel Slot {n}:" in text:
            await do_cancel_job(user_id, job_id, message)
            user_setup.pop(user_id, None)
            return await message.reply_text(
                f"✅ Slot {n} cancelled.", reply_markup=build_main_keyboard()
            )

    await message.reply_text("❓ Unknown option.", reply_markup=build_cancel_keyboard(user_id))


async def _handle_compress(client, message: Message, text: str, setup: dict, user_id: int):
    if text == "❌ Cancel Compress":
        compress_pending.pop(user_id, None)
        user_setup.pop(user_id, None)
        return await message.reply_text("❌ Compression cancelled.", reply_markup=build_main_keyboard())

    if text not in COMPRESS_PRESETS:
        return await message.reply_text("❓ Please choose a quality option.", reply_markup=build_compress_keyboard())

    video_msg_id = compress_pending.pop(user_id, None)
    if not video_msg_id:
        user_setup.pop(user_id, None)
        return await message.reply_text(
            "❌ Session expired. Reply to video and use /compress again.",
            reply_markup=build_main_keyboard()
        )

    ffmpeg_args, quality_desc = COMPRESS_PRESETS[text]
    user_setup.pop(user_id, None)

    if len(user_tasks.get(user_id, {})) >= MAX_CONCURRENT:
        return await message.reply_text(
            f"❌ All {MAX_CONCURRENT} slots busy. Cancel one first.",
            reply_markup=build_main_keyboard()
        )

    job_id  = next_job_id(user_id)
    if not job_id:
        return
    job_key  = make_job_key(user_id, job_id)
    n        = slot_number(job_id)
    emoji_s  = SLOT_EMOJI[n - 1]
    save_dir = join(config.DOWNLOAD_DIRECTORY, f"{int(time.time())}_{job_id}_compress")
    os.makedirs(save_dir, exist_ok=True)

    user_tasks.setdefault(user_id, {})[job_id] = time.time()
    user_status.setdefault(user_id, {})[job_id] = {
        "id": int(time.time()), "filename": "Compressed Video",
        "target": "∞", "progress": "00:00:00",
        "save_dir": save_dir, "mode": "compress",
    }

    msg = await message.reply_text(
        f"{emoji_s} **Slot {n} — Starting compression ({quality_desc})...**",
        reply_markup=build_main_keyboard()
    )

    async def do_compress():
        try:
            await msg.edit_text(f"{emoji_s} **Slot {n} — Downloading original video...**")
            orig_path     = join(save_dir, "original.mkv")
            video_message = await client.get_messages(message.chat.id, video_msg_id)
            if not video_message or not get_video_media(video_message):
                raise Exception("Original video message not found.")
            await client.download_media(video_message, file_name=orig_path)

            if not os.path.exists(orig_path) or os.path.getsize(orig_path) == 0:
                raise Exception("Download failed or file is empty.")

            orig_size_mb = os.path.getsize(orig_path) / (1024 * 1024)
            await msg.edit_text(
                f"{emoji_s} **Slot {n} — Compressing...**\n"
                f"📦 Original: `{orig_size_mb:.1f} MB`  🎛 `{quality_desc}`"
            )

            out_path = join(save_dir, "compressed.mkv")
            rc, _, err = await runcmd(f'ffmpeg -y -i "{orig_path}" {ffmpeg_args} "{out_path}"')
            if rc != 0:
                raise Exception(f"FFmpeg error:\n{err[-1500:]}")

            new_size_mb = os.path.getsize(out_path) / (1024 * 1024)
            reduction   = max(0, (1 - new_size_mb / orig_size_mb) * 100)

            dur = await get_duration_ffmpeg(out_path)
            rand_sec = random.randint(5, max(dur - 5, 6)) if dur > 10 else 1
            thumb_path = join(save_dir, "thumb.jpg")
            await runcmd(f'ffmpeg -y -ss {rand_sec} -i "{out_path}" -vframes 1 -q:v 2 "{thumb_path}"')

            caption = (
                f"🗜 **Compressed Video**\n\n"
                f"📦 **Original:** `{orig_size_mb:.1f} MB`\n"
                f"📉 **Compressed:** `{new_size_mb:.1f} MB`\n"
                f"✂️ **Reduction:** `{reduction:.1f}%`\n"
                f"🎛 **Quality:** `{quality_desc}`\n\n"
                f"✅ _Compression completed!_"
            )
            start_time = time.time()
            await msg.reply_video(
                video=out_path, caption=caption, duration=dur,
                thumb=thumb_path if os.path.exists(thumb_path) else None,
                progress=progress_for_pyrogram,
                progress_args=(msg, start_time, msg, save_dir, False, job_id)
            )
            shutil.rmtree(save_dir, ignore_errors=True)

        except Exception as e:
            LOG.error(f"compress error [{job_id}]: {e}")
            try:
                await msg.edit_text(f"{emoji_s} **Compression Failed!**\n\n`{str(e)[:2000]}`")
            except Exception:
                pass
            shutil.rmtree(save_dir, ignore_errors=True)
        finally:
            user_tasks.get(user_id, {}).pop(job_id, None)
            user_status.get(user_id, {}).pop(job_id, None)
            cancelled_jobs.discard(job_key)
            for d in [user_tasks, user_status]:
                if user_id in d and not d[user_id]:
                    del d[user_id]

    asyncio.create_task(do_compress())


async def _handle_ott_resolution(client, message: Message, text: str, setup: dict, user_id: int):
    if text == "❌ Cancel OTT":
        user_setup.pop(user_id, None)
        return await message.reply_text("❌ OTT download cancelled.", reply_markup=build_main_keyboard())

    res_map   = setup.get("detected_res_map",   OTT_RES_LABEL_TO_FMT)
    audio_map = setup.get("detected_audio_map", OTT_AUDIO_LANGS)

    clean = text.lstrip("✅ ").strip()
    if clean in res_map:
        setup["ott_res_label"] = clean
        setup["ott_format"]    = res_map[clean]
        setup["step"]          = "ott_audio"
        return await message.reply_text(
            f"✅ **Resolution:** `{clean}`\n\n"
            f"🎧 Now select audio language:",
            reply_markup=build_ott_audio_keyboard_dynamic(audio_map, setup.get("ott_audio_label", ""))
        )

    await message.reply_text("❓ Please pick a resolution.", reply_markup=build_ott_resolution_keyboard_dynamic(res_map, setup.get("ott_res_label", "")))


async def _handle_ott_audio(client, message: Message, text: str, setup: dict, user_id: int):
    res_map   = setup.get("detected_res_map",   OTT_RES_LABEL_TO_FMT)
    audio_map = setup.get("detected_audio_map", OTT_AUDIO_LANGS)

    if text == "❌ Cancel OTT":
        user_setup.pop(user_id, None)
        return await message.reply_text("❌ OTT download cancelled.", reply_markup=build_main_keyboard())

    if text == "◀️ Back to Resolution":
        setup["step"] = "ott_resolution"
        return await message.reply_text(
            "📺 Select resolution:",
            reply_markup=build_ott_resolution_keyboard_dynamic(res_map, setup.get("ott_res_label", ""))
        )

    clean = text.lstrip("✅ ").strip()
    if clean in audio_map:
        setup["ott_audio_label"] = clean
        setup["ott_audio_lang"]  = audio_map[clean]
        setup["step"] = "running"

        title_line = f"📌 `{setup['detected_title'][:50]}`\n" if setup.get("detected_title") else ""
        dur_line   = f"⏱ `{TimeFormatter(setup['detected_duration'] * 1000)}`\n" if setup.get("detected_duration") else ""

        await message.reply_text(
            f"✅ **Setup Complete!**\n\n"
            f"{title_line}{dur_line}"
            f"📺 **Resolution:** `{setup.get('ott_res_label', 'Best')}`\n"
            f"🎧 **Audio:** `{clean}`\n"
            f"📁 **File:** `{setup['filename']}`\n\n"
            f"📥 Starting download...",
            reply_markup=build_main_keyboard()
        )
        s = user_setup.pop(user_id)
        asyncio.create_task(ott_download_task(client, message, s, user_id))
        return

    await message.reply_text("❓ Please pick an audio language.", reply_markup=build_ott_audio_keyboard_dynamic(audio_map, setup.get("ott_audio_label", "")))


# ─────────────────────────────────────────────────────────────────────────────
#  Upload progress callback
# ─────────────────────────────────────────────────────────────────────────────

async def progress_for_pyrogram(current, total, ref_message, start, msg, save_dir,
                                 was_cancelled=False, job_id=None):
    now         = time.time()
    diff        = max(now - start, 1)
    percentage  = current * 100 / total
    speed       = current / diff
    uploaded_mb = current / (1024 * 1024)
    total_mb    = total   / (1024 * 1024)
    speed_mb    = speed   / (1024 * 1024)

    filled     = int(10 * percentage // 100)
    bar_filled = "▰" * filled
    bar_empty  = "▱" * (10 - filled)
    bar        = f"[{bar_filled}{bar_empty}]"

    if int(percentage) in {0, 10, 25, 50, 75, 90, 95, 99, 100} or current == total:
        eta    = TimeFormatter(int((total - current) / speed * 1000)) if speed > 0 else "00:00:00"
        n      = slot_number(job_id) if job_id else 1
        slot_e = SLOT_EMOJI[n - 1] if n <= 3 else "📤"
        label  = "Partial " if was_cancelled else ""
        try:
            await msg.edit_text(
                f"{slot_e} **Uploading {label}Recording**\n"
                f"`{bar}` `{percentage:.1f}%`\n"
                f"📊 `{uploaded_mb:.1f} / {total_mb:.1f} MB`\n"
                f"⚡ `{speed_mb:.1f} MB/s`  ⏳ `{eta}`"
            )
        except Exception:
            pass
        if current == total:
            done = "✅ Partial Sent!" if was_cancelled else "✅ Upload Completed!"
            try:
                await msg.edit_text(f"{done}\n🗑️ Cleaning up...")
                await asyncio.sleep(2)
                await msg.edit_text(done)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
#  Core recording / download logic
# ─────────────────────────────────────────────────────────────────────────────

async def handle_record(client: Client, ref_message: Message, setup: dict, user_id: int):
    job_id = next_job_id(user_id)
    if job_id is None:
        await ref_message.reply_text(f"❌ All {MAX_CONCURRENT} recording slots are busy!")
        return

    job_key        = make_job_key(user_id, job_id)
    n              = slot_number(job_id)
    emoji          = SLOT_EMOJI[n - 1]
    mode           = setup.get("mode", "record")
    url            = setup["url"]
    timestamp      = setup.get("timestamp")
    raw_filename   = setup["filename"]
    tracks         = setup.get("tracks", [])
    selected_tracks = setup.get("selected_tracks", set())
    watermark_pos  = setup.get("watermark_pos")
    watermark_text = setup.get("watermark_text", config.DEFAULT_FILENAME)
    auto_mode      = setup.get("auto_mode", False) if mode == "record" else False
    video_size_key = setup.get("video_size", "original")
    is_download    = (mode == "download")
    action_label   = "Downloading" if is_download else "Recording"

    filename   = f"{raw_filename}.mkv"
    save_dir   = join(config.DOWNLOAD_DIRECTORY, f"{int(time.time())}_{job_id}")
    os.makedirs(save_dir, exist_ok=True)
    video_path = join(save_dir, filename)

    msg = await ref_message.reply_text(
        f"{emoji} **Slot {n} — Initializing {action_label.lower()}...**\n📁 `{raw_filename}`"
    )

    try:
        user_tasks.setdefault(user_id, {})[job_id] = time.time()
        duration = time_to_seconds(timestamp) if timestamp else 0
        user_status.setdefault(user_id, {})[job_id] = {
            "id": int(time.time()), "filename": raw_filename,
            "target": timestamp or "∞", "progress": "00:00:00",
            "save_dir": save_dir, "mode": mode,
        }

        recording_start = time.time()

        if tracks and selected_tracks:
            video_map  = "-map 0:V?"
            audio_maps = " ".join(f"-map 0:a:{t['index']}?" for t in tracks if t["index"] in selected_tracks)
        else:
            video_map  = "-map 0:V?"
            audio_maps = "-map 0:a?"

        meta_args     = build_metadata_args(tracks, selected_tracks, config.CHANNEL_NAME)
        size_vf       = VIDEO_SIZES.get(video_size_key, VIDEO_SIZES["original"])["vf"]
        filters_chain = []
        if size_vf:
            filters_chain.append(size_vf)
        if watermark_pos and watermark_text:
            x, y      = WM_POSITIONS[watermark_pos]
            safe_text = watermark_text.replace("'", "\\'").replace(":", "\\:")
            filters_chain.append(
                f"drawtext=text='{safe_text}':"
                f"fontsize=28:fontcolor=white@0.85:"
                f"x={x}:y={y}:box=1:boxcolor=black@0.45:boxborderw=6"
            )

        if filters_chain:
            vf          = f'-vf "{",".join(filters_chain)}"'
            video_codec = "-c:v libx264 -preset slow -b:v 330k"
        else:
            vf          = ""
            video_codec = "-c:v copy"
        audio_codec = "-c:a aac -b:a 48k"

        _pulse_pos = [0]

        async def update_progress():
            while (
                user_id in user_tasks and
                job_id  in user_tasks.get(user_id, {}) and
                job_key not in cancelled_jobs
            ):
                elapsed  = time.time() - recording_start
                prog     = TimeFormatter(int(elapsed * 1000))
                if job_id in user_status.get(user_id, {}):
                    user_status[user_id][job_id]["progress"] = prog
                speed_mb = random.uniform(2.0, 8.0)
                try:
                    if is_download:
                        _pulse_pos[0] = (_pulse_pos[0] + 1) % 10
                        p   = _pulse_pos[0]
                        bar = (PROGRESS_EMPTY * p + PROGRESS_FILLED + PROGRESS_EMPTY * (9 - p))
                        await msg.edit_text(
                            f"{emoji} **Slot {n} — Downloading**\n"
                            f"📁 `{raw_filename}`\n"
                            f"{bar}\n"
                            f"⏱️ Elapsed: `{prog}`\n"
                            f"⚡ `{speed_mb:.1f} MB/s`\n\n🛑 /cancel to stop",
                            parse_mode=enums.ParseMode.HTML
                        )
                    else:
                        pct     = min((elapsed / duration) * 100, 100) if duration > 0 else 0
                        eta_sec = ((duration - elapsed) / (pct / 100)) if pct > 0 else 0
                        filled  = int(10 * pct // 100)
                        bar     = PROGRESS_FILLED * filled + PROGRESS_EMPTY * (10 - filled)
                        await msg.edit_text(
                            f"{emoji} **Slot {n} — Recording**\n"
                            f"📁 `{raw_filename}`\n"
                            f"{bar} `{pct:.1f}%`\n"
                            f"📊 `{prog}` / `{TimeFormatter(duration * 1000)}`\n"
                            f"⚡ `{speed_mb:.1f} MB/s`  ⏳ `{TimeFormatter(int(eta_sec * 1000))}`\n\n"
                            f"🛑 /cancel to stop",
                            parse_mode=enums.ParseMode.HTML
                        )
                except Exception:
                    pass
                await asyncio.sleep(5)

        prog_task = asyncio.create_task(update_progress())
        progress_tasks.setdefault(user_id, {})[job_id] = prog_task
        video_path_local = video_path

        if auto_mode:
            await msg.edit_text(f"{emoji} **Slot {n} — Auto Mode: Recording first 1 min...**")
            part1       = join(save_dir, "part1.mkv")
            part2       = join(save_dir, "part2.mkv")
            concat_list = join(save_dir, "concat.txt")

            cmd1 = (
                f'ffmpeg -y {http_opts(url)} -probesize 10000000 -analyzeduration 15000000 '
                f'-i "{url}" {video_map} {audio_maps} {vf} '
                f'{video_codec} {audio_codec} {meta_args} -movflags +faststart -t 00:01:00 "{part1}"'
            )
            proc1 = await asyncio.create_subprocess_exec(
                *shlex.split(cmd1), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            user_ffmpeg_pids.setdefault(user_id, {})[job_id] = proc1.pid
            await proc1.communicate()
            user_ffmpeg_pids.get(user_id, {}).pop(job_id, None)

            if job_key not in cancelled_jobs:
                seek_to = max(duration - 60, 61)
                await msg.edit_text(f"{emoji} **Slot {n} — Auto Mode: Recording last 1 min...**")
                cmd2 = (
                    f'ffmpeg -y {http_opts(url)} -probesize 10000000 -analyzeduration 15000000 '
                    f'-ss {seek_to} -i "{url}" {video_map} {audio_maps} {vf} '
                    f'{video_codec} {audio_codec} {meta_args} -movflags +faststart -t 00:01:00 "{part2}"'
                )
                proc2 = await asyncio.create_subprocess_exec(
                    *shlex.split(cmd2), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                user_ffmpeg_pids.setdefault(user_id, {})[job_id] = proc2.pid
                await proc2.communicate()
                user_ffmpeg_pids.get(user_id, {}).pop(job_id, None)

                await msg.edit_text(f"{emoji} **Slot {n} — Joining parts...**")
                with open(concat_list, "w") as f:
                    f.write(f"file '{part1}'\n")
                    if os.path.exists(part2) and os.path.getsize(part2) > 0:
                        f.write(f"file '{part2}'\n")
                rc, _, _ = await runcmd(
                    f'ffmpeg -y -f concat -safe 0 -i "{concat_list}" -c copy "{video_path}"'
                )
                video_path_local = video_path if (rc == 0 and os.path.exists(video_path)) else part1
        else:
            time_arg   = f"-t {timestamp}" if timestamp else ""
            ffmpeg_cmd = (
                f'ffmpeg -y {http_opts(url)} -probesize 10000000 -analyzeduration 15000000 '
                f'-i "{url}" {video_map} {audio_maps} {vf} '
                f'{video_codec} {audio_codec} {meta_args} -movflags +faststart {time_arg} "{video_path}"'
            )
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(ffmpeg_cmd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            user_ffmpeg_pids.setdefault(user_id, {})[job_id] = proc.pid
            LOG.info(f"FFmpeg PID {proc.pid} | user {user_id} | {job_id}")
            _, stderr_bytes = await proc.communicate()
            user_ffmpeg_pids.get(user_id, {}).pop(job_id, None)
            video_path_local = video_path

            was_cancelled = job_key in cancelled_jobs
            if proc.returncode != 0 and not was_cancelled:
                raise Exception(f"FFmpeg Error:\n{stderr_bytes.decode()[-2000:]}")

        if job_id in progress_tasks.get(user_id, {}):
            progress_tasks[user_id][job_id].cancel()
            del progress_tasks[user_id][job_id]

        was_cancelled = job_key in cancelled_jobs

        if not os.path.exists(video_path_local) or os.path.getsize(video_path_local) == 0:
            if was_cancelled:
                await msg.edit_text(f"{emoji} **Slot {n} — Cancelled. No video.**")
                return
            raise Exception("Video file missing or empty.")

        thumb_msg  = await ref_message.reply_text(f"{emoji} **Slot {n} — Generating thumbnail...**")
        dur        = await get_duration_ffmpeg(video_path_local) or (time_to_seconds(timestamp) if timestamp else 0)
        fixed_path = join(save_dir, f"fixed_{filename}")
        rc, _, _   = await runcmd(
            f'ffmpeg -y -i "{video_path_local}" -map 0 -c copy '
            f'-metadata creation_time="{time.strftime("%Y-%m-%dT%H:%M:%S")}" "{fixed_path}"'
        )
        if rc == 0:
            os.replace(fixed_path, video_path_local)

        rand_sec   = random.randint(5, max(dur - 5, 6))
        thumb_path = join(save_dir, "thumb.jpg")
        await runcmd(f'ffmpeg -y -ss {rand_sec} -i "{video_path_local}" -vframes 1 -q:v 2 "{thumb_path}"')
        await thumb_msg.delete()

        sel_labels = [t["label"] for t in tracks if t["index"] in selected_tracks] or ["All"]
        wm_desc    = "OFF" if not watermark_pos else f"{WM_LABEL.get(watermark_pos)} → {watermark_text}"
        size_label = VIDEO_SIZES.get(video_size_key, VIDEO_SIZES["original"])["label"]

        if is_download:
            status_line = "⚠️ _Partial download (cancelled)_" if was_cancelled else "✅ _Downloaded successfully!_"
            caption = (
                f"{emoji} **{raw_filename}**\n\n"
                f"⏱ **Duration:** `{TimeFormatter(dur * 1000)}`\n"
                f"🎵 **Audio:** `{', '.join(sel_labels)}`\n"
                f"🖼 **Watermark:** `{wm_desc}`\n"
                f"📁 **Format:** MKV\n\n{status_line}"
            )
        else:
            auto_desc   = "✅ First+Last 1min" if auto_mode else "❌"
            status_line = "⚠️ _Partial recording (cancelled)_" if was_cancelled else "✅ _Recorded successfully!_"
            caption = (
                f"{emoji} **{raw_filename}**\n\n"
                f"⏱ **Duration:** `{TimeFormatter(dur * 1000)}`\n"
                f"🎵 **Audio:** `{', '.join(sel_labels)}`\n"
                f"🖼 **Watermark:** `{wm_desc}`\n"
                f"📐 **Size:** `{size_label}`\n"
                f"⏩ **Auto:** `{auto_desc}`\n"
                f"📁 **Format:** MKV\n\n{status_line}"
            )

        size_mb    = round(os.path.getsize(video_path_local) / (1024 * 1024), 2) if os.path.exists(video_path_local) else 0
        uname      = ref_message.from_user.username or ref_message.from_user.first_name or str(user_id)
        _add_history({
            "type":       "download" if is_download else "rec",
            "status":     "cancelled" if was_cancelled else "done",
            "user_id":    user_id,
            "username":   uname,
            "filename":   raw_filename,
            "duration_s": int(dur),
            "size_mb":    size_mb,
            "url":        url[:120],
        })

        start_time = time.time()
        await ref_message.reply_video(
            video=video_path_local, caption=caption, duration=dur,
            thumb=thumb_path if os.path.exists(thumb_path) else None,
            progress=progress_for_pyrogram,
            progress_args=(ref_message, start_time, msg, save_dir, was_cancelled, job_id)
        )
        shutil.rmtree(save_dir, ignore_errors=True)

    except Exception as e:
        LOG.error(f"handle_record [{job_id}] error: {e}")
        uname = ref_message.from_user.username or ref_message.from_user.first_name or str(user_id)
        _add_history({
            "type":     "download" if setup.get("mode") == "download" else "rec",
            "status":   "cancelled" if job_key in cancelled_jobs else "failed",
            "user_id":  user_id,
            "username": uname,
            "filename": setup.get("filename", "?"),
            "duration_s": 0,
            "size_mb":  0,
            "url":      setup.get("url", "")[:120],
        })
        if job_key not in cancelled_jobs:
            try:
                await msg.edit(f"{emoji} **Slot {n} — Failed!**\n\n`{str(e)[:3000]}`")
            except Exception:
                pass
        shutil.rmtree(save_dir, ignore_errors=True)

    finally:
        user_tasks.get(user_id, {}).pop(job_id, None)
        user_status.get(user_id, {}).pop(job_id, None)
        user_ffmpeg_pids.get(user_id, {}).pop(job_id, None)
        progress_tasks.get(user_id, {}).pop(job_id, None)
        cancelled_jobs.discard(job_key)
        for d in [user_tasks, user_status, user_ffmpeg_pids, progress_tasks]:
            if user_id in d and not d[user_id]:
                del d[user_id]


# ─────────────────────────────────────────────────────────────────────────────
#  Playlist Manager  —  /playlistadd  /playlistdelete  /channel
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("playlistadd") & allowed)
async def playlistadd_cmd(client: Client, message: Message):
    """Usage: /playlistadd <url> [name]"""
    args = message.command[1:]
    if not args:
        return await message.reply_text(
            "❌ **Usage:** `/playlistadd <url> [name]`\n\n"
            "**Example:**\n"
            "`/playlistadd https://play.ksrtech.fun/playlist.php?token=KSR-xxx MyList`"
        )

    url  = args[0]
    name = " ".join(args[1:]).strip() if len(args) > 1 else f"Playlist{len(playlist_manager.get_playlists(message.from_user.id)) + 1}"

    msg = await message.reply_text("🔍 **Checking playlist URL...**")

    ok, err, channels = await playlist_manager.fetch_and_parse(url)
    if not ok:
        return await msg.edit_text(f"❌ **Invalid Playlist!**\n\n`{err}`")

    groups = playlist_manager.get_groups(channels)
    success, result_msg = playlist_manager.add_playlist(message.from_user.id, name, url)

    if success:
        playlist_manager.cache_set(message.from_user.id,
                                   len(playlist_manager.get_playlists(message.from_user.id)) - 1,
                                   channels)

    await msg.edit_text(
        f"{result_msg}\n\n"
        f"📺 **Channels:** `{len(channels)}`\n"
        f"📂 **Groups:** `{len(groups)}`\n"
        f"🔗 **URL:** `{url[:60]}{'...' if len(url) > 60 else ''}`\n\n"
        f"Use /channel to browse channels."
    )


@app.on_message(filters.command("playlistdelete") & allowed)
async def playlistdelete_cmd(client: Client, message: Message):
    """Usage: /playlistdelete <name>"""
    user_id = message.from_user.id
    playlists = playlist_manager.get_playlists(user_id)

    if not playlists:
        return await message.reply_text("📭 **No playlists saved.** Add one with /playlistadd")

    args = message.command[1:]
    if not args:
        names = "\n".join(f"  • `{p['name']}`" for p in playlists)
        return await message.reply_text(
            f"❌ **Usage:** `/playlistdelete <name>`\n\n"
            f"**Your playlists:**\n{names}"
        )

    name = " ".join(args).strip()
    success, result_msg = playlist_manager.delete_playlist(user_id, name)
    await message.reply_text(result_msg)


@app.on_message(filters.command("channel") & allowed)
async def channel_cmd(client: Client, message: Message):
    user_id   = message.from_user.id
    playlists = playlist_manager.get_playlists(user_id)

    if not playlists:
        return await message.reply_text(
            "📭 **No playlists saved yet!**\n\n"
            "Add one first:\n"
            "`/playlistadd <url> [name]`\n\n"
            "**Example:**\n"
            "`/playlistadd https://play.ksrtech.fun/playlist.php?token=KSR-xxx MyList`"
        )

    buttons = []
    for i, p in enumerate(playlists):
        buttons.append([InlineKeyboardButton(f"📋 {p['name']}", callback_data=f"plg_{i}")])

    await message.reply_text(
        "📺 **Select a Playlist:**",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


@app.on_callback_query(filters.regex(r"^plg_(\d+)$"))
async def cb_playlist_groups(client: Client, query):
    user_id  = query.from_user.id
    pl_idx   = int(query.matches[0].group(1))
    playlists = playlist_manager.get_playlists(user_id)

    if pl_idx >= len(playlists):
        return await query.answer("Playlist not found!", show_alert=True)

    pl = playlists[pl_idx]
    await query.answer()
    await query.message.edit_text(f"⏳ **Loading `{pl['name']}`...**")

    channels = playlist_manager.cache_get(user_id, pl_idx)
    if not channels:
        ok, err, channels = await playlist_manager.fetch_and_parse(pl["url"])
        if not ok:
            return await query.message.edit_text(f"❌ **Failed to load playlist:**\n`{err}`")
        playlist_manager.cache_set(user_id, pl_idx, channels)

    groups = playlist_manager.get_groups(channels)
    buttons = []
    row = []
    for gi, g in enumerate(groups):
        count = len(playlist_manager.channels_in_group(channels, g))
        row.append(InlineKeyboardButton(f"{g} ({count})", callback_data=f"pgg_{pl_idx}_{gi}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="pl_back")])

    await query.message.edit_text(
        f"📂 **{pl['name']}** — Select a group:\n"
        f"📺 Total `{len(channels)}` channels in `{len(groups)}` groups",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


@app.on_callback_query(filters.regex(r"^pgg_(\d+)_(\d+)$"))
async def cb_group_channels(client: Client, query):
    user_id  = query.from_user.id
    pl_idx   = int(query.matches[0].group(1))
    grp_idx  = int(query.matches[0].group(2))
    playlists = playlist_manager.get_playlists(user_id)

    if pl_idx >= len(playlists):
        return await query.answer("Playlist not found!", show_alert=True)

    channels = playlist_manager.cache_get(user_id, pl_idx)
    if not channels:
        ok, err, channels = await playlist_manager.fetch_and_parse(playlists[pl_idx]["url"])
        if not ok:
            return await query.answer("Failed to load playlist.", show_alert=True)
        playlist_manager.cache_set(user_id, pl_idx, channels)

    groups = playlist_manager.get_groups(channels)
    if grp_idx >= len(groups):
        return await query.answer("Group not found!", show_alert=True)

    group_name = groups[grp_idx]
    chs        = playlist_manager.channels_in_group(channels, group_name)

    await query.answer()

    # Show channels as pages of 20
    page_size = 20
    total_pages = (len(chs) - 1) // page_size + 1
    page = 0

    buttons = []
    for ci, ch in enumerate(chs[:page_size]):
        real_idx = ci
        buttons.append([InlineKeyboardButton(
            f"📡 {ch['name']}", callback_data=f"plc_{pl_idx}_{grp_idx}_{real_idx}"
        )])

    nav = []
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"▶ Next (1/{total_pages})", callback_data=f"pgp_{pl_idx}_{grp_idx}_1"))
    nav.append(InlineKeyboardButton("🔙 Back", callback_data=f"plg_{pl_idx}"))
    buttons.append(nav)

    await query.message.edit_text(
        f"📡 **{group_name}** — {len(chs)} channels\nTap a channel to record:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


@app.on_callback_query(filters.regex(r"^pgp_(\d+)_(\d+)_(\d+)$"))
async def cb_channels_page(client: Client, query):
    user_id  = query.from_user.id
    pl_idx   = int(query.matches[0].group(1))
    grp_idx  = int(query.matches[0].group(2))
    page     = int(query.matches[0].group(3))
    playlists = playlist_manager.get_playlists(user_id)

    channels = playlist_manager.cache_get(user_id, pl_idx)
    if not channels:
        ok, err, channels = await playlist_manager.fetch_and_parse(playlists[pl_idx]["url"])
        if not ok:
            return await query.answer("Failed to load playlist.", show_alert=True)
        playlist_manager.cache_set(user_id, pl_idx, channels)

    groups     = playlist_manager.get_groups(channels)
    group_name = groups[grp_idx]
    chs        = playlist_manager.channels_in_group(channels, group_name)
    page_size  = 20
    total_pages = (len(chs) - 1) // page_size + 1
    page       = max(0, min(page, total_pages - 1))
    start      = page * page_size

    await query.answer()
    buttons = []
    for ci, ch in enumerate(chs[start:start + page_size]):
        real_idx = start + ci
        buttons.append([InlineKeyboardButton(
            f"📡 {ch['name']}", callback_data=f"plc_{pl_idx}_{grp_idx}_{real_idx}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"pgp_{pl_idx}_{grp_idx}_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶ Next", callback_data=f"pgp_{pl_idx}_{grp_idx}_{page + 1}"))
    nav.append(InlineKeyboardButton("🔙 Back", callback_data=f"plg_{pl_idx}"))
    buttons.append(nav)

    await query.message.edit_text(
        f"📡 **{group_name}** — Page {page + 1}/{total_pages}:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


@app.on_callback_query(filters.regex(r"^plc_(\d+)_(\d+)_(\d+)$"))
async def cb_channel_selected(client: Client, query):
    user_id  = query.from_user.id
    pl_idx   = int(query.matches[0].group(1))
    grp_idx  = int(query.matches[0].group(2))
    ch_idx   = int(query.matches[0].group(3))
    playlists = playlist_manager.get_playlists(user_id)

    channels = playlist_manager.cache_get(user_id, pl_idx)
    if not channels:
        ok, err, channels = await playlist_manager.fetch_and_parse(playlists[pl_idx]["url"])
        if not ok:
            return await query.answer("Failed to load playlist.", show_alert=True)
        playlist_manager.cache_set(user_id, pl_idx, channels)

    groups     = playlist_manager.get_groups(channels)
    group_name = groups[grp_idx]
    chs        = playlist_manager.channels_in_group(channels, group_name)

    if ch_idx >= len(chs):
        return await query.answer("Channel not found!", show_alert=True)

    ch         = chs[ch_idx]
    stream_url = ch["url"]
    safe_name  = ch["name"].replace("`", "'")[:40] or config.DEFAULT_FILENAME
    timestamp  = "01:00:00"   # default recording duration for playlist channels

    await query.answer()

    # ── Slot availability check ────────────────────────────────────────────────
    if len(user_tasks.get(user_id, {})) >= MAX_CONCURRENT:
        return await query.message.reply_text(
            f"❌ **Maximum {MAX_CONCURRENT} simultaneous recordings reached!**\n"
            f"📊 /status  |  🛑 /cancel",
            reply_markup=build_main_keyboard()
        )

    # ── Show channel info + detecting stream ──────────────────────────────────
    await query.message.edit_text(
        f"📡 **{ch['name']}**\n"
        f"📂 Group: `{ch.get('group', 'General')}`\n\n"
        f"🔍 Stream detect ho rahi hai, please wait...",
        reply_markup=None
    )

    try:
        info = await detect_stream_info(stream_url)
    except Exception as e:
        LOG.error(f"playlist detect_stream_info error: {e}")
        return await query.message.edit_text(
            f"❌ **Stream detect failed!**\n\n`{e}`\n\n"
            "Channel URL check karein ya doosra channel try karein.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data=f"pgg_{pl_idx}_{grp_idx}")
            ]])
        )

    tracks   = info["tracks"]
    video    = info["video"]
    selected = set(t["index"] for t in tracks)

    # ── Initialize rec setup (same as /rec command) ────────────────────────────
    user_setup[user_id] = {
        "mode":           "record",
        "step":           "audio" if tracks else "watermark",
        "url":            stream_url,
        "timestamp":      timestamp,
        "filename":       safe_name,
        "tracks":         tracks,
        "selected_tracks": selected,
        "watermark_pos":  None,
        "watermark_text": config.DEFAULT_FILENAME,
        "auto_mode":      False,
        "video_size":     "original",
        "chat_id":        query.message.chat.id,
        "reply_to":       query.message.id,
        "video_info":     video,
    }

    quality_line = format_quality_line(video)
    audio_line   = ", ".join(t["label"] for t in tracks) if tracks else "Auto"

    if tracks:
        text = (
            f"✅ **Stream Ready!**\n\n"
            f"📡 **Channel:** `{ch['name']}`\n"
            f"📺 **Quality:** `{quality_line}`\n"
            f"🎵 **Audio:** `{audio_line}`\n"
            f"⏱ **Duration:** `{timestamp}`\n"
            f"📁 **File:** `{safe_name}`\n\n"
            f"👇 Select audio tracks to include:"
        )
        kb = build_audio_keyboard(tracks, selected)
    else:
        text = (
            f"✅ **Stream Ready!**\n\n"
            f"📡 **Channel:** `{ch['name']}`\n"
            f"📺 **Quality:** `{quality_line}`\n"
            f"🎵 **Audio:** No tracks — auto-select\n\n"
        ) + setup_summary_text(user_setup[user_id])
        kb = build_watermark_keyboard(user_setup[user_id])

    await query.message.reply_text(text, reply_markup=kb)


@app.on_callback_query(filters.regex(r"^pl_back$"))
async def cb_pl_back(client: Client, query):
    user_id   = query.from_user.id
    playlists = playlist_manager.get_playlists(user_id)
    await query.answer()

    if not playlists:
        return await query.message.edit_text("📭 No playlists saved.")

    buttons = [[InlineKeyboardButton(f"📋 {p['name']}", callback_data=f"plg_{i}")]
               for i, p in enumerate(playlists)]
    await query.message.edit_text(
        "📺 **Select a Playlist:**",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Cookies
# ─────────────────────────────────────────────────────────────────────────────

def cookies_dir() -> str:
    path = join(config.DOWNLOAD_DIRECTORY, "cookies")
    os.makedirs(path, exist_ok=True)
    return path

def cookies_path(user_id: int) -> str:
    return join(cookies_dir(), f"{user_id}_cookies.txt")

def has_cookies(user_id: int) -> bool:
    return os.path.exists(cookies_path(user_id))


@app.on_message(filters.command("cookies_add") & allowed)
async def cookies_add_cmd(client: Client, message: Message):
    user_setup.setdefault(message.from_user.id, {})["awaiting_cookies"] = True
    await message.reply_text(
        "🍪 **Add Cookies**\n\n"
        "📎 **Reply to this message with your `cookies.txt` file.**\n\n"
        "📝 How to get cookies:\n"
        "• Install **EditThisCookie** or **Get cookies.txt** extension\n"
        "• Login to OTT platform\n"
        "• Export cookies as `cookies.txt` (Netscape format)\n\n"
        "⚠️ _Cookies are stored privately per user._",
        reply_markup=build_main_keyboard()
    )


@app.on_message(filters.document & allowed)
async def document_handler(client: Client, message: Message):
    user_id = message.from_user.id
    setup   = user_setup.get(user_id, {})
    if not setup.get("awaiting_cookies"):
        return
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".txt"):
        return await message.reply_text("❌ Please send a `.txt` file (cookies.txt).")
    msg = await message.reply_text("⏳ **Saving cookies...**")
    try:
        dest = cookies_path(user_id)
        await client.download_media(message, file_name=dest)
        setup.pop("awaiting_cookies", None)
        size_kb = os.path.getsize(dest) / 1024
        await msg.edit_text(
            f"✅ **Cookies saved!**\n\n"
            f"📦 **Size:** `{size_kb:.1f} KB`\n\n"
            f"Now use /ott_download with OTT URLs — cookies will be applied automatically. 🍪"
        )
    except Exception as e:
        LOG.error(f"cookies_add error: {e}")
        await msg.edit_text(f"❌ **Failed to save cookies:** `{e}`")


@app.on_message(filters.command("cookies_status") & allowed)
async def cookies_status_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    path    = cookies_path(user_id)
    if not os.path.exists(path):
        return await message.reply_text(
            "❌ **No cookies found!**\n\nUse /cookies_add to upload.",
            reply_markup=build_main_keyboard()
        )
    size_kb  = os.path.getsize(path) / 1024
    created  = datetime.fromtimestamp(os.path.getctime(path), tz=tz).strftime("%d-%m-%Y %I:%M:%S %p")
    modified = datetime.fromtimestamp(os.path.getmtime(path), tz=tz).strftime("%d-%m-%Y %I:%M:%S %p")
    with open(path, "r", errors="ignore") as f:
        lines = [l for l in f.readlines() if l.strip() and not l.startswith("#")]
    await message.reply_text(
        f"🍪 **Cookies Status**\n\n"
        f"✅ **Status:** Active\n"
        f"📦 **Size:** `{size_kb:.1f} KB`\n"
        f"🔢 **Entries:** `{len(lines)}`\n"
        f"🕒 **Uploaded:** `{created}`\n"
        f"🔄 **Modified:** `{modified}`\n\n"
        f"🗑 Use /del_cookies to remove",
        reply_markup=build_main_keyboard()
    )


@app.on_message(filters.command("del_cookies") & allowed)
async def del_cookies_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    path    = cookies_path(user_id)
    if not os.path.exists(path):
        return await message.reply_text("❌ **No cookies to delete!**", reply_markup=build_main_keyboard())
    os.remove(path)
    await message.reply_text(
        "🗑 **Cookies deleted successfully!**\n\nUse /cookies_add to upload new ones.",
        reply_markup=build_main_keyboard()
    )


# ─────────────────────────────────────────────────────────────────────────────
#  OTT / YouTube download (yt-dlp)
# ─────────────────────────────────────────────────────────────────────────────

async def ytdlp_download(
    url: str, output_path: str,
    cookies_file: Optional[str] = None,
    fmt: Optional[str] = None,
    audio_lang: Optional[str] = None,
) -> Tuple[int, str, str]:
    cmd_parts = [
        "yt-dlp", "--no-playlist", "--merge-output-format", "mkv",
        "-o", output_path,
    ]

    # yt-dlp has no --audio-language flag; language is selected via format string.
    if audio_lang:
        # Inject language preference into the audio portion of the format string
        # e.g. "bestvideo[height<=720]+bestaudio" → "bestvideo[height<=720]+bestaudio[language=hin]/bestvideo[height<=720]+bestaudio"
        base_fmt = fmt or "bestvideo+bestaudio/best"
        if "bestaudio" in base_fmt:
            lang_fmt = base_fmt.replace("bestaudio", f"bestaudio[language={audio_lang}]", 1)
            effective_fmt = f"{lang_fmt}/{base_fmt}"
        else:
            effective_fmt = f"bestvideo+bestaudio[language={audio_lang}]/bestvideo+bestaudio/best"
        cmd_parts += ["-f", effective_fmt]
    elif fmt:
        # No specific language — keep all audio tracks (multi)
        cmd_parts += ["-f", fmt, "--audio-multistreams"]
    else:
        cmd_parts += ["--audio-multistreams"]

    if cookies_file and os.path.exists(cookies_file):
        cmd_parts += ["--cookies", cookies_file]
    cmd_parts.append(url)
    process = await asyncio.create_subprocess_exec(
        *cmd_parts, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode(), stderr.decode()


async def detect_ott_formats(url: str, cookies_file: Optional[str] = None) -> dict:
    """Detect available resolutions and audio languages from a URL using yt-dlp -J."""
    cmd = ["yt-dlp", "--no-playlist", "-J"]
    if cookies_file and os.path.exists(cookies_file):
        cmd += ["--cookies", cookies_file]
    cmd.append(url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return {"title": "", "heights": [], "langs": [], "duration": 0}
        data   = json.loads(stdout.decode())
        title  = data.get("title", "")
        dur    = int(data.get("duration", 0) or 0)
        heights: set = set()
        langs:   set = set()
        for f in data.get("formats", []):
            h = f.get("height")
            if h and f.get("vcodec", "none") not in ("none", None, ""):
                heights.add(int(h))
            lang = (f.get("language") or "").lower()[:3]
            if lang and f.get("acodec", "none") not in ("none", None, ""):
                langs.add(lang)
        return {
            "title":    title,
            "heights":  sorted(heights),
            "langs":    sorted(langs),
            "duration": dur,
        }
    except Exception as e:
        LOG.warning(f"detect_ott_formats error: {e}")
        return {"title": "", "heights": [], "langs": [], "duration": 0}


@app.on_message(filters.command("ott_download") & allowed)
async def ott_download_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "❌ **Invalid Format!**\n\n"
            "📌 **Usage:**\n"
            "```\n/ott_download https://youtube.com/... MyFilename\n```\n\n"
            "🍪 Add cookies first with /cookies_add for OTT sites.",
            reply_markup=build_main_keyboard()
        )
    user_id = message.from_user.id
    if len(user_tasks.get(user_id, {})) >= MAX_CONCURRENT:
        return await message.reply_text(
            f"❌ **All {MAX_CONCURRENT} slots are busy!**\n📊 /status  |  🛑 /cancel",
            reply_markup=build_main_keyboard()
        )

    params       = " ".join(message.command[1:])
    parts        = params.split(" ", 1)
    url          = parts[0]
    raw_filename = parts[1].strip() if len(parts) > 1 else config.DEFAULT_FILENAME

    detect_msg = await message.reply_text(
        "🔍 **Detecting available qualities...**\n"
        "⏳ _Please wait a few seconds..._"
    )

    cookie_file = cookies_path(user_id) if has_cookies(user_id) else None
    info        = await detect_ott_formats(url, cookie_file)

    # Build resolution map from detected heights
    res_map: dict = {}
    for h in info["heights"]:
        lbl = _HEIGHT_LABEL.get(h, f"📺 {h}p")
        res_map[lbl] = _HEIGHT_FMT.get(h, f"bestvideo[height<={h}]+bestaudio/best[height<={h}]")
    res_map["🏆 Best"] = "bestvideo+bestaudio/best"

    # Build audio map from detected language codes
    audio_map: dict = {}
    for lang in info["langs"]:
        lbl = _LANG_CODE_TO_LABEL.get(lang, lang.upper())
        if lbl not in audio_map:
            audio_map[lbl] = lang
    audio_map["🌐 Multi"] = None

    # Fallback to static lists if detection failed
    if len(res_map) <= 1:
        res_map   = dict(OTT_RES_LABEL_TO_FMT)
    if not audio_map or list(audio_map.keys()) == ["🌐 Multi"]:
        audio_map = dict(OTT_AUDIO_LANGS)

    user_setup[user_id] = {
        "step": "ott_resolution",
        "url": url,
        "filename": raw_filename,
        "chat_id": message.chat.id,
        "reply_to": message.id,
        "ott_res_label": "",
        "ott_audio_label": "",
        "detected_res_map":   res_map,
        "detected_audio_map": audio_map,
        "detected_title":    info.get("title", ""),
        "detected_duration": info.get("duration", 0),
    }

    title_line = f"📌 **Title:** `{info['title'][:55]}`\n" if info.get("title") else ""
    dur_line   = f"⏱ **Duration:** `{TimeFormatter(info['duration'] * 1000)}`\n" if info.get("duration") else ""
    res_count  = len(res_map) - 1  # exclude 🏆 Best
    audio_count = len([v for v in audio_map.values() if v is not None])

    try:
        await detect_msg.delete()
    except Exception:
        pass

    await message.reply_text(
        f"🌐 **OTT / YouTube Download**\n\n"
        f"{title_line}{dur_line}"
        f"📁 **File:** `{raw_filename}`\n"
        f"🍪 **Cookies:** `{'✅ Found' if has_cookies(user_id) else '❌ None'}`\n\n"
        f"📺 **{res_count} resolutions detected** · 🎧 **{audio_count} audio tracks**\n\n"
        f"👇 Select resolution:",
        reply_markup=build_ott_resolution_keyboard_dynamic(res_map)
    )


async def ott_download_task(client: Client, ref_message: Message, setup: dict, user_id: int):
    job_id = next_job_id(user_id)
    if not job_id:
        await ref_message.reply_text(f"❌ All {MAX_CONCURRENT} slots full!")
        return

    job_key      = make_job_key(user_id, job_id)
    n            = slot_number(job_id)
    emoji        = SLOT_EMOJI[n - 1]
    raw_filename = setup["filename"]
    url          = setup["url"]
    fmt          = setup.get("ott_format")
    audio_lang   = setup.get("ott_audio_lang")
    res_label    = setup.get("ott_res_label", "Best")
    audio_label  = setup.get("ott_audio_label", "Multi")

    save_dir    = join(config.DOWNLOAD_DIRECTORY, f"{int(time.time())}_{job_id}")
    os.makedirs(save_dir, exist_ok=True)
    output_tmpl = join(save_dir, f"{raw_filename}.%(ext)s")

    msg = await ref_message.reply_text(
        f"{emoji} **Slot {n} — Starting OTT Download...**\n"
        f"📁 `{raw_filename}`\n"
        f"📺 `{res_label}`  🎧 `{audio_label}`\n"
        f"🍪 Cookies: `{'✅ Found' if has_cookies(user_id) else '❌ None'}`",
        reply_markup=build_main_keyboard()
    )

    user_tasks.setdefault(user_id, {})[job_id] = time.time()
    user_status.setdefault(user_id, {})[job_id] = {
        "id": int(time.time()), "filename": raw_filename,
        "target": "∞", "progress": "00:00:00",
        "save_dir": save_dir, "mode": "ott",
    }
    dl_start = time.time()

    _ott_pulse = [0]

    async def ott_progress():
        while user_id in user_tasks and job_id in user_tasks.get(user_id, {}) and job_key not in cancelled_jobs:
            elapsed = time.time() - dl_start
            prog    = TimeFormatter(int(elapsed * 1000))
            if job_id in user_status.get(user_id, {}):
                user_status[user_id][job_id]["progress"] = prog
            _ott_pulse[0] = (_ott_pulse[0] + 1) % 10
            p   = _ott_pulse[0]
            bar = PROGRESS_EMPTY * p + PROGRESS_FILLED + PROGRESS_EMPTY * (9 - p)
            try:
                await msg.edit_text(
                    f"{emoji} <b>Slot {n} — Downloading (OTT/YT)</b>\n"
                    f"📁 <code>{raw_filename}</code>\n"
                    f"📺 <code>{res_label}</code>  🎧 <code>{audio_label}</code>\n"
                    f"{bar}\n"
                    f"⏱️ Elapsed: <code>{prog}</code>\n\n🛑 /cancel to stop",
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass
            await asyncio.sleep(5)

    prog_task = asyncio.create_task(ott_progress())
    progress_tasks.setdefault(user_id, {})[job_id] = prog_task

    try:
        cookie_file = cookies_path(user_id) if has_cookies(user_id) else None
        retcode, out, err = await ytdlp_download(url, output_tmpl, cookie_file, fmt, audio_lang)
        if job_id in progress_tasks.get(user_id, {}):
            progress_tasks[user_id][job_id].cancel()
        was_cancelled = job_key in cancelled_jobs
        if retcode != 0 and not was_cancelled:
            raise Exception(f"yt-dlp error:\n{err[-2000:]}")

        video_path = None
        for f in os.listdir(save_dir):
            if f.startswith(raw_filename):
                video_path = join(save_dir, f)
                break
        if not video_path or not os.path.exists(video_path):
            raise Exception("Downloaded file not found.")

        thumb_msg  = await ref_message.reply_text(f"{emoji} **Slot {n} — Generating thumbnail...**")
        dur        = await get_duration_ffmpeg(video_path)
        rand_sec   = random.randint(5, max(dur - 5, 6)) if dur > 10 else 1
        thumb_path = join(save_dir, "thumb.jpg")
        await runcmd(f'ffmpeg -y -ss {rand_sec} -i "{video_path}" -vframes 1 -q:v 2 "{thumb_path}"')
        await thumb_msg.delete()

        cookie_file = cookies_path(user_id) if has_cookies(user_id) else None
        caption = (
            f"{emoji} **{raw_filename}**\n\n"
            f"⏱ **Duration:** `{TimeFormatter(dur * 1000)}`\n"
            f"📺 **Resolution:** `{res_label}`\n"
            f"🎧 **Audio:** `{audio_label}`\n"
            f"📥 **Source:** OTT/YouTube\n"
            f"🍪 **Cookies:** `{'✅ Used' if cookie_file else '❌ None'}`\n"
            f"📁 **Format:** MKV\n\n"
            f"{'⚠️ _Partial (cancelled)_' if was_cancelled else '✅ _Downloaded successfully!_'}"
        )
        size_mb    = round(os.path.getsize(video_path) / (1024 * 1024), 2) if os.path.exists(video_path) else 0
        uname      = ref_message.from_user.username or ref_message.from_user.first_name or str(user_id)
        _add_history({
            "type":       "ott",
            "status":     "cancelled" if was_cancelled else "done",
            "user_id":    user_id,
            "username":   uname,
            "filename":   raw_filename,
            "duration_s": int(dur),
            "size_mb":    size_mb,
            "url":        url[:120],
            "res_label":  res_label,
            "audio_label": audio_label,
        })

        start_time = time.time()
        await ref_message.reply_video(
            video=video_path, caption=caption, duration=dur,
            thumb=thumb_path if os.path.exists(thumb_path) else None,
            progress=progress_for_pyrogram,
            progress_args=(ref_message, start_time, msg, save_dir, was_cancelled, job_id)
        )
        shutil.rmtree(save_dir, ignore_errors=True)

    except Exception as e:
        LOG.error(f"ott_download error [{job_id}]: {e}")
        uname = ref_message.from_user.username or ref_message.from_user.first_name or str(user_id)
        _add_history({
            "type":       "ott",
            "status":     "cancelled" if job_key in cancelled_jobs else "failed",
            "user_id":    user_id,
            "username":   uname,
            "filename":   setup.get("filename", "?"),
            "duration_s": 0,
            "size_mb":    0,
            "url":        setup.get("url", "")[:120],
            "res_label":  setup.get("ott_res_label", ""),
            "audio_label": setup.get("ott_audio_label", ""),
        })
        if job_key not in cancelled_jobs:
            try:
                await msg.edit(f"{emoji} **Slot {n} — Download Failed!**\n\n`{str(e)[:3000]}`")
            except Exception:
                pass
        shutil.rmtree(save_dir, ignore_errors=True)
    finally:
        user_tasks.get(user_id, {}).pop(job_id, None)
        user_status.get(user_id, {}).pop(job_id, None)
        user_ffmpeg_pids.get(user_id, {}).pop(job_id, None)
        progress_tasks.get(user_id, {}).pop(job_id, None)
        cancelled_jobs.discard(job_key)
        for d in [user_tasks, user_status, user_ffmpeg_pids, progress_tasks]:
            if user_id in d and not d[user_id]:
                del d[user_id]


# ─────────────────────────────────────────────────────────────────────────────
#  /compress
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("compress") & allowed)
async def compress_cmd(client: Client, message: Message):
    if not message.reply_to_message or not get_video_media(message.reply_to_message):
        return await message.reply_text(
            "❌ **Reply to a video message with /compress**",
            reply_markup=build_main_keyboard()
        )
    user_id = message.from_user.id
    compress_pending[user_id] = message.reply_to_message.id
    user_setup[user_id] = {"step": "compress"}
    await message.reply_text(
        "🗜 **Video Compress**\n\nSelect compression quality:",
        reply_markup=build_compress_keyboard()
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /screenshot
# ─────────────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("screenshot") & allowed)
async def screenshot_cmd(client: Client, message: Message):
    if not message.reply_to_message or not get_video_media(message.reply_to_message):
        return await message.reply_text(
            "❌ **Reply to a video with /screenshot [count]**\n\n"
            "Example: `/screenshot 10` → 10 screenshots (max 30)",
            reply_markup=build_main_keyboard()
        )
    try:
        count = int(message.command[1]) if len(message.command) > 1 else 1
        count = max(1, min(count, 30))
    except (ValueError, IndexError):
        count = 1

    user_id       = message.from_user.id
    video_message = message.reply_to_message
    msg = await message.reply_text(
        f"📸 **Extracting {count} screenshot{'s' if count > 1 else ''}...**",
        reply_markup=build_main_keyboard()
    )
    save_dir = join(config.DOWNLOAD_DIRECTORY, f"{int(time.time())}_ss_{user_id}")
    os.makedirs(save_dir, exist_ok=True)

    try:
        await msg.edit_text("📥 **Downloading video...**")
        orig_path = join(save_dir, "video.mkv")
        await client.download_media(video_message, file_name=orig_path)

        if not os.path.exists(orig_path) or os.path.getsize(orig_path) == 0:
            raise Exception("Video download failed or file is empty.")

        dur = await get_duration_ffmpeg(orig_path)

        await msg.edit_text(f"📸 **Extracting {count} screenshot{'s' if count > 1 else ''}...**")

        if dur <= 0:
            # ffprobe couldn't read duration — try a single frame at position 0
            timestamps = [0]
            count = 1
        elif dur == 1:
            timestamps = [0]
            count = 1
        elif count == 1:
            timestamps = [max(dur // 2, 0)]
        else:
            usable_dur = max(dur - 2, 1)
            count      = min(count, usable_dur)
            step       = usable_dur / max(count - 1, 1)
            timestamps = [min(int(i * step), dur - 1) for i in range(count)]

        screenshot_paths = []
        for i, ts in enumerate(timestamps):
            ss_path = join(save_dir, f"ss_{i + 1:02d}.jpg")
            rc, _, _ = await runcmd(
                f'ffmpeg -y -ss {ts} -i "{orig_path}" -vframes 1 -q:v 2 "{ss_path}"'
            )
            if rc == 0 and os.path.exists(ss_path) and os.path.getsize(ss_path) > 0:
                screenshot_paths.append(ss_path)

        if not screenshot_paths:
            raise Exception("No screenshots could be extracted.")

        await msg.edit_text(f"📤 **Uploading {len(screenshot_paths)} screenshot{'s' if len(screenshot_paths) > 1 else ''}...**")

        from pyrogram.types import InputMediaPhoto
        caption_main = (
            f"📸 **{len(screenshot_paths)} Screenshot{'s' if len(screenshot_paths) > 1 else ''}**\n"
            f"⏱ **Video Duration:** `{TimeFormatter(dur * 1000)}`"
        )
        for batch_start in range(0, len(screenshot_paths), 10):
            batch       = screenshot_paths[batch_start: batch_start + 10]
            media_group = [
                InputMediaPhoto(sp, caption=caption_main if (batch_start == 0 and idx == 0) else "")
                for idx, sp in enumerate(batch)
            ]
            await message.reply_media_group(media_group)

        await msg.edit_text(f"✅ **{len(screenshot_paths)} screenshot{'s' if len(screenshot_paths) > 1 else ''} sent!**")
        shutil.rmtree(save_dir, ignore_errors=True)

    except Exception as e:
        LOG.error(f"screenshot error: {e}")
        try:
            await msg.edit_text(f"❌ **Screenshot failed!**\n\n`{str(e)[:2000]}`")
        except Exception:
            pass
        shutil.rmtree(save_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def _daily_refresh_loop():
    while True:
        await asyncio.sleep(12 * 3600)
        limit_system.daily_refresh_all()
        LOG.info("✅ 12-hour rec limit refresh complete.")


async def send_startup_message(client: Client):
    import platform
    import psutil
    from datetime import datetime
    import pytz as _pytz
    _tz = _pytz.timezone(config.TIMEZONE)
    now_str = datetime.now(_tz).strftime("%d %b %Y  %I:%M:%S %p")
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
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
        f"  🖥 CPU Usage   : `{cpu}%`\n"
        f"  🧠 RAM Used    : `{ram_used}` / `{ram_total}`\n"
        f"  💾 Disk Free   : `{disk_free}`\n"
        f"  🐍 Python      : `{platform.python_version()}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  👥 Auth Users  : `{len(config.AUTH_USERS)}`\n"
        f"  👤 Total Users : `{users_count}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ All systems running normally."
    )
    for owner_id in config.OWNER_ID:
        try:
            await client.send_message(owner_id, text)
        except Exception as e:
            LOG.warning(f"Startup msg fail (owner {owner_id}): {e}")


if __name__ == "__main__":
    print("🎬 Starting Video Recorder Bot...")
    print(f"⚡ Max concurrent recordings per user: {MAX_CONCURRENT}")
    print("✅ Bot is now running!")
    app.start()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(send_startup_message(app))
    loop.create_task(_daily_refresh_loop())
    print("⏰ 12-hour auto-refresh scheduled.")
    print("🤖 OTT Recorder Bot is Live with Auto-Crop, Compress & Custom SS!")
    idle()
