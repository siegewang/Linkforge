# Desktop Protocol Handlers Setup Guide

This guide explains how to enable direct 1-click launching of 3D models from your browser into **Bambu Studio** (`bambustudio://`) and **Autodesk Fusion 360** (`fusion360://`).

---

## How It Works

1. In the 3DPrintLib web dashboard, clicking **"Open in Bambu Studio"** triggers a custom URI:
   ```
   bambustudio://open?file=http://192.168.0.77:8000/api/models/12/download
   ```
2. Your client operating system catches the custom protocol and forwards the URL to a local launcher script.
3. The launcher script downloads the 3D asset into a local temporary folder (`%TEMP%\3DPrintLib` on Windows or `/tmp/3dprintlib` on Linux).
4. The launcher executes Bambu Studio (or Fusion 360) with the model file path as an argument, immediately placing the model onto the virtual build plate.

---

## 1. Windows Setup (Recommended)

A pre-packaged 1-click registration script is included in `desktop-handlers/windows/`.

### Automated 1-Click Install:
1. Open Windows File Explorer and navigate to:
   ```
   desktop-handlers\windows\
   ```
2. Right-click **`install_handlers.bat`** and choose **"Run as administrator"**.
3. Accept the UAC prompt. The script registers:
   - `bambustudio://` -> invokes `bambustudio_launcher.bat`
   - `fusion360://` -> invokes `fusion360_launcher.bat`

### Manual Registry Setup (Alternative):
If you prefer manual registry configuration:
1. Verify the paths inside `register_bambustudio_protocol.reg` and `register_fusion360_protocol.reg`.
2. Double-click the `.reg` files and confirm merging into Windows Registry (`HKCR`).

### Browser Confirmation:
The first time you click "Open in Bambu Studio" in Chrome, Edge, or Firefox:
- A prompt appears: *"Open Bambu Studio?"*
- Check the box: **"Always allow 192.168.0.77 to open links of this type in the associated app"**.
- Click **Open**.

---

## 2. Linux Setup (Ubuntu / Debian / Fedora / Arch)

A `.desktop` handler and launcher script are provided in `desktop-handlers/linux/`.

1. Open your terminal and run:
   ```bash
   cd desktop-handlers/linux
   chmod +x install_handlers.sh
   ./install_handlers.sh
   ```
2. This copies `bambustudio-launcher.sh` to `/usr/local/bin/` and registers `bambustudio-handler.desktop` using `xdg-mime default bambustudio-handler.desktop x-scheme-handler/bambustudio`.

---

## 3. macOS Setup

On macOS, custom URL schemes can be registered via an AppleScript application bundle:
1. Open **Script Editor** on macOS.
2. Open `desktop-handlers/macos/BambuStudioHandler.applescript`.
3. Export as an **Application** (`BambuStudioHandler.app`).
4. In `Info.plist` of the exported app, add the `CFBundleURLTypes` entry specifying `bambustudio` as the URL scheme.

---

## Direct Browser Download Fallback

If you are browsing the library from a device without desktop handlers configured (such as a tablet or guest computer), every model card and table row includes a direct **Download** button that downloads the `.stl`, `.3mf`, `.obj`, or `.step` file through standard HTTP.
