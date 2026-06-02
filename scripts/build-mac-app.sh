#!/usr/bin/env bash
set -euo pipefail

# Build a macOS .app bundle for tmux-kanban
# Output: tmux-kanban.app (placed next to this project)

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOGO="$PROJECT_DIR/assets/logo.png"
APP_DIR="$PROJECT_DIR/tmux-kanban.app"
ICONSET_DIR="$PROJECT_DIR/tmux-kanban.iconset"
PORT="${TMUX_KANBAN_PORT:-59235}"

echo "==> Building tmux-kanban.app (port: $PORT)"

# -------------------------------------------------------------------
# 1. Create .icns from logo.png via iconset
# -------------------------------------------------------------------
echo "  -> Generating icon..."

rm -rf "$ICONSET_DIR"
mkdir -p "$ICONSET_DIR"

# Required sizes for .icns (in pixels @1x and @2x)
resize() { sips -z "$1" "$1" "$LOGO" --out "$ICONSET_DIR/$2" > /dev/null 2>&1; }
resize 16   icon_16x16.png
resize 32   icon_16x16@2x.png
resize 32   icon_32x32.png
resize 64   icon_32x32@2x.png
resize 128  icon_128x128.png
resize 256  icon_128x128@2x.png
resize 256  icon_256x256.png
resize 512  icon_256x256@2x.png
resize 512  icon_512x512.png
resize 1024 icon_512x512@2x.png

iconutil -c icns "$ICONSET_DIR" -o "$PROJECT_DIR/assets/tmux-kanban.icns"
rm -rf "$ICONSET_DIR"
echo "  -> Icon created: assets/tmux-kanban.icns"

# -------------------------------------------------------------------
# 2. Build .app bundle
# -------------------------------------------------------------------
echo "  -> Building .app bundle..."

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# Copy icon
cp "$PROJECT_DIR/assets/tmux-kanban.icns" "$APP_DIR/Contents/Resources/"

# Resolve tmux-kanban binary path (Finder has minimal PATH)
TKB_BIN="$(which tmux-kanban 2>/dev/null || true)"

# -------------------------------------------------------------------
# 2a. Launcher script (runs tmux-kanban, opens browser, stays alive)
# -------------------------------------------------------------------
cat > "$APP_DIR/Contents/MacOS/tmux-kanban" << LAUNCHER
#!/usr/bin/env bash
export PATH="$HOME/.local/bin:$HOME/miniforge3/bin:$HOME/miniconda3/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

TKB="${TKB_BIN:-tmux-kanban}"
PORT="\${TMUX_KANBAN_PORT:-59235}"
URL="http://127.0.0.1:\${PORT}"
PID_FILE="\$HOME/.tmux-kanban/server.pid"
LOG_FILE="\$HOME/.tmux-kanban/server.log"

if [ -f "\$PID_FILE" ]; then
  OLD_PID=\$(cat "\$PID_FILE")
  if kill -0 "\$OLD_PID" 2>/dev/null; then
    osascript -e "display notification \"Already running on port \${PORT}\" with title \"tmux-kanban\"" 2>/dev/null
    open "\$URL"
    exit 0
  fi
  rm -f "\$PID_FILE"
fi

mkdir -p "\$HOME/.tmux-kanban"
"\$TKB" --port "\$PORT" > "\$LOG_FILE" 2>&1 &
SERVER_PID=\$!
echo "\$SERVER_PID" > "\$PID_FILE"

for i in \$(seq 1 25); do
  if curl -s -o /dev/null "\$URL/api/auth/status" 2>/dev/null; then
    break
  fi
  sleep 0.2
done

open "\$URL"

cleanup() {
  kill "\$SERVER_PID" 2>/dev/null || true
  rm -f "\$PID_FILE"
}
trap cleanup EXIT INT TERM

wait "\$SERVER_PID" 2>/dev/null
rm -f "\$PID_FILE"
LAUNCHER

chmod +x "$APP_DIR/Contents/MacOS/tmux-kanban"

# -------------------------------------------------------------------
# 2b. Info.plist
# -------------------------------------------------------------------
cat > "$APP_DIR/Contents/Info.plist" << INFO
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>tmux-kanban</string>
    <key>CFBundleExecutable</key>
    <string>tmux-kanban</string>
    <key>CFBundleIconFile</key>
    <string>tmux-kanban</string>
    <key>CFBundleIdentifier</key>
    <string>io.github.linwk20.tmux-kanban</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>tmux-kanban</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
</dict>
</plist>
INFO

echo ""
echo "  Done! App created at: $APP_DIR"
echo ""
echo "  To install, drag tmux-kanban.app to /Applications:"
echo "    cp -r \"$APP_DIR\" /Applications/"
echo ""
echo "  Or use it directly:"
echo "    open \"$APP_DIR\""
echo ""
