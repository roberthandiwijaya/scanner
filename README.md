# Package Video Recorder

Lightweight local app for recording packing and returned-package unboxing videos.

The app is designed for old Windows PCs and barcode scanner workflows:

- Scan invoice or shipping receipt into the form.
- Choose `Packing` or `Return unboxing`.
- Start recording from the webcam.
- Stop, retry if needed, then save.
- Or upload an existing packing / return unboxing video without using the webcam.
- Search saved videos by invoice or shipping receipt.

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

Webcam access works on `localhost` in modern browsers. If you open this app from another computer on the network by IP address, browsers may block the camera unless HTTPS is configured.

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

- Delete recordings from the UI.
- Export recordings by date range.
- Add operator name.
- Add optional maximum recording duration.
- Add external drive backup.
