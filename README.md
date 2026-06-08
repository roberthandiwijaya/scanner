# Package Video Recorder

Lightweight local app for recording packing and returned-package unboxing videos.

The app is designed for old Windows PCs and barcode scanner workflows:

- Scan invoice or shipping receipt into the form.
- Choose `Packing` or `Return unboxing`.
- Start recording from the webcam.
- Stop, retry if needed, then save.
- Or upload an existing packing / return unboxing video without using the webcam.
- Search saved videos by invoice or shipping receipt.
- Review scanned shipping label log details.
- Delete old recordings from the UI.

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
http://192.168.1.50:8000
```

Replace `192.168.1.50` with the host computer's actual IP address.

If the page does not load, allow inbound TCP traffic on port `8000` in Windows Firewall on the host computer:

```powershell
New-NetFirewallRule -DisplayName "Scanner App 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

Important: browsers usually allow camera recording only on `localhost` or HTTPS. Other LAN users can open the app to search, watch, delete, or upload existing videos, but recording from their own webcam over `http://host-ip:8000` may be blocked unless HTTPS is configured.

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

## Configuration

Environment variables:

- `DATA_DIR`: where database and videos are stored inside the container.
- `MAX_UPLOAD_MB`: max video upload size, default `512`.

In Docker Compose, `./data` on the host is mounted to `/app/data` in the container.

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
- Add optional maximum recording duration.
- Add external drive backup.
