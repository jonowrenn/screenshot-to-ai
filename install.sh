#!/bin/bash
# ── Screenshot to AI — one-command installer ──────────────────────────────────
#
# Installs a real macOS .app into ~/Applications and optionally /Applications.
# Run this once, then double-click the app or enable "Start at Login" in menu.
#
#   curl -fsSL https://raw.githubusercontent.com/jonowrenn/screenshot-to-ai/main/install.sh | bash

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
echo "    ✅ Source downloaded → $SRC_DIR"
echo ""

# ── 2. Install Python dependencies ───────────────────────────────────────────
echo "  ▸ Installing Python packages…"
pip3 install rumps watchdog pyobjc-core pyobjc-framework-Cocoa \
    --quiet --break-system-packages 2>/dev/null \
  || pip3 install rumps watchdog pyobjc-core pyobjc-framework-Cocoa --quiet
echo "    ✅ Python packages ready"
echo ""

# ── 3. Build the .app bundle ──────────────────────────────────────────────────
echo "  ▸ Building $APP_NAME.app…"

PYTHON="$(which python3)"
mkdir -p "$HOME/Applications"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# Bundle the source inside the .app so it's fully self-contained
cp "$SRC_DIR/app.py" "$APP_DIR/Contents/Resources/app.py"

# Launcher script — runs python3 with the bundled source
cat > "$APP_DIR/Contents/MacOS/$APP_NAME" << LAUNCHER
#!/bin/bash
RESOURCES="\$(cd "\$(dirname "\$0")/../Resources" && pwd)"
exec "$PYTHON" "\$RESOURCES/app.py"
LAUNCHER
chmod +x "$APP_DIR/Contents/MacOS/$APP_NAME"

# Info.plist — LSUIElement=true keeps it out of the Dock (menu-bar-only app)
cat > "$APP_DIR/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>   <string>$APP_NAME</string>
  <key>CFBundleIdentifier</key>   <string>$BUNDLE_ID</string>
  <key>CFBundleName</key>         <string>Screenshot to AI</string>
  <key>CFBundleDisplayName</key>  <string>Screenshot to AI</string>
  <key>CFBundleVersion</key>      <string>1.0.0</string>
  <key>CFBundleShortVersionString</key> <string>1.0</string>
  <key>LSMinimumSystemVersion</key>    <string>10.15</string>
  <key>LSUIElement</key>          <true/>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>NSHumanReadableDescription</key>
  <string>Automatically pastes screenshots into Claude.ai or ChatGPT.</string>
</dict>
</plist>
PLIST

echo "    ✅ $APP_NAME.app built"
echo ""

# ── 4. Ask about /Applications ───────────────────────────────────────────────
echo "  The app is ready at:"
echo "    $APP_DIR"
echo ""
read -r -p "  Also copy to /Applications (system-wide, requires password)? [y/N] " choice
if [[ "$choice" =~ ^[Yy]$ ]]; then
    sudo cp -R "$APP_DIR" /Applications/
    echo "    ✅ Copied to /Applications/$APP_NAME.app"
fi

# ── 5. Grant Gatekeeper trust so macOS doesn't block on first launch ──────────
xattr -cr "$APP_DIR" 2>/dev/null || true
if [ -d "/Applications/$APP_NAME.app" ]; then
    sudo xattr -cr "/Applications/$APP_NAME.app" 2>/dev/null || true
fi

# ── 6. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "  ✅  All done!"
echo ""
echo "  How to use:"
echo "    1. Open Finder → Applications (or use Spotlight: ScreenshotToAI)"
echo "    2. Double-click ScreenshotToAI to launch"
echo "    3. The 📸 icon appears in your menu bar"
echo "    4. Click it and enable 'Start at Login' so it's always there"
echo ""
echo "  Required permissions (macOS will prompt on first use):"
echo "    • Accessibility  — for pasting into Chrome"
echo "    • Screen Recording — macOS may request this on Ventura+"
echo "    • Notifications  — for paste confirmations"
echo ""
