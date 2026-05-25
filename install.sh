#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  OTT Recorder Bot — Server Install Script
#  Usage:  bash install.sh
#  Tested: Ubuntu 20.04 / 22.04 / Debian 11+
# ═══════════════════════════════════════════════════════════════════
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${GREEN}[✔]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✘]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[→]${NC} $1"; }

echo -e "\n${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}${CYAN}   OTT Recorder Bot — Auto Installer${NC}"
echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

# ── Check root ────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  err "Please run as root:  sudo bash install.sh"
fi

BOT_DIR="/opt/ottbot"
BOT_USER="ottbot"
SERVICE_NAME="ottbot"
PYTHON_MIN="3.11"

# ── 1. System packages ────────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq

info "Installing system dependencies..."
apt-get install -y -qq \
  python3 python3-pip python3-venv \
  ffmpeg \
  git curl wget \
  screen \
  > /dev/null 2>&1
log "System packages installed"

# ── 2. Python version check ───────────────────────────────────────
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python version: $PY_VER"
if python3 -c "import sys; exit(0 if sys.version_info >= (3,11) else 1)"; then
  log "Python $PY_VER is compatible"
else
  err "Python $PYTHON_MIN+ required. Detected: $PY_VER"
fi

# ── 3. FFmpeg check ───────────────────────────────────────────────
if command -v ffmpeg &>/dev/null; then
  FFMPEG_VER=$(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')
  log "FFmpeg installed: $FFMPEG_VER"
else
  err "FFmpeg installation failed. Install manually: apt-get install ffmpeg"
fi

# ── 4. Create bot user & directory ───────────────────────────────
info "Setting up bot directory: $BOT_DIR"
mkdir -p "$BOT_DIR/downloads" "$BOT_DIR/logs"

if ! id "$BOT_USER" &>/dev/null; then
  useradd -r -s /bin/bash -d "$BOT_DIR" "$BOT_USER"
  log "Created user: $BOT_USER"
else
  warn "User $BOT_USER already exists — skipping"
fi

# ── 5. Copy bot files ─────────────────────────────────────────────
info "Copying bot files to $BOT_DIR ..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FILES=(main.py config.py verify.py limit_system.py playlist_manager.py)
for f in "${FILES[@]}"; do
  if [[ -f "$SCRIPT_DIR/$f" ]]; then
    cp "$SCRIPT_DIR/$f" "$BOT_DIR/"
    log "Copied $f"
  else
    warn "$f not found in $SCRIPT_DIR — skipping"
  fi
done

# ── 6. Python virtual environment ────────────────────────────────
info "Creating Python virtual environment..."
python3 -m venv "$BOT_DIR/venv"
"$BOT_DIR/venv/bin/pip" install --upgrade pip -q
log "Virtual environment created: $BOT_DIR/venv"

# ── 7. Install Python packages ────────────────────────────────────
info "Installing Python packages (this may take 1-2 minutes)..."
"$BOT_DIR/venv/bin/pip" install -q \
  "pyrogram>=2.0.106" \
  "tgcrypto>=1.2.5" \
  "aiohttp>=3.13.5" \
  "requests>=2.34.2" \
  "psutil>=7.2.2" \
  "pytz>=2026.2" \
  "yt-dlp>=2026.3.17"
log "Python packages installed"

# ── 8. Create .env file (if not exists) ──────────────────────────
ENV_FILE="$BOT_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  info "Creating .env template..."
  cat > "$ENV_FILE" << 'EOF'
# ── Telegram Bot Credentials ────────────────────────────
# Get API_ID and API_HASH from: https://my.telegram.org
API_ID=YOUR_API_ID_HERE
API_HASH=YOUR_API_HASH_HERE

# Get BOT_TOKEN from @BotFather on Telegram
BOT_TOKEN=YOUR_BOT_TOKEN_HERE

# ── Access Control ───────────────────────────────────────
# Your Telegram user ID (find via @userinfobot)
OWNER_IDS=YOUR_OWNER_ID_HERE

# Additional user IDs who can use the bot (space-separated)
AUTH_USERS=

# ── Bot Settings ─────────────────────────────────────────
DOWNLOAD_DIRECTORY=/opt/ottbot/downloads
DEFAULT_FILENAME=REC
TIMEZONE=Asia/Kolkata
CHANNEL_NAME=@YourChannelName
DEFAULT_METADATA=
EOF
  log ".env template created at $ENV_FILE"
  warn "⚠  IMPORTANT: Edit $ENV_FILE and fill in your credentials before starting!"
else
  warn ".env already exists — skipping template creation"
fi
chmod 600 "$ENV_FILE"

# ── 9. Create start script ────────────────────────────────────────
cat > "$BOT_DIR/start.sh" << 'STARTEOF'
#!/usr/bin/env bash
cd /opt/ottbot
source venv/bin/activate
exec python3 main.py
STARTEOF
chmod +x "$BOT_DIR/start.sh"
log "Start script created: $BOT_DIR/start.sh"

# ── 10. Create systemd service ────────────────────────────────────
info "Creating systemd service: $SERVICE_NAME"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=OTT Recorder Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$BOT_USER
WorkingDirectory=$BOT_DIR
EnvironmentFile=$BOT_DIR/.env
ExecStart=$BOT_DIR/venv/bin/python3 $BOT_DIR/main.py
Restart=on-failure
RestartSec=10
StandardOutput=append:$BOT_DIR/logs/bot.log
StandardError=append:$BOT_DIR/logs/bot.log

[Install]
WantedBy=multi-user.target
EOF
log "Systemd service created"

# ── 11. Fix ownership ─────────────────────────────────────────────
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"
log "Permissions set for $BOT_USER"

# ── 12. Reload systemd ────────────────────────────────────────────
systemctl daemon-reload
log "Systemd reloaded"

# ── Done ──────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}${GREEN}   Installation Complete!${NC}"
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${BOLD}Bot directory:${NC}  $BOT_DIR"
echo -e "  ${BOLD}Config file:${NC}    $ENV_FILE"
echo -e "  ${BOLD}Logs:${NC}           $BOT_DIR/logs/bot.log"
echo -e "  ${BOLD}Downloads:${NC}      $BOT_DIR/downloads/"
echo ""
echo -e "${YELLOW}  Next steps:${NC}"
echo -e "  1. Edit your credentials:"
echo -e "     ${CYAN}nano $ENV_FILE${NC}"
echo ""
echo -e "  2. Enable & start the bot:"
echo -e "     ${CYAN}systemctl enable $SERVICE_NAME${NC}"
echo -e "     ${CYAN}systemctl start  $SERVICE_NAME${NC}"
echo ""
echo -e "  3. Check bot status:"
echo -e "     ${CYAN}systemctl status $SERVICE_NAME${NC}"
echo ""
echo -e "  4. View live logs:"
echo -e "     ${CYAN}tail -f $BOT_DIR/logs/bot.log${NC}"
echo ""
echo -e "  ${RED}⚠  Do NOT start until you fill in $ENV_FILE${NC}"
echo ""
