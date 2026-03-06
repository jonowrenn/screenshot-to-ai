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
import rumps
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Config ─────────────────────────────────────────────────────────────────────
WATCH_DIRS = [
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Pictures/Screenshots"),
]

AI_DOMAINS = ["claude.ai", "chat.openai.com", "chatgpt.com"]

SCREENSHOT_EXTS = {".png", ".jpg", ".jpeg"}

# Debounce: ignore files modified within this many seconds of each other
DEBOUNCE_SECONDS = 1.5

# ── AppleScript helpers ────────────────────────────────────────────────────────

def run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True
    )
    return result.stdout.strip()


def find_ai_tab() -> tuple[int, int] | None:
    """
    Returns (window_index, tab_index) of the first Claude/ChatGPT tab found,
    or None if no matching tab is open.
    Indices are 1-based (AppleScript convention).
    """
    script = """
    tell application "Google Chrome"
        set winIdx to 0
        repeat with w in windows
            set winIdx to winIdx + 1
            set tabIdx to 0
            repeat with t in tabs of w
                set tabIdx to tabIdx + 1
                set u to URL of t
                if u contains "claude.ai" or u contains "chat.openai.com" or u contains "chatgpt.com" then
                    return (winIdx as string) & "," & (tabIdx as string)
                end if
            end repeat
        end repeat
        return ""
    end tell
    """
    result = run_applescript(script)
    if not result:
        return None
    parts = result.split(",")
    if len(parts) == 2:
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None
    return None


def activate_tab_and_paste(window_idx: int, tab_idx: int, filepath: str):
    """
    Switches Chrome to the target tab, copies the screenshot to the clipboard,
    clicks the chat input area, and pastes.
    """
    # 1. Copy the image file to clipboard using osascript + set the clipboard
    copy_script = f"""
    set theFile to POSIX file "{filepath}"
    set theImage to read theFile as JPEG picture
    set the clipboard to theImage
    """
    # Use Python Pillow + AppKit instead for reliability
    _copy_image_to_clipboard(filepath)

    # 2. Activate Chrome and switch to the right tab
    activate_script = f"""
    tell application "Google Chrome"
        set index of window {window_idx} to 1
        set active tab index of window {window_idx} to {tab_idx}
        activate
    end tell
    """
    run_applescript(activate_script)
    time.sleep(0.4)  # let Chrome come to foreground

    # 3. Click the chat input (click in the lower-center of the window)
    click_input_script = f"""
    tell application "Google Chrome"
        tell window {window_idx}
            set winBounds to bounds
            set winLeft to item 1 of winBounds
            set winTop to item 2 of winBounds
            set winRight to item 3 of winBounds
            set winBottom to item 4 of winBounds
            set clickX to (winLeft + winRight) / 2
            set clickY to winBottom - 80
        end tell
    end tell
    tell application "System Events"
        tell process "Google Chrome"
            click at {{(winLeft + winRight) / 2, winBottom - 80}}
        end tell
    end tell
    """
    # Simpler: just use Cmd+V after activating — Chrome will paste into focused input
    time.sleep(0.2)
    paste_script = """
    tell application "System Events"
        keystroke "v" using command down
    end tell
    """
    run_applescript(paste_script)


def _copy_image_to_clipboard(filepath: str):
    """Copy an image file to the macOS clipboard using AppKit."""
    script = f"""
    tell application "Finder"
        set theFile to POSIX file "{filepath}" as alias
    end tell
    do shell script "osascript -e 'set the clipboard to (read (POSIX file \\"{filepath}\\") as JPEG picture)'"
    """
    # Use a cleaner approach via pbcopy + Python
    subprocess.run([
        "python3", "-c",
        f"""
import AppKit, objc
img = AppKit.NSImage.alloc().initWithContentsOfFile_('{filepath}')
pb = AppKit.NSPasteboard.generalPasteboard()
pb.clearContents()
pb.writeObjects_([img])
"""
    ])


# ── File watcher ───────────────────────────────────────────────────────────────

class ScreenshotHandler(FileSystemEventHandler):
    def __init__(self, app: "ScreenshotToAIApp"):
        super().__init__()
        self.app = app
        self._last_fired: float = 0

    def on_created(self, event):
        if event.is_directory:
            return
        _, ext = os.path.splitext(event.src_path)
        if ext.lower() not in SCREENSHOT_EXTS:
            return

        # Debounce rapid duplicate events
        now = time.time()
        if now - self._last_fired < DEBOUNCE_SECONDS:
            return
        self._last_fired = now

        # Small delay so macOS finishes writing the file
        time.sleep(0.5)
        self.app.handle_new_screenshot(event.src_path)


# ── Menubar app ────────────────────────────────────────────────────────────────

class ScreenshotToAIApp(rumps.App):
    def __init__(self):
        super().__init__(
            name="ScreenshotToAI",
            title="📸",          # menubar icon (emoji fallback)
            quit_button="Quit",
        )
        self.menu = [
            rumps.MenuItem("Auto-paste: ON", callback=self.toggle),
            None,  # separator
            rumps.MenuItem("Last action: —"),
        ]
        self.enabled = True
        self.observer = None
        self._start_watcher()

    # ── Toggle ──────────────────────────────────────────────────────────────

    @rumps.clicked("Auto-paste: ON")
    def toggle(self, sender):
        self.enabled = not self.enabled
        if self.enabled:
            sender.title = "Auto-paste: ON"
            self.title = "📸"
            self._start_watcher()
        else:
            sender.title = "Auto-paste: OFF"
            self.title = "📸✕"
            self._stop_watcher()

    # ── Watcher lifecycle ────────────────────────────────────────────────────

    def _start_watcher(self):
        if self.observer and self.observer.is_alive():
            return
        handler = ScreenshotHandler(self)
        self.observer = Observer()
        for watch_dir in WATCH_DIRS:
            if os.path.isdir(watch_dir):
                self.observer.schedule(handler, watch_dir, recursive=False)
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

        # Run in background thread so we don't block the UI
        threading.Thread(
            target=self._paste_screenshot,
            args=(filepath,),
            daemon=True
        ).start()

    def _paste_screenshot(self, filepath: str):
        filename = os.path.basename(filepath)
        tab = find_ai_tab()

        if tab is None:
            rumps.notification(
                title="Screenshot to AI",
                subtitle="No AI tab found",
                message="Open Claude.ai or ChatGPT in Chrome first.",
                sound=False,
            )
            self._set_last_action(f"⚠️ No AI tab — {filename}")
            return

        try:
            activate_tab_and_paste(tab[0], tab[1], filepath)
            self._set_last_action(f"✅ Pasted {filename}")
            rumps.notification(
                title="Screenshot to AI",
                subtitle="Pasted!",
                message=f"{filename} → {'Claude' if 'claude' in self._get_tab_url(tab) else 'ChatGPT'}",
                sound=False,
            )
        except Exception as e:
            self._set_last_action(f"❌ Error: {e}")
            rumps.notification(
                title="Screenshot to AI",
                subtitle="Error pasting screenshot",
                message=str(e),
                sound=False,
            )

    def _get_tab_url(self, tab: tuple[int, int]) -> str:
        script = f"""
        tell application "Google Chrome"
            return URL of tab {tab[1]} of window {tab[0]}
        end tell
        """
        return run_applescript(script).lower()

    def _set_last_action(self, text: str):
        self.menu["Last action: —"].title = f"Last: {text}"


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ScreenshotToAIApp().run()
