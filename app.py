#!/usr/bin/env python3
"""
screenshot-to-ai  —  macOS menubar app
Watches your Screenshots folder and auto-pastes new screenshots
into your open Claude.ai or ChatGPT Chrome tab.
"""

import os
import sys
import time
import threading
import subprocess
import glob
import json
import ctypes
from typing import Optional, Tuple, List, Dict

LOG_PATH = os.path.expanduser("~/Library/Logs/screenshot-to-ai.log")
APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/screenshot-to-ai")
SETUP_STATE_PATH = os.path.join(APP_SUPPORT_DIR, "setup.json")


def _prelaunch_hide_dock_icon():
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyProhibited
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyProhibited
        )
        return True
    except Exception:
        return False


if __name__ == "__main__":
    _prelaunch_hide_dock_icon()

import rumps

# watchdog is only used as a fallback if Spotlight is unavailable
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False

# ── NSSwitch toggle (macOS 10.15+) ─────────────────────────────────────────────
# Embeds a real iOS-style toggle switch directly inside a menu item view.
# Falls back to a plain checkmark if PyObjC / NSSwitch is unavailable.

try:
    import objc
    from Foundation import NSObject
    from AppKit import (
        NSMenuItem, NSView, NSSwitch, NSTextField,
        NSFont, NSColor, NSMakeRect, NSAppearance,
        NSApplication, NSApplicationActivationPolicyAccessory,
        NSApplicationActivationPolicyProhibited,
        NSRunningApplication,
        NSAlert, NSWindow, NSButton, NSBox,
        NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
        NSBackingStoreBuffered, NSLineBreakByWordWrapping,
    )
    _NSSWITCH_AVAILABLE = True
except Exception:
    _NSSWITCH_AVAILABLE = False

try:
    from Quartz import (
        CGEventCreateKeyboardEvent,
        CGEventPost,
        CGEventPostToPid,
        CGEventSetFlags,
        kCGAnnotatedSessionEventTap,
        kCGEventFlagMaskCommand,
    )
    _QUARTZ_EVENTS_AVAILABLE = True
except Exception:
    _QUARTZ_EVENTS_AVAILABLE = False

try:
    from PyObjCTools import AppHelper
    _APPHELPER_AVAILABLE = True
except Exception:
    _APPHELPER_AVAILABLE = False


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


    class _CallbackTarget(NSObject):
        @objc.python_method
        def init_with_callback(self, callback):
            self = self.init()
            self._cb = callback
            return self

        def triggered_(self, sender):
            self._cb(sender)


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

SCREENSHOT_EXTS  = {".png", ".jpg", ".jpeg", ".tiff", ".tif"}
DEBOUNCE_SECONDS = 2.0

# ── Spotlight-based screenshot watcher ─────────────────────────────────────────
# Uses `mdfind kMDItemIsScreenCapture == 1` which is set by macOS on every
# screenshot regardless of save location. Requires NO special permissions —
# no Full Disk Access, no Desktop/Documents approval. Polls every second.

class SpotlightScreenshotWatcher:
    """Primary watcher: polls mdfind every second. No permissions needed."""

    POLL_INTERVAL = 1.0

    def __init__(self, callback):
        self._callback = callback
        self._seen:   set                        = set()
        self._stop                               = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── public interface (mirrors watchdog Observer) ──────────────────────────

    def start(self):
        initial = self._mdfind()
        self._seen.update(initial)
        log(f"Spotlight watcher: seeded with {len(initial)} existing screenshot(s)")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="spotlight-watcher"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, timeout: float = 2.0):
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── internals ─────────────────────────────────────────────────────────────

    def _mdfind(self) -> set:
        try:
            r = run_command(
                ["mdfind", "-onlyin", os.path.expanduser("~"),
                 "kMDItemIsScreenCapture == 1"],
                timeout=3
            )
            return {p.strip() for p in r.stdout.strip().split("\n") if p.strip()}
        except Exception:
            return set()

    def _loop(self):
        log("Spotlight watcher active — no Full Disk Access required")
        while not self._stop.wait(self.POLL_INTERVAL):
            try:
                current   = self._mdfind()
                new_paths = current - self._seen
                self._seen.update(new_paths)
                for path in sorted(new_paths):
                    if not is_real_screenshot(path):
                        continue
                    try:
                        age = time.time() - os.path.getmtime(path)
                    except OSError:
                        continue
                    if age < 15:   # only fire for screenshots taken < 15 s ago
                        log(f"📸 Spotlight detected: {os.path.basename(path)}")
                        self._callback(path)
            except Exception as e:
                log(f"Spotlight watcher error: {e}")

# ── Helpers ────────────────────────────────────────────────────────────────────

def log(msg: str):
    line = f"[screenshot-to-ai] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_command(args: List[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def run_applescript(script: str) -> Tuple[str, str]:
    result = run_command(["osascript", "-e", script])
    return result.stdout.strip(), result.stderr.strip()


def run_jxa(script: str) -> Tuple[str, str]:
    """Run a JXA (JavaScript for Automation) script via osascript -l JavaScript."""
    result = run_command(["osascript", "-l", "JavaScript", "-e", script])
    return result.stdout.strip(), result.stderr.strip()


def get_screenshot_dirs() -> List[str]:
    dirs = []
    r = run_command(["defaults", "read", "com.apple.screencapture", "location"])
    if r.returncode == 0 and r.stdout.strip():
        custom = os.path.expanduser(r.stdout.strip())
        if os.path.isdir(custom):
            dirs.append(custom)
    for fallback in ["~/Desktop", "~/Pictures/Screenshots"]:
        path = os.path.expanduser(fallback)
        if os.path.isdir(path) and path not in dirs:
            dirs.append(path)
    return dirs


def can_access_directory(path: str) -> bool:
    try:
        with os.scandir(path) as it:
            next(it, None)
        return True
    except PermissionError:
        return False
    except FileNotFoundError:
        return False
    except Exception:
        return True


def is_real_screenshot(path: str) -> bool:
    name = os.path.basename(path)
    if name.startswith("."):
        return False
    _, ext = os.path.splitext(name)
    return ext.lower() in SCREENSHOT_EXTS


def is_accessibility_trusted(prompt: bool = False) -> bool:
    try:
        app_services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        if not prompt:
            app_services.AXIsProcessTrusted.restype = ctypes.c_bool
            return bool(app_services.AXIsProcessTrusted())

        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )

        app_services.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        app_services.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool

        core_foundation.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        core_foundation.CFDictionaryCreate.restype = ctypes.c_void_p
        core_foundation.CFRelease.argtypes = [ctypes.c_void_p]

        key = ctypes.c_void_p.in_dll(app_services, "kAXTrustedCheckOptionPrompt")
        value = ctypes.c_void_p.in_dll(core_foundation, "kCFBooleanTrue")
        options = core_foundation.CFDictionaryCreate(
            None,
            (ctypes.c_void_p * 1)(key.value),
            (ctypes.c_void_p * 1)(value.value),
            1,
            None,
            None,
        )
        try:
            return bool(app_services.AXIsProcessTrustedWithOptions(options))
        finally:
            if options:
                core_foundation.CFRelease(options)
    except Exception as e:
        log(f"Accessibility trust check failed: {e}")
        return False


def open_accessibility_settings():
    subprocess.run(
        [
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ],
        capture_output=True,
    )


def suppress_dock_icon():
    try:
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyProhibited
        )
    except Exception:
        try:
            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory
            )
        except Exception:
            pass


# ── Multi-browser support ───────────────────────────────────────────────────────
# All Chromium-based browsers expose the same AppleScript/JXA interface.
# Each entry: (AppleScript app name, bundle ID, pgrep process name)

BROWSERS = [
    ("Google Chrome",          "com.google.Chrome",               "Google Chrome"),
    ("Arc",                    "company.thebrowser.Browser",       "Arc"),
    ("Brave Browser",          "com.brave.Browser",                "Brave Browser"),
    ("Microsoft Edge",         "com.microsoft.edgemac",            "Microsoft Edge"),
]


def get_running_browser() -> Optional[str]:
    """Return the AppleScript app name of the first supported browser that is running."""
    for app_name, bundle_id, pgrep_name in BROWSERS:
        if _NSSWITCH_AVAILABLE:
            try:
                apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
                if apps:
                    return app_name
                continue
            except Exception:
                pass
        result = subprocess.run(["pgrep", "-x", pgrep_name], capture_output=True, text=True)
        if result.returncode == 0:
            return app_name
    return None


def is_chrome_running() -> bool:
    return get_running_browser() is not None


def get_browser_pid(app_name: Optional[str] = None) -> Optional[int]:
    if not _NSSWITCH_AVAILABLE:
        return None
    if app_name is None:
        app_name = get_running_browser()
    if app_name is None:
        return None
    bundle_id = next((b for a, b, _ in BROWSERS if a == app_name), None)
    if bundle_id is None:
        return None
    try:
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
        if not apps:
            return None
        frontmost = [app for app in apps if app.isActive()]
        target = frontmost[0] if frontmost else apps[0]
        return int(target.processIdentifier())
    except Exception as e:
        log(f"  Browser PID lookup failed: {e}")
        return None


def get_chrome_pid() -> Optional[int]:
    return get_browser_pid()


def send_native_cmd_v(target_pid: Optional[int] = None) -> bool:
    if not _QUARTZ_EVENTS_AVAILABLE:
        log("  Native Cmd+V unavailable: Quartz events not available")
        return False
    try:
        # Real key chord: Command down -> V down -> V up -> Command up.
        # Chrome appears to ignore a single flagged V event in this flow.
        cmd_down = CGEventCreateKeyboardEvent(None, 55, True)
        key_down = CGEventCreateKeyboardEvent(None, 9, True)
        key_up = CGEventCreateKeyboardEvent(None, 9, False)
        cmd_up = CGEventCreateKeyboardEvent(None, 55, False)
        CGEventSetFlags(key_down, kCGEventFlagMaskCommand)
        CGEventSetFlags(key_up, kCGEventFlagMaskCommand)
        if target_pid:
            CGEventPostToPid(target_pid, cmd_down)
            time.sleep(0.01)
            CGEventPostToPid(target_pid, key_down)
            time.sleep(0.02)
            CGEventPostToPid(target_pid, key_up)
            time.sleep(0.01)
            CGEventPostToPid(target_pid, cmd_up)
        else:
            CGEventPost(kCGAnnotatedSessionEventTap, cmd_down)
            time.sleep(0.01)
            CGEventPost(kCGAnnotatedSessionEventTap, key_down)
            time.sleep(0.02)
            CGEventPost(kCGAnnotatedSessionEventTap, key_up)
            time.sleep(0.01)
            CGEventPost(kCGAnnotatedSessionEventTap, cmd_up)
        log(f"  Step 4: native Cmd+V sent{' to pid ' + str(target_pid) if target_pid else ''}")
        return True
    except Exception as e:
        log(f"  Native Cmd+V failed: {e}")
        return False


# ── Browser tab helpers ────────────────────────────────────────────────────────

def get_tab_url(window_idx: int, tab_idx: int, browser: Optional[str] = None) -> str:
    browser = browser or get_running_browser() or "Google Chrome"
    out, _ = run_applescript(f"""
    tell application "{browser}"
        return URL of tab {tab_idx} of window {window_idx}
    end tell
    """)
    return out.lower()


def get_tab_title(window_idx: int, tab_idx: int, browser: Optional[str] = None) -> str:
    browser = browser or get_running_browser() or "Google Chrome"
    out, _ = run_applescript(f"""
    tell application "{browser}"
        return title of tab {tab_idx} of window {window_idx}
    end tell
    """)
    return out


AI_DOMAINS = [
    "claude.ai",
    "chat.openai.com",
    "chatgpt.com",
    "gemini.google.com",
    "perplexity.ai",
    "grok.com",
    "x.com/i/grok",
]


def is_ai_url(url: str) -> bool:
    return any(d in url for d in AI_DOMAINS)


def scan_all_ai_tabs() -> List[Dict]:
    """
    Scan every tab in every browser window and return a list of AI tabs.
    Each entry: {"window": int, "tab": int, "url": str, "title": str, "active": bool, "browser": str}
    Uses 'output' not 'result' — 'result' is a reserved AppleScript keyword.
    """
    browser = get_running_browser()
    if not browser:
        return []
    out, err = run_applescript(f"""
    tell application "{browser}"
        set output to ""
        set winIdx to 0
        repeat with w in windows
            set winIdx to winIdx + 1
            set activeIdx to active tab index of w
            set tabIdx to 0
            repeat with t in tabs of w
                set tabIdx to tabIdx + 1
                set u to URL of t
                if u contains "claude.ai" or u contains "chat.openai.com" or u contains "chatgpt.com" or u contains "gemini.google.com" or u contains "perplexity.ai" or u contains "grok.com" then
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
                    "window":  int(parts[0]),
                    "tab":     int(parts[1]),
                    "url":     parts[2].lower(),
                    "title":   parts[3],
                    "active":  parts[4].strip().lower() == "true",
                    "browser": browser,
                })
            except ValueError:
                continue
    log(f"  scan found {len(tabs)} AI tab(s) in {browser}")
    return tabs


def find_active_ai_tab() -> Optional[Tuple[int, int]]:
    """Only checks the currently visible tab of each window (front-to-back)."""
    browser = get_running_browser()
    if not browser:
        return None
    out, err = run_applescript(f"""
    tell application "{browser}"
        set winIdx to 0
        repeat with w in windows
            set winIdx to winIdx + 1
            set activeIdx to active tab index of w
            set t to active tab of w
            set u to URL of t
            if u contains "claude.ai" or u contains "chat.openai.com" or u contains "chatgpt.com" or u contains "gemini.google.com" or u contains "perplexity.ai" or u contains "grok.com" then
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
    if "claude.ai" in url:
        return "Claude"
    if "gemini.google.com" in url:
        return "Gemini"
    if "perplexity.ai" in url:
        return "Perplexity"
    if "grok.com" in url or "x.com/i/grok" in url:
        return "Grok"
    return "ChatGPT"


# ── Clipboard & paste ──────────────────────────────────────────────────────────

def copy_image_to_clipboard(filepath: str) -> bool:
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".png":
        filetype = "«class PNGf»"
    elif ext in (".tiff", ".tif"):
        filetype = "TIFF picture"
    else:
        filetype = "JPEG picture"
    # Escape any quotes in the path so AppleScript doesn't break
    safe_path = filepath.replace("\\", "\\\\").replace('"', '\\"')
    _, err = run_applescript(
        f'set the clipboard to (read (POSIX file "{safe_path}") as {filetype})'
    )
    if err:
        log(f"  Clipboard error: {err}")
        return False
    return True


def try_dom_clipboard_upload(window_idx: int, tab_idx: int, browser: Optional[str] = None) -> Tuple[bool, str]:
    js = (
        "(async function(){"
        "function targetEl(){"
        "return document.getElementById('prompt-textarea')"
        "|| document.querySelector('.ProseMirror')"
        "|| document.querySelector('[contenteditable=\"true\"]')"
        "|| document.querySelector('textarea');"
        "}"
        "function fileInput(){"
        "return document.querySelector('input[type=\"file\"]');"
        "}"
        "try{"
        "if(!navigator.clipboard||!navigator.clipboard.read){return 'clipboard-api-unavailable';}"
        "const items=await navigator.clipboard.read();"
        "let blob=null;"
        "for(const item of items){"
        "const imgType=item.types.find(t=>t.startsWith('image/'));"
        "if(imgType){blob=await item.getType(imgType);break;}"
        "}"
        "if(!blob){return 'no-image-in-clipboard';}"
        "const name=(blob.type==='image/jpeg')?'screenshot.jpg':'screenshot.png';"
        "const file=new File([blob], name, {type: blob.type || 'image/png'});"
        "const input=fileInput();"
        "if(input){"
        "const dt=new DataTransfer();"
        "dt.items.add(file);"
        "input.files=dt.files;"
        "input.dispatchEvent(new Event('input',{bubbles:true}));"
        "input.dispatchEvent(new Event('change',{bubbles:true}));"
        "return 'uploaded:file-input';"
        "}"
        "const el=targetEl();"
        "if(!el){return 'no-target-element';}"
        "const dt=new DataTransfer();"
        "dt.items.add(file);"
        "const evt=new ClipboardEvent('paste',{clipboardData: dt, bubbles:true, cancelable:true});"
        "el.dispatchEvent(evt);"
        "return 'uploaded:paste-event';"
        "}catch(e){return 'error:'+String(e&&e.message||e);}"
        "})()"
    )
    js_literal = json.dumps(js)
    browser = browser or get_running_browser() or "Google Chrome"
    jxa = (
        f"var chrome=Application('{browser}');"
        f"var tab=chrome.windows[{window_idx-1}].tabs[{tab_idx-1}];"
        f"tab.execute({{javascript:{js_literal}}});"
    )
    out, err = run_jxa(jxa)
    result = out or err or "no output"
    ok = result.startswith("uploaded:")
    return ok, result


def activate_tab_and_paste(window_idx: int, tab_idx: int, filepath: str) -> bool:
    browser = get_running_browser() or "Google Chrome"

    log("  Step 1: Copying image to clipboard...")
    if not copy_image_to_clipboard(filepath):
        return False
    log("  Step 1: ✅ clipboard set")

    log(f"  Step 2: Activating {browser} tab...")
    _, err = run_applescript(f"""
    tell application "{browser}"
        set index of window {window_idx} to 1
        set active tab index of window {window_idx} to {tab_idx}
        activate
    end tell
    """)
    if err:
        log(f"  Step 2 warning: {err}")
    # Poll until the browser is frontmost (max 2 s) rather than sleeping blindly
    deadline = time.time() + 2.0
    while time.time() < deadline:
        check, _ = run_applescript(f'tell application "{browser}" to return (active of front window) as string')
        if check.strip() == "true":
            break
        time.sleep(0.1)
    log(f"  Step 2: ✅ {browser} activated")

    log("  Step 3: Focusing input via JavaScript...")
    # Use JXA (JavaScript for Automation) + json.dumps so ALL special chars
    # are escaped correctly — no AppleScript string-quoting issues possible.
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
    js_literal = json.dumps(js)  # produces a properly-escaped JS/JSON string literal
    # JXA: windows[] and tabs[] are 0-based, AppleScript indices are 1-based
    jxa = (
        f"var chrome=Application('{browser}');"
        f"var tab=chrome.windows[{window_idx-1}].tabs[{tab_idx-1}];"
        f"tab.execute({{javascript:{js_literal}}});"
    )
    js_result, js_err = run_jxa(jxa)
    log(f"  Step 3 JS: {js_result or js_err or 'no output'}")

    if "INPUT NOT FOUND" in (js_result or "") or not js_result:
        log("  Step 3b: JS focus failed — no coordinate fallback")

    time.sleep(0.3)

    log("  Step 3c: Trying DOM clipboard upload...")
    dom_ok, dom_result = try_dom_clipboard_upload(window_idx, tab_idx, browser)
    log(f"  Step 3c DOM: {dom_result}")
    if dom_ok:
        log("  Step 3c: ✅ DOM upload triggered")
        return True

    log("  Step 4: Sending Cmd+V...")
    browser_pid = get_browser_pid(browser)
    if not send_native_cmd_v(browser_pid):
        log("  Step 4 error: native Cmd+V failed")
        return False
    log("  Step 4: ✅ paste sent")
    return True


# ── File watcher ───────────────────────────────────────────────────────────────

class ScreenshotHandler(FileSystemEventHandler):
    def __init__(self, app: "ScreenshotToAIApp"):
        super().__init__()
        self.app = app
        self._last_fired: float = 0
        self._last_path:  str   = ""

    def on_created(self, event):
        if not event.is_directory and is_real_screenshot(event.src_path):
            self._trigger(event.src_path, "created")

    def on_moved(self, event):
        # macOS renames the hidden .Screenshot*.png temp file to the final name.
        # We only care about the destination and only if it's a real screenshot.
        if not event.is_directory and is_real_screenshot(event.dest_path):
            self._trigger(event.dest_path, "moved")

    def on_deleted(self, event):
        # If the user manually drags the screenshot away from the watch folder,
        # watchdog fires on_deleted on the source AND on_created on the dest
        # (if the dest folder is also watched). We just log it here so the
        # subsequent processing attempt can gracefully handle the missing file.
        if not event.is_directory and is_real_screenshot(event.src_path):
            log(f"📸 Deleted/moved away: {os.path.basename(event.src_path)}")

    def _trigger(self, path: str, reason: str):
        now = time.time()
        # Deduplicate: same path within debounce window = ignore
        if path == self._last_path and now - self._last_fired < DEBOUNCE_SECONDS:
            log(f"  Debounced duplicate ({reason}): {os.path.basename(path)}")
            return
        # Different path but still within debounce window: allow (rapid back-to-back shots)
        if path != self._last_path and now - self._last_fired < DEBOUNCE_SECONDS:
            log(f"  New file during debounce window — processing ({reason}): {os.path.basename(path)}")
        self._last_fired = now
        self._last_path  = path
        log(f"📸 Detected ({reason}): {os.path.basename(path)}")
        time.sleep(1.0)
        self.app.handle_new_screenshot(path)


# ── Menubar app ────────────────────────────────────────────────────────────────

class ScreenshotToAIApp(rumps.App):
    def __init__(self):
        super().__init__(name="ScreenshotToAI", title="📸", quit_button="Quit")
        self.enabled  = False
        self._watcher = None   # SpotlightScreenshotWatcher or watchdog Observer
        self._setup_state = self._load_setup_state()

        # Pinned target: set explicitly via "Choose Target" menu.
        # When set, ALL screenshots go here regardless of active tab.
        self._pinned_tab:     Optional[Tuple[int, int]] = None
        self._pinned_service: str = ""

        # Fallback memory: last tab we successfully pasted to.
        self._last_tab:     Optional[Tuple[int, int]] = None
        self._last_service: str = ""

        # ── Menu items ────────────────────────────────────────────────────────
        self.menu_title_item = rumps.MenuItem("Screenshot to AI")
        self.menu_title_item.set_callback(None)
        self.menu_subtitle_item = rumps.MenuItem("Drop screenshots into ChatGPT")
        self.menu_subtitle_item.set_callback(None)
        self.toggle_item = rumps.MenuItem("Auto-paste", callback=self.toggle)
        self.target_item = rumps.MenuItem("Choose Target Tab", callback=self.set_target)
        self.pin_item    = rumps.MenuItem("Target: Auto-detect", callback=self.clear_pin)
        self.test_item   = rumps.MenuItem("Paste Latest Screenshot", callback=self.paste_last)
        self.status_item = rumps.MenuItem("Last: —")
        self.status_item.set_callback(None)
        self.setup_item = rumps.MenuItem("Open Setup", callback=self.show_setup_window)
        self.login_item  = rumps.MenuItem("Launch at Login", callback=self.toggle_login_item)
        self.login_item.state = 1 if self._is_agent_installed() else 0

        self.menu = [
            self.menu_title_item,
            self.menu_subtitle_item,
            None,
            self.toggle_item,
            None,
            self.target_item,
            self.pin_item,
            None,
            self.test_item,
            None,
            self.setup_item,
            self.status_item,
            None,
            self.login_item,
        ]

        # NSSwitch is attached via a short timer AFTER the app finishes launching.
        # Setting the view during __init__ is too early — the NSMenu isn't
        # fully wired up yet and the view gets dropped silently.
        self._switch_refs  = None
        self._nsswitch     = None
        self._toggle_badge = None
        self._setup_window = None
        self._setup_label = None
        self._setup_hint_label = None
        self._setup_title_label = None
        self._setup_row_labels = []
        self._setup_row_title_labels = []
        self._setup_row_detail_labels = []
        self._setup_row_views = []
        self._menu_header_view = None
        self._menu_header_subtitle = None
        self._menu_header_badge = None
        self._setup_button_targets = []
        self._setup_buttons = {}
        self.toggle_item.state = 0   # checkmark fallback until NSSwitch attaches

        if _NSSWITCH_AVAILABLE:
            rumps.Timer(self._deferred_attach_switch, 0.4).start()

        self._refresh_setup_state(check_permissions=False)
        self.enabled = False
        self.title = "📸✕"
        if self._setup_completed() and self._setup_state.get("ready"):
            self._set_status("Ready")
        else:
            self._set_status("Setup required")
            rumps.Timer(self._deferred_first_run_prompt, 0.8).start()

        # Auto-discover an AI tab on startup so first screenshot works immediately
        threading.Thread(target=self._auto_discover, daemon=True).start()

    # ── Deferred NSSwitch setup ───────────────────────────────────────────────

    def _deferred_attach_switch(self, timer):
        """
        Called ~0.4 s after launch. Builds a polished toggle row:
          [Auto-paste label]   [ON/OFF colored badge]   [NSSwitch]

        Strategy: get the real NSMenu, find the rumps 'Auto-paste' item by
        index, create a FRESH NSMenuItem (pure ObjC, no rumps wrapper), give
        it a custom view containing an NSSwitch, and splice it in at the same
        position. This sidesteps all rumps wrapper / setView_ limitations.
        """
        import traceback
        timer.stop()
        try:
            # ── Locate the real NSMenu ─────────────────────────────────────────
            ns_menu = None
            for getter in [
                lambda: self._status_item.menu(),
                lambda: self._menu._menu,
            ]:
                try:
                    m = getter()
                    if m is not None and hasattr(m, 'indexOfItemWithTitle_'):
                        ns_menu = m
                        break
                except Exception:
                    pass

            if ns_menu is None:
                log("NSSwitch: NSMenu not found — checkmark fallback")
                self.toggle_item.state = 1 if self.enabled else 0
                return

            self._attach_menu_header()

            idx = ns_menu.indexOfItemWithTitle_("Auto-paste")
            if idx == -1:
                log("NSSwitch: 'Auto-paste' item not found in NSMenu")
                self.toggle_item.state = 1 if self.enabled else 0
                return

            log(f"NSSwitch: found 'Auto-paste' at NSMenu index {idx}")

            # ── Build the custom view ──────────────────────────────────────────
            W, H = 260, 38
            view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))

            # Main "Auto-paste" label
            lbl = NSTextField.labelWithString_("Auto-paste")
            lbl.setFrame_(NSMakeRect(16, 12, 126, 16))
            lbl.setFont_(NSFont.menuFontOfSize_(13.0))
            view.addSubview_(lbl)

            # Colored ON / OFF badge — green when on, grey when off
            badge_text  = "ON"  if self.enabled else "OFF"
            badge_color = NSColor.systemGreenColor() if self.enabled else NSColor.secondaryLabelColor()
            badge = NSTextField.labelWithString_(badge_text)
            badge.setFrame_(NSMakeRect(146, 13, 34, 13))
            badge.setFont_(NSFont.boldSystemFontOfSize_(10.0))
            badge.setTextColor_(badge_color)
            view.addSubview_(badge)

            # NSSwitch — forced to Aqua so the blue ON colour is vivid in dark mode
            sw = NSSwitch.alloc().initWithFrame_(NSMakeRect(188, 8, 51, 22))
            try:
                sw.setAppearance_(
                    NSAppearance.appearanceNamed_("NSAppearanceNameAqua")
                )
            except Exception:
                pass

            tgt = _SwitchTarget.alloc().init_with_callback(self._on_switch_toggled)
            sw.setTarget_(tgt)
            sw.setAction_(objc.selector(tgt.toggled_, signature=b'v@:@'))
            view.addSubview_(sw)

            # ── Splice in a fresh native NSMenuItem ───────────────────────────
            # We create a brand-new ObjC NSMenuItem (not a rumps wrapper) so we
            # can call setView_() on it without hitting rumps' Python layer.
            new_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Auto-paste", None, ""
            )
            new_item.setView_(view)

            # Set switch state AFTER the item is fully built
            sw.setState_(1 if self.enabled else 0)
            sw.setNeedsDisplay_(True)

            # Replace the old rumps item at the same slot
            ns_menu.removeItemAtIndex_(idx)
            ns_menu.insertItem_atIndex_(new_item, idx)

            # Keep ObjC objects alive (Python GC would free them otherwise)
            self._switch_refs  = tgt
            self._nsswitch     = sw
            self._toggle_badge = badge
            self._toggle_ns_item = new_item
            log("NSSwitch toggle attached ✅")

        except Exception:
            log(f"NSSwitch setup failed (checkmark fallback):\n{traceback.format_exc()}")
            self.toggle_item.state = 1 if self.enabled else 0

    def _update_toggle_badge(self):
        """Refresh the green ON / grey OFF badge and force-redraw the switch."""
        badge = getattr(self, "_toggle_badge", None)
        if badge is not None:
            if self.enabled:
                badge.setStringValue_("ON")
                badge.setTextColor_(NSColor.systemGreenColor())
            else:
                badge.setStringValue_("OFF")
                badge.setTextColor_(NSColor.secondaryLabelColor())
        # Force the NSSwitch to repaint so the blue/grey colour updates promptly
        sw = getattr(self, "_nsswitch", None)
        if sw is not None:
            sw.setNeedsDisplay_(True)
        subtitle = getattr(self, "_menu_header_subtitle", None)
        if subtitle is not None:
            subtitle.setStringValue_(self._menu_header_subtitle_text())
        self._refresh_menu_header_badge()

    def _menu_header_subtitle_text(self) -> str:
        if self.enabled:
            return "Watching for new screenshots"
        if not self._setup_completed():
            return "Finish setup, then turn on Auto-paste"
        return "Ready when you want to enable it"

    def _menu_header_badge_text(self) -> str:
        if self.enabled:
            return "LIVE"
        if not self._setup_completed():
            return "SETUP"
        return "READY"

    def _refresh_menu_header_badge(self):
        badge = getattr(self, "_menu_header_badge", None)
        if badge is None:
            return
        badge.setStringValue_(self._menu_header_badge_text())
        if self.enabled:
            badge.setTextColor_(NSColor.systemGreenColor())
        elif not self._setup_completed():
            badge.setTextColor_(NSColor.systemOrangeColor())
        else:
            badge.setTextColor_(NSColor.systemBlueColor())

    def _attach_menu_header(self):
        if not _NSSWITCH_AVAILABLE:
            return
        try:
            view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 62))

            title = NSTextField.labelWithString_("Screenshot to AI")
            title.setFrame_(NSMakeRect(14, 31, 180, 18))
            title.setFont_(NSFont.boldSystemFontOfSize_(15.0))
            view.addSubview_(title)

            subtitle = NSTextField.labelWithString_(self._menu_header_subtitle_text())
            subtitle.setFrame_(NSMakeRect(14, 13, 190, 14))
            subtitle.setFont_(NSFont.systemFontOfSize_(11.0))
            subtitle.setTextColor_(NSColor.secondaryLabelColor())
            view.addSubview_(subtitle)

            badge = NSTextField.labelWithString_(self._menu_header_badge_text())
            badge.setFrame_(NSMakeRect(204, 27, 44, 16))
            badge.setAlignment_(2)
            badge.setFont_(NSFont.boldSystemFontOfSize_(10.0))
            view.addSubview_(badge)

            self.menu_title_item.setView_(view)
            self.menu_subtitle_item.setHidden_(True)
            self._menu_header_view = view
            self._menu_header_subtitle = subtitle
            self._menu_header_badge = badge
            self._refresh_menu_header_badge()
        except Exception:
            pass

    def _load_setup_state(self) -> Dict[str, object]:
        try:
            with open(SETUP_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"Setup state load failed: {e}")
        return {}

    def _save_setup_state(self):
        try:
            os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
            with open(SETUP_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._setup_state, f)
        except Exception as e:
            log(f"Setup state save failed: {e}")

    def _required_dirs(self) -> List[str]:
        return get_screenshot_dirs()

    def _missing_setup_steps(self) -> List[str]:
        missing = []
        required_dirs = self._required_dirs()
        blocked_dirs = [d for d in required_dirs if not can_access_directory(d)]
        if blocked_dirs:
            missing.append(os.path.basename(blocked_dirs[0]) or blocked_dirs[0])
        return missing

    def _setup_ready(self) -> bool:
        return not self._missing_setup_steps()

    def _setup_completed(self) -> bool:
        return bool(self._setup_state.get("completed"))

    def _refresh_setup_state(self, check_permissions: bool = True):
        if check_permissions:
            missing = self._missing_setup_steps()
            self._setup_state["missing_steps"] = missing
            self._setup_state["ready"] = not missing
        else:
            self._setup_state.setdefault("missing_steps", [])
            self._setup_state.setdefault("ready", False)
        self._save_setup_state()
        self._update_setup_ui()

    def _update_setup_ui(self):
        def _apply():
            missing = self._setup_state.get("missing_steps", [])
            if not self._setup_completed():
                self.setup_item.title = "Open Setup"
            elif not missing:
                self.setup_item.title = "Open Setup"
            else:
                self.setup_item.title = "Open Setup"
        self._run_on_main(_apply)

    def _request_folder_access(self) -> bool:
        required_dirs = self._required_dirs()
        if not required_dirs:
            return True
        for path in required_dirs:
            if can_access_directory(path):
                continue
            try:
                with os.scandir(path) as it:
                    next(it, None)
            except PermissionError:
                pass
            except Exception:
                pass
            if not can_access_directory(path):
                self._notify(
                    "Allow Folder Access",
                    f"Allow access to {os.path.basename(path) or path}, then click Complete Setup again.",
                )
                self._set_status(f"Allow {os.path.basename(path) or path} access")
                return False
        return True

    def _deferred_first_run_prompt(self, timer):
        timer.stop()
        if self._setup_completed():
            return
        self._run_on_main(self.show_setup_window)

    def _setup_checklist_text(self) -> str:
        self._refresh_setup_state()
        folder_ready = not self._setup_state.get("missing_steps")
        access_ready = is_accessibility_trusted()
        test_ready = bool(self._setup_state.get("test_paste_ok"))
        folder_label = self._required_dirs()[0] if self._required_dirs() else "Screenshot folder"
        return (
            f"{'✓' if folder_ready else '○'} Folder access for {os.path.basename(folder_label) or folder_label}\n"
            f"{'✓' if access_ready else '○'} Accessibility for reliable paste shortcuts\n"
            f"{'✓' if test_ready else '○'} Test paste completed successfully"
        )

    def _setup_row_data(self):
        self._refresh_setup_state()
        folder_ready = not self._setup_state.get("missing_steps")
        access_ready = is_accessibility_trusted()
        test_ready = bool(self._setup_state.get("test_paste_ok"))
        folder_label = self._required_dirs()[0] if self._required_dirs() else "Screenshot folder"
        return [
            (
                folder_ready,
                "Folder access",
                os.path.basename(folder_label) or folder_label,
            ),
            (
                access_ready,
                "Accessibility",
                "Recommended for reliable keyboard paste",
            ),
            (
                test_ready,
                "Test paste",
                "Verify a screenshot lands in the active chat",
            ),
        ]

    def _setup_hint_text(self) -> str:
        self._refresh_setup_state()
        if not self._setup_completed():
            return (
                "Start with Complete Setup. The app will ask for access only when you choose it."
            )
        if self._setup_state.get("missing_steps"):
            missing = self._setup_state["missing_steps"][0]
            return f"One step remains: allow access to {missing}, then click Complete Setup again."
        if not self._setup_state.get("test_paste_ok"):
            return "Setup is almost done. Run Test Paste once to verify Chrome receives uploads."
        return "Everything is ready. You can close this window and turn on Auto-paste anytime."

    def _setup_footer_text(self) -> str:
        if self.enabled:
            return "Auto-paste is on"
        if self._setup_completed() and not self._setup_state.get("missing_steps"):
            return "Ready to enable"
        return "Setup in progress"

    def _style_setup_button(self, button, key: str):
        button.setBezelStyle_(1)
        if key == "primary":
            try:
                button.setKeyEquivalent_("\r")
            except Exception:
                pass

    def _make_card_view(self, frame, background):
        card = NSView.alloc().initWithFrame_(frame)
        card.setWantsLayer_(True)
        try:
            card.layer().setCornerRadius_(14.0)
            card.layer().setBackgroundColor_(background.CGColor())
        except Exception:
            pass
        return card

    def _ensure_setup_window(self):
        if self._setup_window is not None or not _NSSWITCH_AVAILABLE:
            return
        frame = NSMakeRect(0, 0, 600, 560)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        window.setTitle_("Screenshot to AI Setup")
        try:
            window.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameAqua"))
        except Exception:
            pass

        content = window.contentView()
        content.setWantsLayer_(True)
        try:
            content.layer().setBackgroundColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(0.96, 0.97, 0.99, 1.0).CGColor()
            )
        except Exception:
            pass

        hero = self._make_card_view(
            NSMakeRect(24, 404, 552, 126),
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.14, 0.35, 0.82, 1.0),
        )
        content.addSubview_(hero)

        title = NSTextField.labelWithString_("Finish Setup")
        title.setFrame_(NSMakeRect(28, 68, 320, 34))
        title.setFont_(NSFont.boldSystemFontOfSize_(30.0))
        title.setTextColor_(NSColor.whiteColor())
        hero.addSubview_(title)

        subtitle = NSTextField.labelWithString_(
            "Set permissions once, verify paste, then let the app quietly catch new screenshots."
        )
        subtitle.setFrame_(NSMakeRect(28, 28, 420, 30))
        subtitle.setFont_(NSFont.systemFontOfSize_(15.0))
        subtitle.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.92, 1.0))
        subtitle.setUsesSingleLineMode_(False)
        subtitle.setLineBreakMode_(NSLineBreakByWordWrapping)
        hero.addSubview_(subtitle)

        hero_badge = NSTextField.labelWithString_("")
        hero_badge.setFrame_(NSMakeRect(442, 76, 82, 18))
        hero_badge.setAlignment_(2)
        hero_badge.setFont_(NSFont.boldSystemFontOfSize_(11.0))
        hero_badge.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.95, 1.0))
        hero.addSubview_(hero_badge)

        checklist_label = NSTextField.labelWithString_("Setup checklist")
        checklist_label.setFrame_(NSMakeRect(34, 368, 160, 18))
        checklist_label.setFont_(NSFont.boldSystemFontOfSize_(12.0))
        checklist_label.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(checklist_label)

        body = NSTextField.alloc().initWithFrame_(NSMakeRect(34, 342, 300, 20))
        body.setBezeled_(False)
        body.setDrawsBackground_(False)
        body.setEditable_(False)
        body.setSelectable_(False)
        body.setFont_(NSFont.boldSystemFontOfSize_(16.0))
        body.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(body)

        row_views = []
        row_title_labels = []
        row_detail_labels = []
        row_status_labels = []
        row_y = 274
        for _ in range(3):
            card = self._make_card_view(
                NSMakeRect(34, row_y, 532, 72),
                NSColor.whiteColor(),
            )
            status = NSTextField.labelWithString_("○")
            status.setFrame_(NSMakeRect(20, 24, 28, 24))
            status.setFont_(NSFont.boldSystemFontOfSize_(22.0))
            card.addSubview_(status)

            row_title = NSTextField.labelWithString_("")
            row_title.setFrame_(NSMakeRect(58, 38, 260, 18))
            row_title.setFont_(NSFont.boldSystemFontOfSize_(16.0))
            card.addSubview_(row_title)

            row_detail = NSTextField.labelWithString_("")
            row_detail.setFrame_(NSMakeRect(58, 18, 430, 16))
            row_detail.setFont_(NSFont.systemFontOfSize_(13.0))
            row_detail.setTextColor_(NSColor.secondaryLabelColor())
            card.addSubview_(row_detail)

            content.addSubview_(card)
            row_views.append(card)
            row_status_labels.append(status)
            row_title_labels.append(row_title)
            row_detail_labels.append(row_detail)
            row_y -= 88

        hint_card = self._make_card_view(
            NSMakeRect(34, 92, 532, 90),
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.94, 0.97, 1.0, 1.0),
        )
        content.addSubview_(hint_card)

        hint_title = NSTextField.labelWithString_("What to do next")
        hint_title.setFrame_(NSMakeRect(18, 58, 160, 18))
        hint_title.setFont_(NSFont.boldSystemFontOfSize_(12.0))
        hint_title.setTextColor_(NSColor.secondaryLabelColor())
        hint_card.addSubview_(hint_title)

        hint = NSTextField.alloc().initWithFrame_(NSMakeRect(18, 18, 496, 34))
        hint.setBezeled_(False)
        hint.setDrawsBackground_(False)
        hint.setEditable_(False)
        hint.setSelectable_(False)
        hint.setUsesSingleLineMode_(False)
        hint.setLineBreakMode_(NSLineBreakByWordWrapping)
        hint.setFont_(NSFont.systemFontOfSize_(13.0))
        hint.setTextColor_(NSColor.labelColor())
        hint_card.addSubview_(hint)

        buttons = [
            ("Complete Setup", self.complete_setup, 34, 28, 150, "primary"),
            ("Grant Accessibility", self.grant_accessibility, 198, 28, 170, "secondary"),
            ("Test Paste", self.paste_last, 382, 28, 104, "secondary"),
            ("Reset", self.reset_setup, 500, 28, 66, "secondary"),
        ]

        for label, callback, x, y, width, role in buttons:
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, width, 36))
            btn.setTitle_(label)
            self._style_setup_button(btn, role)
            target = _CallbackTarget.alloc().init_with_callback(callback)
            btn.setTarget_(target)
            btn.setAction_(objc.selector(target.triggered_, signature=b'v@:@'))
            self._setup_button_targets.append(target)
            self._setup_buttons[label] = btn
            content.addSubview_(btn)

        self._setup_window = window
        self._setup_title_label = title
        self._setup_label = body
        self._setup_hint_label = hint
        self._setup_footer_label = hero_badge
        self._setup_row_labels = row_status_labels
        self._setup_row_title_labels = row_title_labels
        self._setup_row_detail_labels = row_detail_labels
        self._setup_row_views = row_views
        self._refresh_setup_window()

    def _refresh_setup_window(self):
        if self._setup_label is None:
            return
        if self._setup_completed() and not self._setup_state.get("missing_steps"):
            self._setup_label.setStringValue_("Everything looks good")
        else:
            self._setup_label.setStringValue_("Finish these three steps")
        row_data = self._setup_row_data()
        for idx, row in enumerate(row_data):
            ready, title, detail = row
            status = self._setup_row_labels[idx]
            title_label = self._setup_row_title_labels[idx]
            detail_label = self._setup_row_detail_labels[idx]
            card = self._setup_row_views[idx]
            status.setStringValue_("✓" if ready else "○")
            status.setTextColor_(
                NSColor.systemGreenColor() if ready else NSColor.tertiaryLabelColor()
            )
            title_label.setStringValue_(title)
            detail_label.setStringValue_(detail)
            try:
                card.layer().setBackgroundColor_(
                    (NSColor.colorWithCalibratedRed_green_blue_alpha_(0.91, 0.98, 0.93, 1.0) if ready
                     else NSColor.whiteColor()).CGColor()
                )
            except Exception:
                pass
        if self._setup_hint_label is not None:
            self._setup_hint_label.setStringValue_(self._setup_hint_text())
        footer = getattr(self, "_setup_footer_label", None)
        if footer is not None:
            footer.setStringValue_(self._setup_footer_text().upper())
        complete_button = self._setup_buttons.get("Complete Setup")
        test_button = self._setup_buttons.get("Test Paste")
        if complete_button is not None:
            complete_button.setEnabled_(not self._setup_completed() or bool(self._setup_state.get("missing_steps")))
        if test_button is not None:
            test_button.setEnabled_(self._setup_completed() and not self._setup_state.get("missing_steps"))

    def show_setup_window(self, _=None):
        if not _NSSWITCH_AVAILABLE:
            self._notify(
                "Open Setup",
                "Open the menu and use Complete Setup, Grant Accessibility, and Paste Last Screenshot.",
            )
            return
        try:
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self._ensure_setup_window()
            self._refresh_setup_window()
            self._setup_window.makeKeyAndOrderFront_(None)
        except Exception as e:
            log(f"First-run prompt failed: {e}")

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
        self._update_toggle_badge()

    def toggle(self, sender):
        """Called when the menu item row is clicked (fallback / keyboard nav)."""
        new_state = not self.enabled
        if self._nsswitch is not None:
            self._nsswitch.setState_(1 if new_state else 0)
        self._apply_toggle(new_state)
        self._update_toggle_badge()
        if self._nsswitch is None:
            sender.state = 1 if new_state else 0

    def _apply_toggle(self, is_on: bool):
        if is_on:
            self._refresh_setup_state()
            if not self._setup_completed() or not self._setup_ready():
                self.enabled = False
                self.title = "📸✕"
                if self._nsswitch is not None:
                    self._nsswitch.setState_(0)
                self.toggle_item.state = 0
                self._update_toggle_badge()
                self._notify(
                    "Setup Required",
                    "Click Complete Setup first.",
                )
                return
            self.enabled = True
            self.title   = "📸"
            self._start_watcher()
            log("Auto-paste enabled")
        else:
            self.enabled = False
            self.title   = "📸✕"
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
        def _apply():
            if self._pinned_tab:
                self.pin_item.title = f"Target: {self._pinned_service} pinned"
            elif self._last_tab:
                self.pin_item.title = f"Target: Auto ({self._last_service})"
            else:
                self.pin_item.title = "Target: Auto-detect"
        self._run_on_main(_apply)

    # ── Manual retry ──────────────────────────────────────────────────────────

    def paste_last(self, _):
        self._refresh_setup_state()
        # Read the user's custom screenshot prefix (defaults to "Screenshot")
        r = run_command(["defaults", "read", "com.apple.screencapture", "name"])
        prefix = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "Screenshot"
        candidates = []
        for d in get_screenshot_dirs():
            for ext in ("png", "jpg", "jpeg", "tiff", "tif"):
                candidates += [
                    f for f in glob.glob(os.path.join(d, f"{prefix}*.{ext}"))
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
        if self._watcher and self._watcher.is_alive():
            return

        # ── Primary: watchdog on the actual screenshot directories ─────────────
        if not _WATCHDOG_AVAILABLE:
            log("watchdog unavailable, trying Spotlight watcher")
        else:
            handler = ScreenshotHandler(self)
            watcher = Observer()
            watched_any = False
            for d in get_screenshot_dirs():
                try:
                    watcher.schedule(handler, d, recursive=False)
                    log(f"Watching (watchdog): {d}")
                    watched_any = True
                except PermissionError:
                    log(f"⚠️  Permission denied watching {d}")

            if watched_any:
                watcher.start()
                self._watcher = watcher
                return

            log("No screenshot directories watchable via watchdog, trying Spotlight")

        # ── Fallback: Spotlight via mdfind ────────────────────────────────────
        try:
            test = run_command(
                ["mdfind", "-onlyin", os.path.expanduser("~"),
                 "kMDItemIsScreenCapture == 1"],
                timeout=3
            )
            if test.returncode == 0:
                self._watcher = SpotlightScreenshotWatcher(self.handle_new_screenshot)
                self._watcher.start()
                return
        except Exception as e:
            log(f"Spotlight unavailable ({e})")

        log("❌ Neither watchdog nor Spotlight watcher could start")

    def _stop_watcher(self):
        if self._watcher:
            self._watcher.stop()
            self._watcher.join()
            self._watcher = None

    # ── Core logic ────────────────────────────────────────────────────────────

    def handle_new_screenshot(self, filepath: str):
        if not self.enabled:
            return
        threading.Thread(target=self._paste_screenshot, args=(filepath,), daemon=True).start()

    def _paste_screenshot(self, filepath: str):
        filename = os.path.basename(filepath)
        log(f"Processing: {filename}")

        # ── Edge case: file moved/deleted before we got here ──────────────────
        # Wait up to 3 s for the file to settle (macOS sometimes renames/moves
        # the temp file to the final path in the background).
        deadline = time.time() + 3.0
        while not os.path.exists(filepath):
            if time.time() > deadline:
                log(f"  ❌ File no longer exists (may have been moved): {filename}")
                self._notify("Screenshot not found ⚠️",
                             "The file was moved or deleted before it could be pasted.")
                self._set_status("⚠️  file missing")
                return
            time.sleep(0.25)

        # ── Edge case: file size is still 0 (writing in progress) ─────────────
        deadline2 = time.time() + 3.0
        while os.path.getsize(filepath) == 0:
            if time.time() > deadline2:
                log(f"  ❌ File size still 0 after waiting: {filename}")
                break
            time.sleep(0.2)

        # ── Edge case: Chrome not running at all ──────────────────────────────
        if not is_chrome_running():
            log("  ❌ Google Chrome is not running")
            self._notify("Chrome not running ⚠️",
                         "Open Google Chrome with Claude.ai or ChatGPT to use auto-paste.")
            self._set_status("⚠️  Chrome not running")
            return

        # Priority 1 — explicitly pinned tab
        tab, used_pin, used_fallback = None, False, False

        if self._pinned_tab:
            if verify_tab(*self._pinned_tab):
                tab      = self._pinned_tab
                used_pin = True
                log(f"  Using pinned tab: {self._pinned_service}")
            else:
                log(f"  Pinned tab ({self._pinned_service}) is gone — clearing pin")
                self._notify(
                    f"Pinned tab closed ⚠️",
                    f"{self._pinned_service} tab is no longer open. Pin cleared — switching to auto-detect."
                )
                self._pinned_tab     = None
                self._pinned_service = ""
                self._run_on_main(self._update_pin_label)

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
                "Open Claude.ai or ChatGPT in Chrome, or use 🎯 Set target tab."
            )
            self._set_status("⚠️  No AI tab found")
            return

        # ── Edge case: file was moved/renamed between detection and now ───────
        # Re-check existence right before the paste attempt.
        if not os.path.exists(filepath):
            log(f"  ❌ File disappeared just before paste: {filename}")
            self._notify("Screenshot vanished ⚠️",
                         "The file was moved or deleted just before pasting.")
            self._set_status("⚠️  file disappeared")
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

                self._setup_state["test_paste_ok"] = True
                self._save_setup_state()
                self._run_on_main(self._refresh_setup_window)

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
                self._notify(
                    "Paste failed ⚠️",
                    "Grant Accessibility to Screenshot to AI, then try again.",
                )
                self._set_status("❌  paste failed")
        except Exception as e:
            log(f"  ❌ Exception: {e}")
            self._notify("Error ❌", str(e))
            self._set_status(f"❌  {e}")

    # ── Start at Login (Launch Agent) ─────────────────────────────────────────

    _AGENT_LABEL = "com.screenshot-to-ai"
    _AGENT_PLIST = os.path.expanduser(
        "~/Library/LaunchAgents/com.screenshot-to-ai.plist"
    )

    def _is_agent_installed(self) -> bool:
        return os.path.exists(self._AGENT_PLIST)

    def _install_launch_agent(self):
        """Write the launchd plist and load it so the app auto-starts on login."""
        python  = sys.executable
        script  = os.path.abspath(__file__)
        log     = os.path.expanduser("~/Library/Logs/screenshot-to-ai.log")

        os.makedirs(os.path.dirname(self._AGENT_PLIST), exist_ok=True)
        os.makedirs(os.path.dirname(log), exist_ok=True)

        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{self._AGENT_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
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
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
</dict>
</plist>
"""
        with open(self._AGENT_PLIST, "w") as f:
            f.write(plist)

        # Load immediately (takes effect for this session too)
        subprocess.run(
            ["launchctl", "load", self._AGENT_PLIST],
            capture_output=True
        )
        log_msg = f"Launch Agent installed → {self._AGENT_PLIST}"
        log(log_msg)

    def _uninstall_launch_agent(self):
        """Unload and remove the launchd plist."""
        subprocess.run(
            ["launchctl", "unload", self._AGENT_PLIST],
            capture_output=True
        )
        try:
            os.remove(self._AGENT_PLIST)
        except FileNotFoundError:
            pass
        log(f"Launch Agent removed")

    def toggle_login_item(self, sender):
        """Install or remove the Launch Agent when the user clicks 'Start at Login'."""
        if self._is_agent_installed():
            self._uninstall_launch_agent()
            sender.state = 0
            self._notify("Removed from Login Items",
                         "The app will no longer start automatically.")
        else:
            self._install_launch_agent()
            sender.state = 1
            self._notify("Added to Login Items ✅",
                         "The 📸 icon will appear automatically every time you log in.")

    def grant_accessibility(self, _):
        if is_accessibility_trusted(prompt=True):
            self._set_status("✅  Accessibility granted")
            self._notify("Accessibility already enabled", "Auto-paste is ready.")
            self._run_on_main(self._refresh_setup_window)
            return
        self._set_status("⚠️  Enable Accessibility, then turn Auto-paste on")
        self._notify(
            "Grant Accessibility",
            "Turn on Screenshot to AI in System Settings, then re-enable Auto-paste.",
        )
        self._run_on_main(self._refresh_setup_window)

    def complete_setup(self, _):
        self._refresh_setup_state()
        if not self._request_folder_access():
            self._refresh_setup_state()
            return
        self._refresh_setup_state()
        self._setup_state["completed"] = True
        self._save_setup_state()
        self.enabled = True
        self.title = "📸"
        if self._nsswitch is not None:
            self._nsswitch.setState_(1)
        self.toggle_item.state = 1
        self._update_toggle_badge()
        self._start_watcher()
        self._set_status("Setup complete")
        self._notify(
            "Setup Complete ✅",
            "Folder access is ready. Auto-paste is on.",
        )
        self._run_on_main(self._refresh_setup_window)

    def reset_setup(self, _):
        self._setup_state = {"completed": False, "ready": False, "missing_steps": []}
        self._save_setup_state()
        self.enabled = False
        self.title = "📸✕"
        if self._nsswitch is not None:
            self._nsswitch.setState_(0)
        self.toggle_item.state = 0
        self._update_toggle_badge()
        self._stop_watcher()
        self._set_status("Setup reset")
        self._update_setup_ui()
        self._refresh_setup_window()
        self.show_setup_window()

    def _ensure_accessibility(self, prompt: bool, notify: bool) -> bool:
        if is_accessibility_trusted(prompt=prompt):
            return True
        log("Accessibility permission missing")
        self._set_status("⚠️  Accessibility required")
        if notify:
            self._notify(
                "Accessibility required",
                "Enable Screenshot to AI in System Settings to allow pasting into Chrome.",
            )
        return False

    def _run_on_main(self, fn, *args):
        if _APPHELPER_AVAILABLE:
            AppHelper.callAfter(fn, *args)
        else:
            fn(*args)

    # ── Notifications / status ─────────────────────────────────────────────────

    def _notify(self, subtitle: str, message: str):
        self._run_on_main(
            lambda: rumps.notification("Screenshot to AI", subtitle, message, sound=False)
        )

    def _set_status(self, text: str):
        self._run_on_main(lambda: setattr(self.status_item, "title", f"Last: {text}"))


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import fcntl

    # ── Single-instance lock ───────────────────────────────────────────────────
    # Prevents duplicate 📸 icons when the app is opened more than once.
    # The lock file is held for the lifetime of the process and automatically
    # released (even on crash) when the process exits.
    _LOCK_DIR  = os.path.expanduser("~/Library/Application Support/screenshot-to-ai")
    _LOCK_PATH = os.path.join(_LOCK_DIR, "app.lock")
    os.makedirs(_LOCK_DIR, exist_ok=True)
    _lock_fh = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        # Another instance is already running — surface it and exit quietly.
        subprocess.run(
            ["osascript", "-e",
             'display notification "Already running — look for 📸 in your menu bar." '
             'with title "Screenshot to AI"'],
            capture_output=True
        )
        sys.exit(0)

    # ── Suppress Dock icon ─────────────────────────────────────────────────────
    suppress_dock_icon()

    log("Starting…")
    ScreenshotToAIApp().run()
