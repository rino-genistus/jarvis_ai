#!/bin/zsh
# Build Jarvis.app — compiles launcher.c and assembles a minimal app bundle
# so Jarvis can be opened like any normal Mac app.
set -euo pipefail
cd "$(dirname "$0")"

APP="Jarvis.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

clang -O2 -o "$APP/Contents/MacOS/Jarvis" launcher.c

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>Jarvis</string>
    <key>CFBundleDisplayName</key>     <string>Jarvis</string>
    <key>CFBundleIdentifier</key>      <string>com.jarvisai.app</string>
    <key>CFBundleVersion</key>         <string>1.0</string>
    <key>CFBundleExecutable</key>      <string>Jarvis</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>LSUIElement</key>             <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Jarvis listens for the wake word and voice commands.</string>
</dict>
</plist>
PLIST

echo "Built $APP — open it with: open $APP"
