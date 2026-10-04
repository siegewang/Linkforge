#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
echo "Installing Bambu Studio URI Handler..."

sudo cp "$DIR/bambustudio-launcher.sh" /usr/local/bin/bambustudio-launcher.sh
sudo chmod +x /usr/local/bin/bambustudio-launcher.sh

mkdir -p ~/.local/share/applications
cp "$DIR/bambustudio-handler.desktop" ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/ 2>/dev/null || true
xdg-mime default bambustudio-handler.desktop x-scheme-handler/bambustudio

echo "Successfully registered bambustudio:// protocol handler on Linux!"
