# Package Video Recorder

Lightweight local app for recording packing and returned-package unboxing videos.

The app is designed for old Windows PCs and barcode scanner workflows:

- Scan invoice or shipping receipt into the form.
- Choose `Packing` or `Return unboxing`.
- Start recording from the webcam.
- Stop, retry if needed, then save.
- Or upload an existing packing / return unboxing video without using the webcam.
- Search saved videos by invoice or shipping receipt and date range.
- Review scanned shipping label log details.
- Delete old recordings from the UI.
- See dashboard totals, daily scan counts, and storage usage.
- Get duplicate label warnings before recording.
- Use light or dark mode.

## Recommended Strategy

Use this as a local web app, not a heavy desktop app.

Why:

- The webcam can be handled directly by the browser using `MediaRecorder`.
- Existing videos can be uploaded through the same browser form.
- Zebra LS2208 scanners normally behave like a keyboard, so no special driver code is needed.
- Docker keeps setup repeatable.
- Python + Flask is small and easy to maintain.
- SQLite is enough for metadata because videos are stored as normal local files.

The app stores:

- Metadata in `data/recordings.sqlite3`.
- Videos in `data/videos/YYYY-MM/`.

## Run With Docker

```powershell
docker compose up --build
```

Open:

```text
http://localhost:8000
```

Webcam access works on `localhost` in modern browsers.

The camera has a short countdown before recording starts, so the operator has a moment to position the package or label.

Use `Stop webcam preview` to turn off the live camera preview after recording or when the workstation is idle. The next `Start recording` action will request and show the camera again.

HTTPS is also available through Caddy:

```text
https://192.168.100.13:8443
```

If the host computer's LAN IP changes, set `LAN_HOST` to the new IP before starting Docker Compose:

```powershell
$env:LAN_HOST="192.168.100.13"
docker compose up --build
```

## Access From Another Computer On The LAN

Start the app on the host computer:

```powershell
docker compose up --build
```

Find the host computer's LAN IP address:

```powershell
ipconfig
```

Look for the `IPv4 Address`, for example:

```text
192.168.1.50
```

From another computer on the same network, open:

```text
https://<host-ip>:8443
```

For this host computer right now, that is:

```text
https://192.168.100.13:8443
```

If the page does not load, allow inbound TCP traffic on port `8443` in Windows Firewall on the host computer:

```powershell
New-NetFirewallRule -DisplayName "Scanner HTTPS 8443" -Direction Inbound -Protocol TCP -LocalPort 8443 -Action Allow
```

Important: browsers usually allow camera recording only on `localhost` or HTTPS. Other LAN users should use the HTTPS address if they need to record from their own webcam.

The first HTTPS setup uses Caddy's internal certificate authority. On each LAN computer that needs to use the camera, install/trust this certificate after Caddy has started:

```text
data/caddy/data/caddy/pki/authorities/local/root.crt
```

For example, copy `root.crt` to the client computer and run:

```powershell
certutil -user -addstore Root root.crt
```

Without trusting that certificate, the browser may show a warning and may still block webcam access.

## Barcode Scanner Notes

The Zebra LS2208 usually works as a USB HID keyboard. Put the cursor in the invoice field and scan.

Recommended scanner setting:

- Add an `Enter` suffix after scan if you want scanning to immediately start recording.
- Do not add random prefixes/suffixes unless your invoice format needs them.

## Upload Existing Videos

If the video was recorded outside the app:

1. Scan or type the invoice / shipping receipt.
2. Choose `Packing` or `Return unboxing`.
3. Select the video file in `Upload existing video`.
4. Click `Upload video`.

Supported file extensions include `.webm`, `.mp4`, `.mov`, `.m4v`, `.avi`, and `.mkv`.

## Recording Format

Webcam recordings are saved as `.webm`. The browser recorder tries WebM with VP9 first, then VP8, then generic WebM.

WebM is a good fit for this local recorder because it is well supported by browser recording APIs and can keep file sizes smaller at similar quality. MP4 is still more widely compatible for playback outside browsers, so uploaded existing videos may still use common formats like `.mp4`, `.mov`, `.avi`, and `.mkv`.

## Scanned Label Log

The search panel shows a log of scanned shipping labels and their saved recording details:

- Label / invoice value.
- Packing or return type.
- Saved date and time.
- Video format.
- File size.
- Video preview.
- Delete action.

The log supports:

- Date range filtering.
- Pagination for large history lists.
- Duplicate label warnings when the same invoice / shipping receipt already exists.
- Clear loading and empty states.

## Dashboard

The dashboard shows:

- Total saved videos.
- Total storage used.
- Today's scan count.
- Daily scan counts for the last 7 days.

The UI also includes a dark mode toggle for low-light work areas.

## Configuration

Environment variables:

- `DATA_DIR`: where database and videos are stored inside the container.
- `MAX_UPLOAD_MB`: max video upload size, default `512`.

In Docker Compose, `./data` on the host is mounted to `/app/data` in the container.

## Windows Auto Start

Both Docker Compose services use `restart: unless-stopped`. That means Docker will restart them when Docker Desktop starts, unless you manually stopped them.

This repo also includes:

```text
scripts/start-scanner.ps1
```

The script waits for Docker Desktop, detects the host computer's LAN IP, sets `LAN_HOST`, and runs:

```powershell
docker compose up -d
```

To run it automatically when this Windows user logs in, create a shortcut in:

```text
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

The shortcut should run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\rober\Documents\scanner\scanner\scripts\start-scanner.ps1"
```

Startup logs are written to:

```text
data/startup.log
```

## Hardware Guidance

For an i3-3xxx computer:

- Prefer 720p video.
- Use a wired USB webcam if possible.
- Keep the app open locally on the same PC as the webcam.
- Use an SSD if video search and saving feels slow.

## Future Improvements

Useful next features:

- Export recordings by date range.
- Add operator name.
- Add external drive backup.
