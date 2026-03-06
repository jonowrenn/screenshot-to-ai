#!/usr/bin/env python3
"""
screenshot-to-ai  —  macOS menubar app
Watches your Screenshots folder and auto-pastes new screenshots
into your open Claude.ai or ChatGPT Chrome tab.
"""

import os
import time
import threading
import subprocess
import glob
from typing import Optional, Tuple
import rumps
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Config ─────────────────────────────────────────────────────────────────────

SCREENSHOT_EXTS  = {".png", ".jpg", ".jpeg"}
DEBOUNCE_SECONDS = 2.0

# ── Helpers ────────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[screenshot-to-ai] {msg}", flush=True)


def run_applescript(script: str) -> Tuple[str, str]:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True
    )
    return result.stdout.strip(), result.stderr.strip()


def get_screenshot_dirs() -> list:
    dirs = []
    r = subprocess.run(
        ["defaults", "read", "com.apple.screencapture", "location"],
        capture_output=True, text=True
    )
    if r.returncode == 0 and r.stdout.strip():
        custom = os.path.expanduser(r.stdout.strip())
        if os.path.isdir(custom):
            dirs.append(custom)
    for fallback in ["~/Desktop", "~/Pictures/Screenshots"]:
        path = os.path.expanduser(fallback)
        if os.path.isdir(path) and path not in dirs:
            dirs.append(path)
    return dirs


def is_real_screenshot(path: str) -> bool:
    """
    Return True only for fully-written, non-temporary screenshot files.
    macOS first creates a hidden .Screenshot...png temp file, then renames
    it to the final Screenshot...png — we only want the final file.
    """
    name = os.path.basename(path)
    # Ignore hidden / temp files (start with a dot)
    if name.startswith("."):
        return False
    # Must be a recognised image extension
    _, ext = os.path.splitext(name)
    if ext.lower() not in SCREENSHOT_EXTS:
        return False
    return True


# ── Core functions ─────────────────────────────────────────────────────────────

def copy_image_to_clipboard(filepath: str) -> bool:
    ext = os.path.splitext(filepath)[1].lower()
    filetype = "«class PNGf»" if ext == ".png" else "JPEG picture"
    _, err = run_applescript(
        f'set the clipboard to (read (POSIX file "{filepath}") as {filetype})'
    )
    if err:
        log(f"  Clipboard error: {err}")
        return False
    return True


def find_ai_tab() -> Optional[Tuple[int, int]]:
    """
    Find the best Claude/ChatGPT tab to use, in priority order:
      1. The active (foreground) tab of any window if it's an AI tab
      2. Any other AI tab that is NOT in a collapsed tab group (loading=false trick)
      3. Any AI tab as a last resort
    Returns (window_index, tab_index) — 1-based — or None.
    """
    out, err = run_applescript("""
    tell application "Google Chrome"
        -- Pass 1: prefer the active tab if it's an AI tab
        set winIdx to 0
        repeat with w in windows
            set winIdx to winIdx + 1
            set activeTab to active tab of w
            set u to URL of activeTab
            if u contains "claude.ai" or u contains "chat.openai.com" or u contains "chatgpt.com" then
                set activeTabIdx to active tab index of w
                return "active," & (winIdx as string) & "," & (activeTabIdx as string)
            end if
        end repeat

        -- Pass 2: any visible (loading or complete) AI tab
        set winIdx to 0
        repeat with w in windows
            set winIdx to winIdx + 1
            set tabIdx to 0
            repeat with t in tabs of w
                set tabIdx to tabIdx + 1
                set u to URL of t
                if u contains "claude.ai" or u contains "chat.openai.com" or u contains "chatgpt.com" then
                    -- Prefer tabs that are the active tab of their window
                    if tabIdx is equal to (active tab index of w) then
                        return "active," & (winIdx as string) & "," & (tabIdx as string)
                    end if
                end if
            end repeat
        end repeat

        -- Pass 3: any AI tab at all
        set winIdx to 0
        repeat with w in windows
            set winIdx to winIdx + 1
            set tabIdx to 0
            repeat with t in tabs of w
                set tabIdx to tabIdx + 1
                set u to URL of t
                if u contains "claude.ai" or u contains "chat.openai.com" or u contains "chatgpt.com" then
                    return "any," & (winIdx as string) & "," & (tabIdx as string)
                end if
            end repeat
        end repeat

        return ""
    end tell
    """)
    if err:
        log(f"  find_ai_tab error: {err}")
    if not out:
        return None
    parts = out.split(",")
    try:
        # parts = ["active"/"any", window_idx, tab_idx]
        return int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return None


def get_tab_url(window_idx: int, tab_idx: int) -> str:
    out, _ = run_applescript(f"""
    tell application "Google Chrome"
        return URL of tab {tab_idx} of window {window_idx}
    end tell
    """)
    return out.lower()


def activate_tab_and_paste(window_idx: int, tab_idx: int, filepath: str) -> bool:
    # ── Step 1: Copy image to clipboard ──────────────────────────────────────
    log("  Step 1: Copying image to clipboard...")
    if not copy_image_to_clipboard(filepath):
        return False
    log("  Step 1: ✅ clipboard set")

    # ── Step 2: Activate Chrome and the right tab ─────────────────────────────
    log("  Step 2: Activating Chrome tab...")
    _, err = run_applescript(f"""
    tell application "Google Chrome"
        set index of window {window_idx} to 1
        set active tab index of window {window_idx} to {tab_idx}
        activate
    end tell
    """)
    if err:
        log(f"  Step 2 warning: {err}")
    time.sleep(0.8)
    log("  Step 2: ✅ Chrome activated")

    # ── Step 3: Click the chat input ──────────────────────────────────────────
    log("  Step 3: Clicking chat input...")
    bounds_out, err = run_applescript(f"""
    tell application "Google Chrome"
        return bounds of window {window_idx}
    end tell
    """)
    if err or not bounds_out:
        log(f"  Step 3 bounds error: {err}")
    else:
        try:
            coords  = [int(x.strip()) for x in bounds_out.split(",")]
            left, top, right, bottom = coords
            click_x = (left + right) // 2
            click_y = bottom - 110       # chat input is near the bottom
            log(f"  Step 3: clicking at ({click_x}, {click_y})")
            _, err = run_applescript(f"""
            tell application "System Events"
                tell process "Google Chrome"
                    click at {{{click_x}, {click_y}}}
                end tell
            end tell
            """)
            if err:
                log(f"  Step 3 click warning: {err}")
            else:
                log("  Step 3: ✅ clicked")
        except Exception as e:
            log(f"  Step 3 exception: {e}")

    time.sleep(0.4)

    # ── Step 4: Paste ─────────────────────────────────────────────────────────
    log("  Step 4: Sending Cmd+V...")
    _, err = run_applescript("""
    tell application "System Events"
        key code 9 using command down
    end tell
    """)
    if err:
        log(f"  Step 4 error: {err}")
        return False
    log("  Step 4: ✅ paste sent")
    return True


# ── File watcher ───────────────────────────────────────────────────────────────

class ScreenshotHandler(FileSystemEventHandler):
    def __init__(self, app: "ScreenshotToAIApp"):
        super().__init__()
        self.app = app
        self._last_fired: float = 0

    def on_created(self, event):
        if event.is_directory:
            return
        # Ignore the hidden temp file macOS creates first (.Screenshot...png)
        if not is_real_screenshot(event.src_path):
            return
        self._trigger(event.src_path)

    def on_moved(self, event):
        # macOS renames the temp file to the final name — catch that too
        if event.is_directory:
            return
        if not is_real_screenshot(event.dest_path):
            return
        self._trigger(event.dest_path)

    def _trigger(self, path: str):
        now = time.time()
        if now - self._last_fired < DEBOUNCE_SECONDS:
            log(f"  (debounced: {os.path.basename(path)})")
            return
        self._last_fired = now
        log(f"📸 Detected: {os.path.basename(path)}")
        # Wait for macOS to finish writing the file
        time.sleep(1.0)
        self.app.handle_new_screenshot(path)


# ── Menubar app ────────────────────────────────────────────────────────────────

class ScreenshotToAIApp(rumps.App):
    def __init__(self):
        super().__init__(name="ScreenshotToAI", title="📸", quit_button="Quit")
        self.enabled = True
        self.observer = None

        self.toggle_item = rumps.MenuItem("Auto-paste: ON ✅", callback=self.toggle)
        self.status_item = rumps.MenuItem("Last: —")
        self.test_item   = rumps.MenuItem("🔁 Paste last screenshot", callback=self.paste_last)

        self.menu = [
            self.toggle_item,
            None,
            self.test_item,
            None,
            self.status_item,
        ]
        self._start_watcher()

    # ── Toggle ────────────────────────────────────────────────────────────────

    def toggle(self, sender):
        self.enabled = not self.enabled
        if self.enabled:
            sender.title = "Auto-paste: ON ✅"
            self.title = "📸"
            self._start_watcher()
            log("Auto-paste enabled")
        else:
            sender.title = "Auto-paste: OFF ⏸"
            self.title = "📸✕"
            self._stop_watcher()
            log("Auto-paste disabled")

    # ── Manual retry ──────────────────────────────────────────────────────────

    def paste_last(self, _):
        dirs = get_screenshot_dirs()
        candidates = []
        for d in dirs:
            for ext in ("*.png", "*.jpg", "*.jpeg"):
                candidates += [
                    f for f in glob.glob(os.path.join(d, f"Screenshot{ext[1:]}"))
                    if not os.path.basename(f).startswith(".")
                ]
        if not candidates:
            self._notify("No screenshots found ⚠️", "Take a screenshot first.")
            return
        latest = max(candidates, key=os.path.getmtime)
        log(f"Manual paste: {latest}")
        threading.Thread(target=self._paste_screenshot, args=(latest,), daemon=True).start()

    # ── Watcher ───────────────────────────────────────────────────────────────

    def _start_watcher(self):
        if self.observer and self.observer.is_alive():
            return
        handler = ScreenshotHandler(self)
        self.observer = Observer()
        for d in get_screenshot_dirs():
            self.observer.schedule(handler, d, recursive=False)
            log(f"Watching: {d}")
        self.observer.start()

    def _stop_watcher(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None

    # ── Core logic ────────────────────────────────────────────────────────────

    def handle_new_screenshot(self, filepath: str):
        if not self.enabled:
            return
        threading.Thread(
            target=self._paste_screenshot, args=(filepath,), daemon=True
        ).start()

    def _paste_screenshot(self, filepath: str):
        filename = os.path.basename(filepath)
        log(f"Processing: {filename}")

        tab = find_ai_tab()
        if tab is None:
            log("  ❌ No Claude/ChatGPT tab found")
            self._notify("No AI tab found ⚠️", "Open Claude.ai or ChatGPT in Chrome.")
            self._set_status("⚠️  No AI tab found")
            return

        log(f"  Found tab: window {tab[0]}, tab {tab[1]}")
        url     = get_tab_url(tab[0], tab[1])
        service = "Claude" if "claude.ai" in url else "ChatGPT"
        log(f"  Service: {service}")

        try:
            if activate_tab_and_paste(tab[0], tab[1], filepath):
                self._notify(f"Pasted to {service} ✅", filename)
                self._set_status(f"✅  {filename} → {service}")
                log(f"✅ Done")
            else:
                self._notify("Clipboard error ❌", "Could not copy image.")
                self._set_status("❌  clipboard error")
        except Exception as e:
            log(f"  ❌ Exception: {e}")
            self._notify("Error ❌", str(e))
            self._set_status(f"❌  {e}")

    def _notify(self, subtitle: str, message: str):
        rumps.notification("Screenshot to AI", subtitle, message, sound=False)

    def _set_status(self, text: str):
        self.status_item.title = f"Last: {text}"


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log("Starting…")
    ScreenshotToAIApp().run()
