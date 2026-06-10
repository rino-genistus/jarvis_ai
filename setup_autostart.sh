#!/bin/bash
# Installs Jarvis as a persistent login service.
# Run once: bash setup_autostart.sh
# To uninstall: bash setup_autostart.sh --uninstall

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_PLIST="$LAUNCH_AGENTS/com.jarvisai.backend.plist"
UI_PLIST="$LAUNCH_AGENTS/com.jarvisai.ui.plist"

mkdir -p "$SCRIPT_DIR/logs"

if [[ "$1" == "--uninstall" ]]; then
    echo "Unloading Jarvis services..."
    launchctl unload "$BACKEND_PLIST" 2>/dev/null
    launchctl unload "$UI_PLIST" 2>/dev/null
    rm -f "$BACKEND_PLIST" "$UI_PLIST"
    echo "Done. Jarvis will no longer start at login."
    exit 0
fi

echo "Installing Jarvis autostart..."
cp "$SCRIPT_DIR/com.jarvisai.backend.plist" "$BACKEND_PLIST"
cp "$SCRIPT_DIR/com.jarvisai.ui.plist" "$UI_PLIST"

# Unload first in case already loaded (clean reload)
launchctl unload "$BACKEND_PLIST" 2>/dev/null
launchctl unload "$UI_PLIST" 2>/dev/null

launchctl load "$BACKEND_PLIST"
launchctl load "$UI_PLIST"

echo ""
echo "Done. Jarvis will now start automatically at every login."
echo "Logs are in $SCRIPT_DIR/logs/"
echo ""
echo "Manual controls:"
echo "  Stop:    launchctl unload $BACKEND_PLIST && launchctl unload $UI_PLIST"
echo "  Start:   launchctl load $BACKEND_PLIST && launchctl load $UI_PLIST"
echo "  Logs:    tail -f $SCRIPT_DIR/logs/backend.log"
echo "  Remove:  bash setup_autostart.sh --uninstall"
