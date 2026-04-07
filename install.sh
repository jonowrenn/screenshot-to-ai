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
"$PYTHON" -m pip install rumps watchdog "pyobjc-core==9.2" "pyobjc-framework-Cocoa==9.2" \
    --quiet --break-system-packages 2>/dev/null \
  || "$PYTHON" -m pip install rumps watchdog "pyobjc-core==9.2" "pyobjc-framework-Cocoa==9.2" \
    --user --quiet 2>/dev/null \
  || "$PYTHON" -m pip install rumps watchdog "pyobjc-core==9.2" "pyobjc-framework-Cocoa==9.2" --quiet

# Verify the packages are actually importable with this Python
if ! "$PYTHON" -c "import rumps, watchdog" 2>/dev/null; then
  echo "    ❌ Package import failed. Trying --user install…"
  "$PYTHON" -m pip install rumps watchdog "pyobjc-core==9.2" "pyobjc-framework-Cocoa==9.2" --user 2>/dev/null
  if ! "$PYTHON" -c "import rumps, watchdog" 2>/dev/null; then
    echo "    ❌ Could not install packages for $PYTHON"
    echo "       Try: $PYTHON -m pip install rumps watchdog pyobjc-core==9.2 pyobjc-framework-Cocoa==9.2"
    exit 1
  fi
fi
echo "    ✅ Python packages ready ($PYTHON)"
echo ""

# ── 4. Stop any running instance before replacing files ───────────────────────
# Note: pkill is intentionally NOT used here — macOS Sequoia's App Management
# blocks it and shows a scary "Terminal was prevented from modifying apps" banner.
# The installer replaces files in place; any running old instance will keep
# working until the user relaunches the new version.
osascript -e 'quit app "ScreenshotToAI"' 2>/dev/null || true
sleep 0.5

# ── 5. Build .app bundle ──────────────────────────────────────────────────────
echo "  ▸ Building $APP_NAME.app…"

mkdir -p "$HOME/Applications"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# Bundle app source
cp "$SRC_DIR/app.py" "$APP_DIR/Contents/Resources/app.py"

# ── Launcher binary ───────────────────────────────────────────────────────────
# A compiled C binary is the CFBundleExecutable. This is critical for two reasons:
#  1. macOS only respects LSUIElement=true for compiled Mach-O binaries, not shell
#     scripts — a shell script launcher causes Python Launcher to flash on every open.
#  2. The binary fork+exec's python3 (not exec), so the compiled binary stays alive
#     as the registered app process. Python Launcher is only triggered when python3
#     is the *main* process of a .app bundle, not when it's a child of a real binary.
LAUNCHER_PATH="$APP_DIR/Contents/MacOS/$APP_NAME"
LAUNCHER_C="/tmp/screenshot-ai-launcher-$$.c"

cat > "$LAUNCHER_C" << 'CSRC'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <mach-o/dyld.h>

int main(void) {
    /* ── locate app.py: .../Contents/MacOS/<binary> → .../Contents/Resources/app.py */
    char exe[4096] = {0};
    uint32_t sz = (uint32_t)sizeof(exe);
    if (_NSGetExecutablePath(exe, &sz) != 0) return 1;
    char *sl = strrchr(exe, '/');
    if (!sl) return 1;
    *sl = '\0';
    char app_py[4096];
    snprintf(app_py, sizeof(app_py), "%s/../Resources/app.py", exe);

    /* ── log file ── */
    const char *home = getenv("HOME");
    if (!home) home = "/tmp";
    char logdir[4096], logpath[4096];
    snprintf(logdir,  sizeof(logdir),  "%s/Library/Logs", home);
    snprintf(logpath, sizeof(logpath), "%s/Library/Logs/screenshot-to-ai.log", home);
    mkdir(logdir, 0755);

    /* ── fork: parent waits, child exec's python3 ── */
    pid_t pid = fork();
    if (pid < 0) return 1;
    if (pid == 0) {
        /* child: redirect stdout+stderr → log, then exec python3 */
        int fd = open(logpath, O_WRONLY|O_CREAT|O_APPEND, 0644);
        if (fd >= 0) { dup2(fd, 1); dup2(fd, 2); close(fd); }
        execl("__PYTHON__", "python3", app_py, (char *)NULL);
        _exit(1);
    }
    /* parent waits so the compiled binary stays alive as the .app process */
    int status = 0;
    waitpid(pid, &status, 0);
    return WEXITSTATUS(status);
}
CSRC

# Substitute the detected Python path into the C source
sed -i '' "s|__PYTHON__|$PYTHON|g" "$LAUNCHER_C"

COMPILED=0
if command -v cc &>/dev/null; then
  if cc "$LAUNCHER_C" -o "$LAUNCHER_PATH" 2>/dev/null; then
    COMPILED=1
    echo "    ✅ Native launcher compiled (Python Launcher will not appear)"
  fi
fi
rm -f "$LAUNCHER_C"

if [ "$COMPILED" -eq 0 ]; then
  # Fallback: shell script launcher (Python Launcher may briefly flash on open)
  echo "    ⚠️  cc not found — using shell script launcher"
  printf '#!/bin/bash\n'                                                         > "$LAUNCHER_PATH"
  printf 'LOG="$HOME/Library/Logs/screenshot-to-ai.log"\n'                     >> "$LAUNCHER_PATH"
  printf 'mkdir -p "$(dirname "$LOG")"\n'                                       >> "$LAUNCHER_PATH"
  printf 'RESOURCES="$(cd "$(dirname "$0")/../Resources" && pwd)"\n'            >> "$LAUNCHER_PATH"
  printf '%s "$RESOURCES/app.py" >> "$LOG" 2>&1\n' "$PYTHON"                   >> "$LAUNCHER_PATH"
fi
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
  <key>LSBackgroundOnly</key>
  <false/>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSSupportsAutomaticGraphicsSwitching</key>
  <true/>
</dict>
</plist>
INFOPLIST

# ── App icon (.icns from icon.png) ────────────────────────────────────────────
ICON_SRC="$SRC_DIR/icon.png"
ICON_OK=0
if [ -f "$ICON_SRC" ] && command -v sips &>/dev/null && command -v iconutil &>/dev/null; then
  ICONSET_PARENT="$(mktemp -d)"
  ICONSET="$ICONSET_PARENT/AppIcon.iconset"
  mkdir -p "$ICONSET"

  SIPS_OK=1
  for sz in 16 32 128 256 512; do
    sips -z $sz $sz "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}.png" &>/dev/null || SIPS_OK=0
    sz2=$((sz * 2))
    sips -z $sz2 $sz2 "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}@2x.png" &>/dev/null || SIPS_OK=0
  done

  ICONUTIL_ERR="$(iconutil -c icns "$ICONSET" -o "$APP_DIR/Contents/Resources/AppIcon.icns" 2>&1)"
  if [ -z "$ICONUTIL_ERR" ] && [ -f "$APP_DIR/Contents/Resources/AppIcon.icns" ]; then
    ICON_OK=1
    echo "    ✅ Icon applied"
  else
    [ -n "$ICONUTIL_ERR" ] && echo "    ⚠️  iconutil: $ICONUTIL_ERR"
    [ "$SIPS_OK" -eq 0 ] && echo "    ⚠️  sips resize had errors"
    # Fallback: copy the PNG directly — macOS will use it as-is
    cp "$ICON_SRC" "$APP_DIR/Contents/Resources/AppIcon.png" 2>/dev/null && ICON_OK=1 \
      && echo "    ✅ Icon applied (PNG fallback)"
  fi
  rm -rf "$ICONSET_PARENT"
elif [ -f "$ICON_SRC" ]; then
  # sips/iconutil not available — copy PNG directly
  cp "$ICON_SRC" "$APP_DIR/Contents/Resources/AppIcon.png" 2>/dev/null && ICON_OK=1 \
    && echo "    ✅ Icon applied (PNG fallback)"
fi
[ "$ICON_OK" -eq 0 ] && echo "    ⚠️  Icon could not be applied (app will use default icon)"

# Bust macOS icon cache so the new icon shows immediately
LSREG="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"
"$LSREG" -u "$APP_DIR" 2>/dev/null || true
"$LSREG" -f "$APP_DIR" 2>/dev/null || true
osascript -e "tell application \"Finder\" to update item (POSIX file \"$APP_DIR\" as alias)" 2>/dev/null || true

echo "    ✅ $APP_NAME.app built → $APP_DIR"
echo ""

# ── 6. Clear Gatekeeper quarantine ───────────────────────────────────────────
xattr -cr "$APP_DIR" 2>/dev/null || true

# ── 7. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "  ✅  Done!"
echo ""
echo "  Open Finder → your Home folder → Applications → double-click ScreenshotToAI"
echo "  The 📸 icon will appear in your menu bar — no extra permissions needed."
echo ""
echo "  First launch: macOS will ask for Accessibility permission — allow it."
echo "  Then click the 📸 icon → 'Start at Login' to keep it in your menu bar."
echo ""
