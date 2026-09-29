#!/bin/bash
# Builds two apps and installs them into Applications, so they show up in
# Launchpad, Spotlight and Finder:
#   Jarvis.app         starts the wake listener in the background — no Dock icon,
#                      no window. Say "Hey Jarvis".
#   Jarvis Status.app  shows whether Jarvis is working, with Restart and Open Log.
#
#   bash build_app.sh
#
# The project folder and .venv paths are baked into the app, so re-run this
# after moving the project or recreating the virtual environment.

set -euo pipefail

PROJECT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT/.venv/bin/python"
BUILD_DIR="$PROJECT/build"
APP="$BUILD_DIR/Jarvis.app"

if [[ ! -x "$PYTHON" ]]; then
    echo "No virtual environment at $PROJECT/.venv. Create it first:"
    echo "  uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt"
    exit 1
fi

# --- Icon -------------------------------------------------------------------
# Redrawn whenever make_icon.py has changed since the last build
if [[ ! -f "$PROJECT/assets/Jarvis.icns" || "$PROJECT/assets/make_icon.py" -nt "$PROJECT/assets/Jarvis.icns" ]]; then
    echo "Drawing icon..."
    ICONSET="$BUILD_DIR/Jarvis.iconset"
    mkdir -p "$ICONSET"
    "$PYTHON" "$PROJECT/assets/make_icon.py" "$BUILD_DIR/icon_1024.png"
    for size in 16 32 128 256 512; do
        sips -z $size $size "$BUILD_DIR/icon_1024.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
        sips -z $((size * 2)) $((size * 2)) "$BUILD_DIR/icon_1024.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$PROJECT/assets/Jarvis.icns"
fi

# --- Bundle -----------------------------------------------------------------
echo "Building Jarvis.app..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$PROJECT/assets/Jarvis.icns" "$APP/Contents/Resources/Jarvis.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>                 <string>Jarvis</string>
    <key>CFBundleDisplayName</key>          <string>Jarvis</string>
    <key>CFBundleIdentifier</key>           <string>com.jarvisai.jarvis</string>
    <key>CFBundleExecutable</key>           <string>Jarvis</string>
    <key>CFBundleIconFile</key>             <string>Jarvis</string>
    <key>CFBundlePackageType</key>          <string>APPL</string>
    <key>CFBundleShortVersionString</key>   <string>0.2</string>
    <key>CFBundleVersion</key>              <string>2</string>
    <key>LSMinimumSystemVersion</key>       <string>13.0</string>
    <!-- Background agent: no Dock icon or menu bar while it runs -->
    <key>LSUIElement</key>                  <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Jarvis listens for "Hey Jarvis" and for your commands.</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>Jarvis manages your reminders and looks things up in Contacts and Notes.</string>
    <!-- Without these macOS refuses calendar access silently instead of asking -->
    <key>NSCalendarsUsageDescription</key>
    <string>Jarvis reads your calendars to answer questions about your schedule.</string>
    <key>NSCalendarsFullAccessUsageDescription</key>
    <string>Jarvis reads your calendars to answer questions about your schedule.</string>
    <key>NSContactsUsageDescription</key>
    <string>Jarvis looks up people's details when you ask.</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/Jarvis" <<LAUNCHER
#!/bin/bash
# Started by macOS when the Jarvis icon is opened.
cd "$PROJECT" || exit 1
mkdir -p logs
# Keep the log from growing without bound
if [[ -f logs/jarvis.log && \$(stat -f%z logs/jarvis.log) -gt 5000000 ]]; then
    mv -f logs/jarvis.log logs/jarvis.log.1
fi
export PYTHONUNBUFFERED=1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
exec "$PYTHON" wake_listener.py >> logs/jarvis.log 2>&1
LAUNCHER
chmod +x "$APP/Contents/MacOS/Jarvis"

# Ad-hoc signature gives macOS a stable identity to attach the microphone
# permission to, so it isn't asked for again after every rebuild.
codesign --force --sign - "$APP" >/dev/null

# --- Status app -------------------------------------------------------------
# A second click on Jarvis.app can't report anything — macOS just brings the
# running app forward without starting it again — so status is its own app.
STATUS_APP="$BUILD_DIR/Jarvis Status.app"
echo "Building Jarvis Status.app..."
rm -rf "$STATUS_APP"
mkdir -p "$STATUS_APP/Contents/MacOS" "$STATUS_APP/Contents/Resources"
cp "$PROJECT/assets/Jarvis.icns" "$STATUS_APP/Contents/Resources/Jarvis.icns"
cat > "$STATUS_APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>                 <string>Jarvis Status</string>
    <key>CFBundleDisplayName</key>          <string>Jarvis Status</string>
    <key>CFBundleIdentifier</key>           <string>com.jarvisai.status</string>
    <key>CFBundleExecutable</key>           <string>JarvisStatus</string>
    <key>CFBundleIconFile</key>             <string>Jarvis</string>
    <key>CFBundlePackageType</key>          <string>APPL</string>
    <key>CFBundleShortVersionString</key>   <string>0.2</string>
    <key>CFBundleVersion</key>              <string>2</string>
    <key>LSMinimumSystemVersion</key>       <string>13.0</string>
    <key>LSUIElement</key>                  <true/>
</dict>
</plist>
PLIST
cat > "$STATUS_APP/Contents/MacOS/JarvisStatus" <<LAUNCHER
#!/bin/bash
# Started by macOS when the Jarvis Status icon is opened.
cd "$PROJECT" || exit 1
exec "$PYTHON" status.py --dialog
LAUNCHER
chmod +x "$STATUS_APP/Contents/MacOS/JarvisStatus"
codesign --force --sign - "$STATUS_APP" >/dev/null

# --- Install ----------------------------------------------------------------
if [[ -w /Applications ]]; then DEST="/Applications"; else DEST="$HOME/Applications"; mkdir -p "$DEST"; fi
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
for bundle in "$APP" "$STATUS_APP"; do
    name="$(basename "$bundle")"
    rm -rf "$DEST/$name"
    cp -R "$bundle" "$DEST/"
    "$LSREGISTER" -f "$DEST/$name"
done

echo ""
echo "Installed $DEST/Jarvis.app and $DEST/Jarvis Status.app"
echo "  Start:   open Jarvis from Launchpad or Spotlight, then say \"Hey Jarvis\""
echo "  Check:   open Jarvis Status (or run: .venv/bin/python status.py)"
echo "  Logs:    tail -f $PROJECT/logs/jarvis.log"
echo "  Stop:    pkill -f wake_listener.py"
