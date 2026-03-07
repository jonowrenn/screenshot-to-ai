# 📸 Screenshot to AI

A lightweight macOS menu bar app that automatically pastes your screenshots directly into [Claude.ai](https://claude.ai) or [ChatGPT](https://chatgpt.com) — the moment you take them.

No copy-paste. No dragging. Just take a screenshot and it's already in the chat.

---

## Install (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/jonowrenn/screenshot-to-ai/main/install.sh | bash
```

This downloads the source, installs Python dependencies, and builds a real `.app` in your `~/Applications` folder in about 30 seconds.

**After installing:**
1. Open **Finder → Applications** (or Spotlight: `ScreenshotToAI`)
2. Double-click `ScreenshotToAI` to launch
3. The **📸 icon** appears in your menu bar
4. Click it → enable **"Start at Login"** so it's always there

---

## Features

- **Auto-paste** — watches your Screenshots folder and sends new screenshots to your open AI tab instantly
- **Smart tab detection** — finds your active Claude or ChatGPT tab; remembers the last one used when you screenshot from another app
- **Pin a tab** — lock screenshots to a specific tab; great when you screenshot from Notability, VS Code, etc.
- **Start at Login** — one click in the menu makes it permanent
- **Crash recovery** — auto-restarts via macOS launchd if something goes wrong
- **Edge case safe** — handles moved/deleted files, Chrome not running, rapid back-to-back shots, collapsed tab groups

---

## Requirements

- **macOS 10.15 Catalina** or later
- **Python 3** (pre-installed on macOS — no extra steps)
- **Google Chrome** with Claude.ai or ChatGPT open in a tab

### Permissions macOS will ask for

| Permission | Why |
|---|---|
| **Accessibility** | Needed to paste into Chrome via keyboard shortcut |
| **Notifications** | Confirmation when a screenshot is pasted |
| **Screen Recording** | May be requested on macOS Ventura+ |

---

## How it works

```
Take a screenshot  →  app detects the new file
  →  copies it to clipboard  →  activates your Chrome AI tab
  →  JavaScript focuses the chat input  →  ⌘V pastes the image
  →  ✅ notification confirms success
```

The app watches `~/Desktop` and your system Screenshots folder (auto-detected from macOS preferences).

---

## Menu reference

| Item | What it does |
|---|---|
| **Auto-paste** toggle | Enable / disable the watcher |
| **Set Target Tab** | Pins the current AI tab instantly (or shows a picker if you're not in one) |
| *(pin status)* | Shows the pinned tab; click to clear it |
| **Paste Last Screenshot** | Manually re-sends the most recent screenshot |
| **Start at Login** | Installs / removes the macOS Launch Agent for auto-start |
| **Quit** | Exits cleanly (won't auto-restart until next login) |

---

## Supported AI services

| Service | URL |
|---|---|
| Claude | `claude.ai` |
| ChatGPT | `chat.openai.com`, `chatgpt.com` |

---

## Manual install (developers)

```bash
git clone https://github.com/jonowrenn/screenshot-to-ai.git
cd screenshot-to-ai
./setup.sh
```

Or run directly:

```bash
pip3 install rumps watchdog pyobjc-core pyobjc-framework-Cocoa
python3 app.py
```

---

## Troubleshooting

**"No AI tab found" notification**
Click **Set Target Tab** while you're in a Claude or ChatGPT tab to pin it.

**App doesn't start on login**
Open the app and click **Start at Login** in the menu.

**macOS blocked the app ("unidentified developer")**
Right-click the `.app` → Open → Open anyway. Or run:
```bash
xattr -cr ~/Applications/ScreenshotToAI.app
```

**View live logs**
```bash
tail -f ~/Library/Logs/screenshot-to-ai.log
```

---

## Contributing

Pull requests welcome. The app is a single Python file (`app.py`) using:
- [`rumps`](https://github.com/jaredks/rumps) — macOS menu bar framework
- [`watchdog`](https://github.com/gorakhargosh/watchdog) — file system events
- `PyObjC` — native macOS UI (NSSwitch toggle)
- `osascript` / AppleScript — Chrome tab control and clipboard

---

## License

MIT — see [LICENSE](LICENSE)
