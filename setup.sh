#!/bin/bash
# ── screenshot-to-ai setup ────────────────────────────────────────────────────
# Run this once to install dependencies and launch the app.

set -e

echo "📦 Installing dependencies..."
pip3 install rumps watchdog pyautogui Pillow --break-system-packages

echo ""
echo "✅ All set! Starting the app..."
echo "   Look for the 📸 icon in your menu bar."
echo "   Click it to toggle auto-paste on/off."
echo ""

python3 app.py
