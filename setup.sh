#!/bin/bash
# ── screenshot-to-ai  ·  one-time installer ───────────────────────────────────
# Run this once from Terminal.  After that, the app lives in your menu bar
# permanently and auto-starts on every login — no Terminal needed ever again.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "  📸  Screenshot to AI — Setup"
echo "  ──────────────────────────────"
echo ""

# ── 1. Install Python dependencies ────────────────────────────────────────────
echo "  ▸ Installing Python packages…"
pip3 install rumps watchdog pyobjc-core pyobjc-framework-Cocoa \
    --quiet --break-system-packages 2>/dev/null \
  || pip3 install rumps watchdog pyobjc-core pyobjc-framework-Cocoa --quiet
echo "    ✅ Packages ready"
echo ""

# ── 2. Install the Launch Agent (auto-start on login) ─────────────────────────
PYTHON="$(which python3)"
PLIST="$HOME/Library/LaunchAgents/com.screenshot-to-ai.plist"
LOG="$HOME/Library/Logs/screenshot-to-ai.log"

echo "  ▸ Installing Launch Agent (auto-start on login)…"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.screenshot-to-ai</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT_DIR/app.py</string>
    </array>

    <!-- Start automatically at every login -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Restart if it crashes, but NOT if the user quits cleanly -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
PLISTEOF

# Unload first in case an old version is running, then load fresh
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load  "$PLIST"
echo "    ✅ Launch Agent installed"
echo ""

# ── 3. Done ───────────────────────────────────────────────────────────────────
echo "  ✅  All done!  The 📸 icon should appear in your menu bar now."
echo ""
echo "  • It will auto-start every time you log in."
echo "  • Use the menu to toggle auto-paste on/off or remove the login item."
echo "  • Logs: $LOG"
echo ""
echo "  To uninstall completely:"
echo "    launchctl unload \"$PLIST\" && rm \"$PLIST\""
echo ""
