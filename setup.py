from setuptools import setup


APP = ["app.py"]
OPTIONS = {
    "argv_emulation": False,
    "strip": False,
    "iconfile": "icon.icns",
    "plist": {
        "CFBundleName": "Screenshot to AI",
        "CFBundleDisplayName": "Screenshot to AI",
        "CFBundleIdentifier": "com.screenshot-to-ai",
        "CFBundleShortVersionString": "1.2",
        "CFBundleVersion": "1.2.0",
        "LSUIElement": True,
        "LSBackgroundOnly": False,
        "LSMinimumSystemVersion": "10.15",
        "NSHighResolutionCapable": True,
        "NSSupportsAutomaticGraphicsSwitching": True,
    },
    "packages": ["rumps", "watchdog"],
    "includes": [
        "objc",
        "AppKit",
        "Foundation",
        "Quartz",
        "ctypes",
    ],
}


setup(
    app=APP,
    name="ScreenshotToAI",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
