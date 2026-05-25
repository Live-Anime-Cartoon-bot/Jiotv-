import os
import json
import re
import aiohttp
import asyncio
from typing import Dict, List, Optional, Tuple

PLAYLIST_FILE = os.path.join(os.path.dirname(__file__), "user_playlists.json")

# In-memory cache: {user_id: {"pl_idx": [...channels...], ...}}
_playlist_cache: Dict[int, Dict[int, List[dict]]] = {}


# ── Storage helpers ────────────────────────────────────────────────────────────

def _load() -> dict:
    if os.path.exists(PLAYLIST_FILE):
        try:
            with open(PLAYLIST_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save(data: dict):
    with open(PLAYLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Public API ─────────────────────────────────────────────────────────────────

def get_playlists(user_id: int) -> List[dict]:
    """Return list of {name, url} dicts saved by this user."""
    data = _load()
    return data.get(str(user_id), [])


def add_playlist(user_id: int, name: str, url: str) -> Tuple[bool, str]:
    """Save a playlist. Returns (success, message)."""
    data = _load()
    key  = str(user_id)
    playlists = data.get(key, [])

    if len(playlists) >= 10:
        return False, "Maximum 10 playlists allowed per user."

    for p in playlists:
        if p["name"].lower() == name.lower():
            return False, f"Playlist **{name}** already exists. Use a different name."

    playlists.append({"name": name, "url": url})
    data[key] = playlists
    _save(data)
    return True, f"✅ Playlist **{name}** saved!"


def delete_playlist(user_id: int, name: str) -> Tuple[bool, str]:
    """Delete a playlist by name. Returns (success, message)."""
    data = _load()
    key  = str(user_id)
    playlists = data.get(key, [])

    new_list = [p for p in playlists if p["name"].lower() != name.lower()]
    if len(new_list) == len(playlists):
        return False, f"No playlist named **{name}** found."

    data[key] = new_list
    _save(data)
    # Clear cache for this user
    _playlist_cache.pop(user_id, None)
    return True, f"🗑 Playlist **{name}** deleted."


# ── M3U8 Fetch & Parse ─────────────────────────────────────────────────────────

async def fetch_and_parse(url: str) -> Tuple[bool, str, List[dict]]:
    """
    Fetch an M3U8/m3u playlist URL.
    Returns (success, error_msg, channels)
    Each channel: {name, url, group, logo}
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15),
                                   allow_redirects=True) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status} from playlist URL.", []
                text = await resp.text(errors="replace")
    except asyncio.TimeoutError:
        return False, "Timeout fetching playlist URL.", []
    except Exception as e:
        return False, f"Network error: {e}", []

    channels = _parse_m3u(text)
    if not channels:
        return False, "No channels found. Make sure the URL returns a valid M3U8 playlist.", []

    return True, "", channels


def _parse_m3u(text: str) -> List[dict]:
    """Parse #EXTM3U text into list of channel dicts."""
    channels = []
    lines = text.splitlines()
    i = 0
    current_info = None

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("#EXTINF"):
            logo  = ""
            group = "General"
            name  = ""

            logo_match  = re.search(r'tvg-logo="([^"]*)"', line)
            group_match = re.search(r'group-title="([^"]*)"', line)
            name_match  = re.search(r',(.+)$', line)

            if logo_match:
                logo  = logo_match.group(1).strip()
            if group_match:
                group = group_match.group(1).strip() or "General"
            if name_match:
                name  = name_match.group(1).strip()

            current_info = {"name": name, "group": group, "logo": logo}

        elif line and not line.startswith("#") and current_info:
            current_info["url"] = line
            channels.append(current_info)
            current_info = None

        i += 1

    return channels


def get_groups(channels: List[dict]) -> List[str]:
    """Return unique group names preserving order."""
    seen = {}
    for ch in channels:
        g = ch.get("group", "General")
        seen[g] = True
    return list(seen.keys())


def channels_in_group(channels: List[dict], group: str) -> List[dict]:
    return [c for c in channels if c.get("group", "General") == group]


# ── Cache helpers ──────────────────────────────────────────────────────────────

def cache_set(user_id: int, pl_idx: int, channels: List[dict]):
    if user_id not in _playlist_cache:
        _playlist_cache[user_id] = {}
    _playlist_cache[user_id][pl_idx] = channels


def cache_get(user_id: int, pl_idx: int) -> Optional[List[dict]]:
    return _playlist_cache.get(user_id, {}).get(pl_idx)
