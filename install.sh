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

# ── 1. Detect Python (must happen first so pip uses the same interpreter) ─────
# Prefer system / Homebrew Python over any IDE-bundled python3.
# PyCharm registers its own python3 on PATH; using it causes PyCharm to open.
PYTHON=""
for candidate in \
    /usr/bin/python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    "$(command -v python3 2>/dev/null)" \
    "$(command -v python 2>/dev/null)"; do
  if [ -x "$candidate" ]; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "  ❌ Python 3 not found. Install it from https://www.python.org and re-run."
  exit 1
fi

# ── 2. Download source ────────────────────────────────────────────────────────
echo "  ▸ Downloading latest source…"
rm -rf "$SRC_DIR"
mkdir -p "$SRC_DIR"
curl -fsSL "https://github.com/$REPO/archive/$BRANCH.tar.gz" \
  | tar -xz -C "$SRC_DIR" --strip-components=1
echo "    ✅ Source downloaded"
echo ""

# ── 3. Install Python dependencies (using the same Python we'll run the app with)
echo "  ▸ Installing Python packages…"
"$PYTHON" -m pip install rumps watchdog pyobjc-core pyobjc-framework-Cocoa \
    --quiet --break-system-packages 2>/dev/null \
  || "$PYTHON" -m pip install rumps watchdog pyobjc-core pyobjc-framework-Cocoa --quiet
echo "    ✅ Python packages ready"
echo ""

# ── 4. Stop any running instance before replacing files ───────────────────────
pkill -f "app.py" 2>/dev/null || true
sleep 0.5

# ── 5. Build .app bundle ──────────────────────────────────────────────────────
echo "  ▸ Building $APP_NAME.app…"

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
# Do NOT use exec here. exec would replace the shell process with python3,
# causing macOS to remap the .app process to Python Launcher (which ignores
# our LSUIElement=true and our icon). By NOT using exec the shell stays alive
# as the bundle's main process, macOS respects LSUIElement, and our icon shows.
printf '%s "$RESOURCES/app.py"\n' "$PYTHON" >> "$LAUNCHER_PATH"
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
  ICONSET_PARENT="$(mktemp -d)"
  ICONSET="$ICONSET_PARENT/AppIcon.iconset"
  mkdir -p "$ICONSET"

  # Standard Apple iconset sizes (64 is not a valid size — iconutil rejects it)
  for sz in 16 32 128 256 512; do
    sips -z $sz $sz "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}.png"      &>/dev/null
    sz2=$((sz * 2))
    sips -z $sz2 $sz2 "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}@2x.png" &>/dev/null
  done

  if iconutil -c icns "$ICONSET" -o "$APP_DIR/Contents/Resources/AppIcon.icns" 2>/dev/null; then
    echo "    ✅ Icon applied"
    # Touch the bundle so Finder re-reads it — no Dock restart needed
    touch "$APP_DIR"
  else
    echo "    ⚠️  Icon conversion failed (app will use default icon)"
  fi
  rm -rf "$ICONSET_PARENT"
fi

echo "    ✅ $APP_NAME.app built → $APP_DIR"
echo ""

# ── 6. Clear Gatekeeper quarantine ───────────────────────────────────────────
xattr -cr "$APP_DIR" 2>/dev/null || true

# ── 7. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "  ✅  Done!"
echo ""
echo "  Open Finder → your Home folder → Applications → double-click ScreenshotToAI"
echo ""
echo "  First launch: macOS will ask for Accessibility permission — allow it."
echo "  Then click the 📸 icon and enable 'Start at Login' to keep it in your menu bar."
echo ""
echo "  Tip: to view logs:"
echo "    tail -f ~/Library/Logs/screenshot-to-ai.log"
echo ""
