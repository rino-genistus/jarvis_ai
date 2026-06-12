#!/bin/zsh
# Register Jarvis as login services: com.jarvisai.backend + com.jarvisai.ui.
# KeepAlive means auto-start on login and auto-restart on crash.
#
#   ./install_launch_agents.sh           install / reinstall both agents
#   ./install_launch_agents.sh remove    unload and delete both agents
set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"
mkdir -p "$AGENTS_DIR" "$JARVIS_DIR/logs"

write_plist() {
    local label="$1" script="$2"
    cat > "$AGENTS_DIR/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>             <string>$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$JARVIS_DIR/$script</string>
    </array>
    <key>WorkingDirectory</key>  <string>$JARVIS_DIR</string>
    <key>RunAtLoad</key>         <true/>
    <key>KeepAlive</key>         <true/>
    <key>StandardOutPath</key>   <string>$JARVIS_DIR/logs/$label.out.log</string>
    <key>StandardErrorPath</key> <string>$JARVIS_DIR/logs/$label.err.log</string>
</dict>
</plist>
PLIST
}

unload_agent() {
    launchctl bootout "gui/$UID_NUM/$1" 2>/dev/null || true
}

if [[ "${1:-}" == "remove" ]]; then
    for label in com.jarvisai.backend com.jarvisai.ui; do
        unload_agent "$label"
        rm -f "$AGENTS_DIR/$label.plist"
    done
    echo "Jarvis launch agents removed."
    exit 0
fi

write_plist com.jarvisai.backend jarvis.py
write_plist com.jarvisai.ui jarvis_ui.py

for label in com.jarvisai.backend com.jarvisai.ui; do
    unload_agent "$label"
    launchctl bootstrap "gui/$UID_NUM" "$AGENTS_DIR/$label.plist"
done

echo "Installed. Jarvis now starts at login and restarts on crash."
echo "Logs: $JARVIS_DIR/logs/  |  Remove with: $0 remove"
