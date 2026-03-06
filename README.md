# 📸 screenshot-to-ai

A lightweight macOS menubar app that watches for new screenshots and automatically pastes them into your open **Claude.ai** or **ChatGPT** Chrome tab — with a one-click toggle to turn it on or off.

---

## How it works

1. You take a screenshot (`Cmd + Shift + 4` or `Cmd + Shift + 3`)
2. The app detects the new file in your Screenshots folder or Desktop
3. It finds your open Claude.ai or ChatGPT Chrome tab
4. It switches to that tab, copies the image to your clipboard, and pastes it into the chat input
5. You just type your question — the screenshot is already there

---

## Requirements

- macOS (tested on macOS 13+)
- Python 3.9+
- Google Chrome
- **Accessibility permission** for Terminal/your Python process
  *(System Settings → Privacy & Security → Accessibility → enable Terminal)*

---

## Setup

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/screenshot-to-ai.git
cd screenshot-to-ai

# Install dependencies and launch
chmod +x setup.sh
./setup.sh
```

Or manually:

```bash
pip3 install -r requirements.txt
python3 app.py
```

---

## Usage

- A **📸** icon appears in your menu bar when the app is running
- Click it to see options:
  - **Auto-paste: ON / OFF** — toggle the feature
  - **Last action** — shows the last screenshot that was handled
  - **Quit** — exit the app

### Toggle keyboard-friendly workflow

1. Turn on auto-paste from the menu bar
2. Take a screenshot → it appears in Claude/ChatGPT automatically
3. Turn off when you don't want screenshots auto-pasting

---

## Supported AI tabs

| Service | URL matched |
|---|---|
| Claude.ai | `claude.ai` |
| ChatGPT | `chat.openai.com`, `chatgpt.com` |

The app picks the **first matching tab** it finds across all Chrome windows. If both are open, Claude.ai takes priority (it's listed first in the search order).

---

## Auto-start on login (optional)

To have the app launch automatically when you log in:

```bash
# Create a launchd plist
cat > ~/Library/LaunchAgents/com.screenshot-to-ai.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.screenshot-to-ai</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/screenshot-to-ai/app.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.screenshot-to-ai.plist
```

---

## Troubleshooting

**"No AI tab found" notification**
→ Make sure Claude.ai or ChatGPT is open in Chrome (not just in the background with no tab visible).

**Screenshot pastes but lands in the wrong place**
→ The app pastes via `Cmd+V` after switching tabs. If Chrome's input field isn't focused, click into the chat input once manually — it should auto-focus on subsequent screenshots.

**App doesn't detect screenshots**
→ Check that your screenshots are saving to `~/Desktop` or `~/Pictures/Screenshots`. You can change the save location in `app.py` under `WATCH_DIRS`.

**Accessibility permission error**
→ Go to System Settings → Privacy & Security → Accessibility and enable your Terminal app (or the Python binary).

---

## License

MIT
