# Package Video Recorder

Lightweight local app for recording packing and returned-package unboxing videos.

The app is designed for old Windows PCs and barcode scanner workflows:

- Scan invoice or shipping receipt into the form.
- Choose `Packing` or `Return unboxing`.
- Start recording from the webcam.
- Stop, retry if needed, then save.
- Optionally record microphone audio with the webcam video.
- Or upload an existing packing / return unboxing video without using the webcam.
- Search saved videos by invoice or shipping receipt and date range.
- Review scanned shipping label log details.
- Convert saved `.webm` recordings to `.mp4`.
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

Turn on `Record audio` before starting if microphone audio is needed. The live webcam preview stays muted to avoid echo, but the saved recording will include audio when the browser has microphone permission.

Turn on `Start on Enter` only when you want a barcode scan with an Enter suffix to start recording automatically.

Use `Stop webcam preview` to turn off the live camera preview after recording or when the workstation is idle. The next `Start recording` action will request and show the camera again.

HTTPS is also available through Caddy:

```text
https://<host-ip>:8443
```

Caddy accepts private LAN IP addresses dynamically. If the host computer's LAN IP changes, use the new IP address with the same `:8443` port; no code or rebuild is needed.

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

If the host computer's IP changes later, use the new `IPv4 Address` from `ipconfig`; no code or `LAN_HOST` setting needs to be changed. If Docker Desktop does not immediately accept the new address, restart the stack with `docker compose up -d`.

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

Without trusting that certificate, the browser may show a warning and may still block webcam access. After the root certificate is trusted, Caddy can create certificates for new private LAN IP addresses automatically.

## Barcode Scanner Notes

The Zebra LS2208 usually works as a USB HID keyboard. Put the cursor in the invoice field and scan.

When the scanner sends Enter after a shipping receipt, the dashboard looks up the shipment and opens an order-details popup. The popup shows the order number, package number, carrier, product variations, SKUs, quantities, and product images. The operator must confirm whether the details match the products collected from the shelf. When `Start on Enter` is enabled, recording waits for a matching confirmation; choosing `Does not match` prevents the automatic recording from starting.

The lookup is sent through the Flask server so the API key is never exposed to browser JavaScript. Create `.env` from `.env.example` and set:

```text
ORDER_LOOKUP_API_KEY=your-api-key
```

Restart the recorder after changing this setting:

```powershell
docker compose up -d --build recorder
```

Recommended scanner setting:

- Keep `Start on Enter` off if scanning should only fill the invoice field.
- Turn on `Start on Enter` if the scanner sends an `Enter` suffix and you want scanning to immediately start recording.
- Do not add random prefixes/suffixes unless your invoice format needs them.

## Upload Existing Videos

If the video was recorded outside the app:

1. Scan or type the invoice / shipping receipt.
2. Choose `Packing` or `Return unboxing`.
3. Select the video file in `Upload existing video`.
4. Click `Upload video`.

Supported file extensions include `.webm`, `.mp4`, `.mov`, `.m4v`, `.avi`, and `.mkv`.

## Recording Format

Webcam recordings are saved as `.webm`. The browser recorder tries WebM with VP9 first, then VP8, then generic WebM. When `Record audio` is enabled, it also asks the browser for Opus audio in the WebM file.

WebM is a good fit for this local recorder because it is well supported by browser recording APIs and can keep file sizes smaller at similar quality. MP4 is still more widely compatible for playback outside browsers, so uploaded existing videos may still use common formats like `.mp4`, `.mov`, `.avi`, and `.mkv`.

The Docker image includes `ffmpeg` so saved `.webm` recordings can be remuxed after upload. This repairs duration and seek metadata that some browsers omit, which helps the playback progress bar match the real video position.

The app validates saved videos before adding them to the log. Webcam recordings are saved through a temporary file first, checked for a valid `.webm` header, readable video stream, and matching duration, then moved into the final video folder only after those checks pass. If the browser produces an incomplete file, the app rejects it and asks the operator to retry instead of saving a broken video.

## Convert WebM To MP4

Open `Convert` from the top menu to convert saved `.webm` recordings to `.mp4`.

The converter uses `ffmpeg` inside the Docker container. It supports:

- Source, 1080p, 720p, 480p, and 360p output resolution.
- Small file, standard, and high quality settings.
- Estimated MP4 file size before conversion.
- Percentage progress while FFmpeg is converting.
- MP4 preview and download after conversion.

Converted files are stored in:

```text
data/converted/YYYY-MM/
```

The estimate is based on the selected video/audio bitrate and source duration. The final file size can be slightly different because video content compresses differently.

Only one MP4 conversion runs at a time. Extra conversion requests wait in a queue, and FFmpeg is limited to 2 threads to reduce lag while the app is also recording or serving users.

## Scanned Label Log

The search panel shows a log of scanned shipping labels and their saved recording details:

- Label / invoice value.
- Packing or return type.
- Saved date and time.
- Video format.
- File size.
- Video preview.
- Download action.
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

The script waits for Docker Desktop and runs:

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
