#!/bin/bash
# ── Screenshot to AI — one-command installer ──────────────────────────────────
#
# curl -fsSL https://raw.githubusercontent.com/jonowrenn/screenshot-to-ai/main/install.sh | bash
#
# Builds a real .app bundle in ~/Applications.
# Run once, then double-click or enable "Start at Login" from the menu bar icon.

set -e

REPO="jonowrenn/screenshot-to-ai"
BRANCH="main"
APP_NAME="ScreenshotToAI"
BUNDLE_ID="com.screenshot-to-ai"
SRC_DIR="$HOME/.screenshot-to-ai"
APP_DIR="$HOME/Applications/$APP_NAME.app"

echo ""
echo "  📸  Screenshot to AI — Installer"
echo "  ──────────────────────────────────"
echo ""

# ── 1. Download source ────────────────────────────────────────────────────────
echo "  ▸ Downloading latest source…"
rm -rf "$SRC_DIR"
mkdir -p "$SRC_DIR"
curl -fsSL "https://github.com/$REPO/archive/$BRANCH.tar.gz" \
  | tar -xz -C "$SRC_DIR" --strip-components=1
echo "    ✅ Source downloaded"
echo ""

# ── 2. Install Python dependencies ───────────────────────────────────────────
echo "  ▸ Installing Python packages…"
pip3 install rumps watchdog pyobjc-core pyobjc-framework-Cocoa \
    --quiet --break-system-packages 2>/dev/null \
  || pip3 install rumps watchdog pyobjc-core pyobjc-framework-Cocoa --quiet
echo "    ✅ Python packages ready"
echo ""

# ── 3. Build .app bundle ──────────────────────────────────────────────────────
echo "  ▸ Building $APP_NAME.app…"

PYTHON="$(which python3)"
mkdir -p "$HOME/Applications"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# Bundle app source
cp "$SRC_DIR/app.py" "$APP_DIR/Contents/Resources/app.py"

# ── Launcher script (printf avoids heredoc quote-escaping issues) ─────────────
LAUNCHER_PATH="$APP_DIR/Contents/MacOS/$APP_NAME"
printf '#!/bin/bash\n' > "$LAUNCHER_PATH"
printf 'RESOURCES="$(cd "$(dirname "$0")/../Resources" && pwd)"\n' >> "$LAUNCHER_PATH"
printf 'exec %s "$RESOURCES/app.py"\n' "$PYTHON" >> "$LAUNCHER_PATH"
chmod +x "$LAUNCHER_PATH"

# ── Info.plist ────────────────────────────────────────────────────────────────
PLIST_PATH="$APP_DIR/Contents/Info.plist"
cat > "$PLIST_PATH" << INFOPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>${APP_NAME}</string>
  <key>CFBundleIdentifier</key>
  <string>${BUNDLE_ID}</string>
  <key>CFBundleName</key>
  <string>Screenshot to AI</string>
  <key>CFBundleDisplayName</key>
  <string>Screenshot to AI</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleVersion</key>
  <string>1.0.0</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>LSMinimumSystemVersion</key>
  <string>10.15</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
INFOPLIST

# ── App icon (.icns from icon.png) ────────────────────────────────────────────
ICON_SRC="$SRC_DIR/icon.png"
if [ -f "$ICON_SRC" ] && command -v sips &>/dev/null && command -v iconutil &>/dev/null; then
  ICONSET="$TMPDIR/AppIcon.iconset"
  rm -rf "$ICONSET" && mkdir -p "$ICONSET"

  for sz in 16 32 64 128 256 512; do
    sips -z $sz $sz "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}.png"      &>/dev/null
    sz2=$((sz * 2))
    sips -z $sz2 $sz2 "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}@2x.png" &>/dev/null
  done

  iconutil -c icns "$ICONSET" -o "$APP_DIR/Contents/Resources/AppIcon.icns" 2>/dev/null \
    && echo "    ✅ Icon applied" || echo "    ⚠️  Icon conversion failed (app will use default icon)"
  rm -rf "$ICONSET"
fi

echo "    ✅ $APP_NAME.app built → $APP_DIR"
echo ""

# ── 4. Clear Gatekeeper quarantine ───────────────────────────────────────────
xattr -cr "$APP_DIR" 2>/dev/null || true

# ── 5. Optional: copy to /Applications ───────────────────────────────────────
echo "  The app is in ~/Applications."
echo ""
# Read from /dev/tty so this works even when script is piped via curl | bash
if read -r -p "  Also copy to /Applications (system-wide)? [y/N] " _choice </dev/tty 2>/dev/null; then
  case "$_choice" in
    [Yy]*)
      sudo cp -R "$APP_DIR" /Applications/
      sudo xattr -cr "/Applications/$APP_NAME.app" 2>/dev/null || true
      echo "    ✅ Copied to /Applications/$APP_NAME.app"
      ;;
  esac
fi

# ── 6. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "  ✅  Done!  Open Finder → Applications → double-click ScreenshotToAI"
echo ""
echo "  First launch: macOS will ask for Accessibility permission — allow it."
echo "  Then click the 📸 icon and enable 'Start at Login' to make it permanent."
echo ""
echo "  Tip: to view logs any time:"
echo "    tail -f ~/Library/Logs/screenshot-to-ai.log"
echo ""
