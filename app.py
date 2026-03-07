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
from typing import Optional, Tuple, List, Dict
import rumps
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── NSSwitch toggle (macOS 10.15+) ─────────────────────────────────────────────
# Embeds a real iOS-style toggle switch directly inside a menu item view.
# Falls back to a plain checkmark if PyObjC / NSSwitch is unavailable.

try:
    import objc
    from Foundation import NSObject
    from AppKit import (
        NSMenuItem, NSView, NSSwitch, NSTextField,
        NSFont, NSColor, NSMakeRect,
    )
    _NSSWITCH_AVAILABLE = True
except Exception:
    _NSSWITCH_AVAILABLE = False


if _NSSWITCH_AVAILABLE:

    class _SwitchTarget(NSObject):
        """Thin ObjC target that forwards NSSwitch actions to a Python callable."""

        @objc.python_method
        def init_with_callback(self, callback):
            self = self.init()
            self._cb = callback
            return self

        def toggled_(self, sender):
            self._cb(bool(sender.state()))


    def _attach_switch(rumps_item, label: str, initial_on: bool, callback):
        """
        Replace *rumps_item*'s NSMenuItem view with a label + NSSwitch row.
        Returns the (_SwitchTarget, NSSwitch) tuple so callers can keep refs alive.
        """
        W, H = 230, 30

        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))

        # Label
        lbl = NSTextField.labelWithString_(label)
        lbl.setFrame_(NSMakeRect(14, 6, 150, 18))
        lbl.setFont_(NSFont.menuFontOfSize_(13.0))
        view.addSubview_(lbl)

        # NSSwitch
        sw = NSSwitch.alloc().initWithFrame_(NSMakeRect(168, 4, 51, 22))
        sw.setState_(1 if initial_on else 0)

        target = _SwitchTarget.alloc().init_with_callback(callback)
        sw.setTarget_(target)
        sw.setAction_(objc.selector(target.toggled_, signature=b'v@:@'))
        view.addSubview_(sw)

        # rumps.MenuItem IS an NSMenuItem subclass — setView_ works directly
        rumps_item.setView_(view)

        return target, sw  # caller must hold refs or they get GC'd

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


def get_screenshot_dirs() -> List[str]:
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
    name = os.path.basename(path)
    if name.startswith("."):
        return False
    _, ext = os.path.splitext(name)
    return ext.lower() in SCREENSHOT_EXTS


# ── Chrome tab helpers ─────────────────────────────────────────────────────────

def get_tab_url(window_idx: int, tab_idx: int) -> str:
    out, _ = run_applescript(f"""
    tell application "Google Chrome"
        return URL of tab {tab_idx} of window {window_idx}
    end tell
    """)
    return out.lower()


def get_tab_title(window_idx: int, tab_idx: int) -> str:
    out, _ = run_applescript(f"""
    tell application "Google Chrome"
        return title of tab {tab_idx} of window {window_idx}
    end tell
    """)
    return out


def is_ai_url(url: str) -> bool:
    return any(d in url for d in ["claude.ai", "chat.openai.com", "chatgpt.com"])


def scan_all_ai_tabs() -> List[Dict]:
    """
    Scan every tab in every Chrome window and return a list of AI tabs.
    Each entry: {"window": int, "tab": int, "url": str, "title": str, "active": bool}
    Uses 'output' not 'result' — 'result' is a reserved AppleScript keyword.
    """
    out, err = run_applescript("""
    tell application "Google Chrome"
        set output to ""
        set winIdx to 0
        repeat with w in windows
            set winIdx to winIdx + 1
            set activeIdx to active tab index of w
            set tabIdx to 0
            repeat with t in tabs of w
                set tabIdx to tabIdx + 1
                set u to URL of t
                if u contains "claude.ai" or u contains "chat.openai.com" or u contains "chatgpt.com" then
                    set isActive to (tabIdx is equal to activeIdx)
                    set ttl to title of t
                    set output to output & (winIdx as string) & "|" & (tabIdx as string) & "|" & u & "|" & ttl & "|" & (isActive as string) & "~ENTRY~"
                end if
            end repeat
        end repeat
        return output
    end tell
    """)
    if err:
        log(f"  scan_all_ai_tabs error: {err}")
    if not out:
        return []
    tabs = []
    for entry in out.split("~ENTRY~"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|", 4)
        if len(parts) == 5:
            try:
                tabs.append({
                    "window": int(parts[0]),
                    "tab":    int(parts[1]),
                    "url":    parts[2].lower(),
                    "title":  parts[3],
                    "active": parts[4].strip().lower() == "true",
                })
            except ValueError:
                continue
    log(f"  scan found {len(tabs)} AI tab(s)")
    return tabs


def find_active_ai_tab() -> Optional[Tuple[int, int]]:
    """Only checks the currently visible tab of each window (front-to-back)."""
    out, err = run_applescript("""
    tell application "Google Chrome"
        set winIdx to 0
        repeat with w in windows
            set winIdx to winIdx + 1
            set activeIdx to active tab index of w
            set t to active tab of w
            set u to URL of t
            if u contains "claude.ai" or u contains "chat.openai.com" or u contains "chatgpt.com" then
                return (winIdx as string) & "," & (activeIdx as string)
            end if
        end repeat
        return ""
    end tell
    """)
    if err:
        log(f"  find_active_ai_tab error: {err}")
    if not out:
        return None
    parts = out.split(",")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None


def verify_tab(window_idx: int, tab_idx: int) -> bool:
    url = get_tab_url(window_idx, tab_idx)
    return is_ai_url(url)


def service_name(url: str) -> str:
    return "Claude" if "claude.ai" in url else "ChatGPT"


# ── Clipboard & paste ──────────────────────────────────────────────────────────

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


def activate_tab_and_paste(window_idx: int, tab_idx: int, filepath: str) -> bool:
    log("  Step 1: Copying image to clipboard...")
    if not copy_image_to_clipboard(filepath):
        return False
    log("  Step 1: ✅ clipboard set")

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
    time.sleep(1.0)
    log("  Step 2: ✅ Chrome activated")

    log("  Step 3: Focusing input via JavaScript...")
    js = (
        "(function(){"
        "var el=document.getElementById('prompt-textarea');"
        "if(!el)el=document.querySelector('.ProseMirror');"
        "if(!el)el=document.querySelector('[contenteditable=\"true\"]');"
        "if(!el)el=document.querySelector('textarea');"
        "if(el){el.click();el.focus();"
        "return 'focused:'+el.tagName+(el.id?'#'+el.id:'');}"
        "return 'INPUT NOT FOUND';})()"
    )
    js_result, js_err = run_applescript(f"""
    tell application "Google Chrome"
        tell tab {tab_idx} of window {window_idx}
            execute javascript "{js}"
        end tell
    end tell
    """)
    log(f"  Step 3 JS: {js_result or js_err or 'no output'}")

    if "INPUT NOT FOUND" in (js_result or "") or not js_result:
        log("  Step 3b: JS focus failed — trying coordinate fallback...")
        bounds_out, _ = run_applescript(f"""
        tell application "Google Chrome"
            return bounds of window {window_idx}
        end tell
        """)
        if bounds_out:
            coords = [int(x.strip()) for x in bounds_out.split(",")]
            left, top, right, bottom = coords
            cx = (left + right) // 2
            for offset in [75, 55, 95]:
                run_applescript(f"""
                tell application "System Events"
                    tell process "Google Chrome"
                        click at {{{cx}, {bottom - offset}}}
                    end tell
                end tell
                """)
                time.sleep(0.15)

    time.sleep(0.5)

    log("  Step 4: Sending Cmd+V...")
    _, err = run_applescript(
        'tell application "System Events" to key code 9 using command down'
    )
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
        if not event.is_directory and is_real_screenshot(event.src_path):
            self._trigger(event.src_path)

    def on_moved(self, event):
        if not event.is_directory and is_real_screenshot(event.dest_path):
            self._trigger(event.dest_path)

    def _trigger(self, path: str):
        now = time.time()
        if now - self._last_fired < DEBOUNCE_SECONDS:
            return
        self._last_fired = now
        log(f"📸 Detected: {os.path.basename(path)}")
        time.sleep(1.0)
        self.app.handle_new_screenshot(path)


# ── Menubar app ────────────────────────────────────────────────────────────────

class ScreenshotToAIApp(rumps.App):
    def __init__(self):
        super().__init__(name="ScreenshotToAI", title="📸", quit_button="Quit")
        self.enabled  = True
        self.observer = None

        # Pinned target: set explicitly via "Choose Target" menu.
        # When set, ALL screenshots go here regardless of active tab.
        self._pinned_tab:     Optional[Tuple[int, int]] = None
        self._pinned_service: str = ""

        # Fallback memory: last tab we successfully pasted to.
        self._last_tab:     Optional[Tuple[int, int]] = None
        self._last_service: str = ""

        # ── Menu items ────────────────────────────────────────────────────────
        self.toggle_item = rumps.MenuItem("Auto-paste", callback=self.toggle)
        self.target_item = rumps.MenuItem("🎯 Set target tab", callback=self.set_target)
        self.pin_item    = rumps.MenuItem("   No target set — auto-detect", callback=self.clear_pin)
        self.test_item   = rumps.MenuItem("🔁 Paste last screenshot", callback=self.paste_last)
        self.status_item = rumps.MenuItem("Last: —")
        self.status_item.set_callback(None)

        self.menu = [
            self.toggle_item,
            None,
            self.target_item,
            self.pin_item,
            None,
            self.test_item,
            None,
            self.status_item,
        ]

        # NSSwitch is attached via a short timer AFTER the app finishes launching.
        # Setting the view during __init__ is too early — the NSMenu isn't
        # fully wired up yet and the view gets dropped silently.
        self._switch_refs = None
        self._nsswitch    = None
        self.toggle_item.state = 1   # checkmark fallback until switch attaches

        if _NSSWITCH_AVAILABLE:
            rumps.Timer(self._deferred_attach_switch, 0.4).start()

        self._start_watcher()

        # Auto-discover an AI tab on startup so first screenshot works immediately
        threading.Thread(target=self._auto_discover, daemon=True).start()

    # ── Deferred NSSwitch setup ───────────────────────────────────────────────

    def _deferred_attach_switch(self, timer):
        """
        Called ~0.4 s after launch so the NSMenu is fully built.
        Finds the 'Auto-paste' menu item by title inside the real NSMenu
        and replaces it with a label + NSSwitch custom view.
        """
        import traceback
        timer.stop()
        try:
            # self._menu is the rumps NSMenu subclass — find our item by title
            item = self._menu.itemWithTitle_("Auto-paste")
            if item is None:
                log("NSSwitch setup: item 'Auto-paste' not found in NSMenu")
                return

            W, H = 240, 30
            view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))

            lbl = NSTextField.labelWithString_("Auto-paste")
            lbl.setFrame_(NSMakeRect(14, 6, 150, 18))
            lbl.setFont_(NSFont.menuFontOfSize_(13.0))
            view.addSubview_(lbl)

            sw = NSSwitch.alloc().initWithFrame_(NSMakeRect(168, 4, 51, 22))
            sw.setState_(1 if self.enabled else 0)

            tgt = _SwitchTarget.alloc().init_with_callback(self._on_switch_toggled)
            sw.setTarget_(tgt)
            sw.setAction_(objc.selector(tgt.toggled_, signature=b'v@:@'))
            view.addSubview_(sw)

            item.setView_(view)

            # Keep ObjC objects alive (Python GC would free them otherwise)
            self._switch_refs = tgt
            self._nsswitch    = sw
            log("NSSwitch toggle attached ✅")

        except Exception:
            log(f"NSSwitch setup failed (checkmark fallback):\n{traceback.format_exc()}")
            self.toggle_item.state = 1 if self.enabled else 0

    # ── Auto-discover on startup ───────────────────────────────────────────────

    def _auto_discover(self):
        """Scan all Chrome tabs on startup and remember the best AI tab found."""
        time.sleep(1.5)  # let the app finish initialising
        tabs = scan_all_ai_tabs()
        if not tabs:
            log("Startup scan: no AI tabs found in Chrome")
            return
        # Prefer an active tab, otherwise take the first one found
        active = [t for t in tabs if t["active"]]
        best   = active[0] if active else tabs[0]
        self._last_tab     = (best["window"], best["tab"])
        self._last_service = service_name(best["url"])
        log(f"Startup scan: auto-discovered {self._last_service} "
            f"(window {best['window']}, tab {best['tab']}) — '{best['title'][:60]}'")
        self._update_pin_label()

    # ── Toggle ────────────────────────────────────────────────────────────────

    def _on_switch_toggled(self, is_on: bool):
        """Called by the NSSwitch when the user flips it."""
        self._apply_toggle(is_on)

    def toggle(self, sender):
        """Called when the menu item row is clicked (fallback / keyboard nav)."""
        new_state = not self.enabled
        # Keep the NSSwitch in sync if it exists
        if self._nsswitch is not None:
            self._nsswitch.setState_(1 if new_state else 0)
        self._apply_toggle(new_state)
        # Update checkmark fallback
        if self._nsswitch is None:
            sender.state = 1 if new_state else 0

    def _apply_toggle(self, is_on: bool):
        self.enabled = is_on
        self.title   = "📸" if is_on else "📸✕"
        if is_on:
            self._start_watcher()
            log("Auto-paste enabled")
        else:
            self._stop_watcher()
            log("Auto-paste disabled")

    # ── Set target tab ────────────────────────────────────────────────────────

    def set_target(self, _):
        """
        Smart one-click target setter:
          • If the currently active Chrome tab IS an AI tab → pin it immediately.
          • Otherwise → show a numbered picker of all open AI tabs.
        """
        active = find_active_ai_tab()

        if active:
            # The user is already in an AI tab — pin it without any dialog
            url   = get_tab_url(active[0], active[1])
            title = get_tab_title(active[0], active[1])
            svc   = service_name(url)
            self._pin(active, svc, title)
            return

        # Not in an AI tab — show a picker
        tabs = scan_all_ai_tabs()
        if not tabs:
            self._notify("No AI tabs open ⚠️", "Open Claude.ai or ChatGPT in Chrome first.")
            return

        lines = []
        for i, t in enumerate(tabs, 1):
            svc         = service_name(t["url"])
            title_short = t["title"][:52] + "…" if len(t["title"]) > 52 else t["title"]
            marker      = "  ◀ active" if t["active"] else ""
            lines.append(f"{i}.  [{svc}]  {title_short}{marker}")

        choices = "\\n".join(lines)
        script  = (
            f'set answer to display dialog "Switch to an AI tab and click Set target tab — or pick one below:\\n\\n{choices}" '
            f'with title "Screenshot to AI" '
            f'default answer "1" '
            f'buttons {{"Cancel", "Pin"}} '
            f'default button "Pin"\n'
            f'return text returned of answer'
        )
        out, err = run_applescript(script)
        if err or not out:
            return
        try:
            chosen = tabs[int(out.strip()) - 1]
        except (ValueError, IndexError):
            self._notify("Invalid choice", f"Enter 1–{len(tabs)}.")
            return

        svc = service_name(chosen["url"])
        self._pin((chosen["window"], chosen["tab"]), svc, chosen["title"])

    def _pin(self, tab: Tuple[int, int], svc: str, title: str):
        self._pinned_tab     = tab
        self._pinned_service = svc
        self._last_tab       = tab
        self._last_service   = svc
        self._update_pin_label()
        log(f"Pinned: {svc} — window {tab[0]}, tab {tab[1]} — '{title[:60]}'")
        self._notify(f"Pinned to {svc} 📌", f"→ {title[:60]}")

    def clear_pin(self, _):
        """Click the pin label to clear the explicit pin and return to auto-detect."""
        if not self._pinned_tab:
            return
        log(f"Pin cleared (was {self._pinned_service})")
        self._pinned_tab     = None
        self._pinned_service = ""
        self._update_pin_label()
        self._notify("Pin cleared", "Back to auto-detect mode.")

    def _update_pin_label(self):
        if self._pinned_tab:
            self.pin_item.title = f"   📌 {self._pinned_service} pinned  (click to clear)"
        elif self._last_tab:
            self.pin_item.title = f"   🔍 Auto: {self._last_service} (no pin)"
        else:
            self.pin_item.title = "   No target set — auto-detect"

    # ── Manual retry ──────────────────────────────────────────────────────────

    def paste_last(self, _):
        candidates = []
        for d in get_screenshot_dirs():
            for pattern in ("Screenshot*.png", "Screenshot*.jpg", "Screenshot*.jpeg"):
                candidates += [
                    f for f in glob.glob(os.path.join(d, pattern))
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
        threading.Thread(target=self._paste_screenshot, args=(filepath,), daemon=True).start()

    def _paste_screenshot(self, filepath: str):
        filename = os.path.basename(filepath)
        log(f"Processing: {filename}")

        # Priority 1 — explicitly pinned tab
        tab, used_pin, used_fallback = None, False, False

        if self._pinned_tab and verify_tab(*self._pinned_tab):
            tab      = self._pinned_tab
            used_pin = True
            log(f"  Using pinned tab: {self._pinned_service}")

        # Priority 2 — currently active AI tab in Chrome
        if tab is None:
            tab = find_active_ai_tab()
            if tab:
                log(f"  Using active AI tab: window {tab[0]}, tab {tab[1]}")

        # Priority 3 — last successfully used tab (e.g. screenshotting from Notability)
        if tab is None and self._last_tab and verify_tab(*self._last_tab):
            tab           = self._last_tab
            used_fallback = True
            log(f"  No active AI tab — using last used: {self._last_service}")

        if tab is None:
            log("  ❌ No AI tab found")
            self._notify(
                "No AI tab found ⚠️",
                "Open Claude.ai or ChatGPT in Chrome, or use 🎯 Choose target tab."
            )
            self._set_status("⚠️  No AI tab found")
            return

        url  = get_tab_url(tab[0], tab[1])
        svc  = service_name(url)
        log(f"  Target: {svc} (window {tab[0]}, tab {tab[1]})")

        try:
            if activate_tab_and_paste(tab[0], tab[1], filepath):
                # Update memory (but don't overwrite an explicit pin)
                if not used_pin:
                    self._last_tab     = tab
                    self._last_service = svc
                    self._update_pin_label()

                if used_pin:
                    label = f"{svc} 📌"
                elif used_fallback:
                    label = f"{svc} (remembered)"
                else:
                    label = svc

                self._notify(f"Pasted to {label} ✅", filename)
                self._set_status(f"✅  {filename} → {label}")
                log(f"✅ Done → {label}")
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
