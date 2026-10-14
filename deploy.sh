#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/news-bot-v2}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP="${APP_DIR}.backup-$(date +%Y%m%d-%H%M%S)"
if [ -d "$APP_DIR" ]; then cp -a "$APP_DIR" "$BACKUP"; fi
mkdir -p "$APP_DIR"
# IMPORTANT: keep current .env and runtime sessions untouched.
rsync -a --delete --exclude='.env' --exclude='runtime/' --exclude='.venv/' "$SRC_DIR/" "$APP_DIR/"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/python" -m compileall -q "$APP_DIR/newsbot" "$APP_DIR/main.py"
echo "Deployment prepared. Backup: $BACKUP"
