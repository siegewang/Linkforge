#!/usr/bin/env bash
# Bambu Studio Desktop URI Handler for Linux
# Handles: bambustudio://open?file=http://...

URI="$1"
if [ -z "$URI" ]; then
    echo "No URI provided"
    exit 1
fi

# Extract file url
FILE_URL=$(echo "$URI" | sed -E 's/.*[?&]file=([^&]+).*/\1/')
if [ -z "$FILE_URL" ] || [ "$FILE_URL" = "$URI" ]; then
    FILE_URL=$(echo "$URI" | sed -E 's/^bambustudio:\/\/(open\?file=)?//')
fi

# Decode URL (if needed)
FILE_URL=$(python3 -c "import urllib.parse, sys; print(urllib.parse.unquote(sys.argv[1]))" "$FILE_URL" 2>/dev/null || echo "$FILE_URL")

TEMP_DIR="/tmp/3dprintlib"
mkdir -p "$TEMP_DIR"

FILENAME=$(basename "${FILE_URL%%\?*}")
if [ -z "$FILENAME" ] || [ "$FILENAME" = "download" ]; then
    FILENAME="model_$(date +%s).3mf"
fi

DEST_FILE="$TEMP_DIR/$FILENAME"
echo "Downloading $FILE_URL to $DEST_FILE..."
curl -sSL -o "$DEST_FILE" "$FILE_URL"

# Search for Bambu Studio binary or AppImage
BAMBU_BIN=$(which bambu-studio 2>/dev/null || which BambuStudio 2>/dev/null || find /opt ~/Applications ~/.local/bin -name "*BambuStudio*.AppImage" 2>/dev/null | head -n 1)

if [ -n "$BAMBU_BIN" ] && [ -x "$BAMBU_BIN" ]; then
    "$BAMBU_BIN" "$DEST_FILE" &
else
    # Fallback to xdg-open
    xdg-open "$DEST_FILE" &
fi
