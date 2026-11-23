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

## Scan History

Open **History** in the navigation (or `/history`) for a searchable table of submitted labels. Each submission is retained, including repeated labels, unsuccessful order lookups, and scans without a saved video. Expand Products for names, variations, quantities, and SKUs. Filter by label/order, UTC date range, mode, lookup result, or video status.

Deleting a recording still removes its original video, but its scan history and order details remain. Video status distinguishes Available, Not recorded, Deleted, and Missing file. Available videos have View and Download links; history does not load all videos in the table.

On first startup after this update, existing saved recordings are imported once. Imported rows show the recording's **save time**, not an original scan timestamp. Past scans that were never saved and recordings already deleted before the update cannot be recovered. New scan history requires a successful connection to the app; a persistence error is shown rather than silently claiming the scan was logged.

The existing Dashboard list and totals still describe saved recordings. Scan history is separate and is not erased by video cleanup. Back up `data/recordings.sqlite3` as well as any videos you need to retain.

## Verified Archive (another hard drive or NAS)

Open **Archive** (`/archive`) to back up older saved recordings and their completed converted MP4 copies, then optionally remove local copies. Archiving is disabled until a separate backup destination is configured. The regular scanner works without a backup drive.

### One-click cleanup: only recordings older than three calendar months

The automatic action on Archive authorizes **backup → verification → permanent local removal** in one run, without per-batch confirmation. Its button explicitly states that verified local copies will be removed. It never runs on a schedule or starts automatically just because you open the page.

The server freezes a UTC cutoff at run creation by subtracting three calendar months from the current date/time (clamping end-of-month dates). Only recordings saved **strictly before** that cutoff qualify; newer recordings and boundary timestamps are excluded. The browser cannot supply a wider cutoff or arbitrary recording selection. Matching converted MP4s are included, history remains Archived, and busy recordings are skipped.

One background worker processes one recording at a time. A selectable 1–20 MiB/s limit (5 MiB/s default) throttles file copy and checksum reads; the UI offers 2, 5, 10, and 20. Automatic disk work yields while recording uploads/saves are active. This reduces contention but cannot guarantee zero latency from disks, antivirus, OS caches, or other processes. Lower the limit during busy hours; avoid heavy conversion workloads.

Use **Pause / Resume / Stop** to control a run. Progress and authorization are persisted. Restarted runs require explicit Resume before automatic cleanup continues. Stop is cooperative: already removed files remain archived; it never restores deleted local copies. A backup/checksum failure never authorizes removal of that recording. Do not change the destination during an unfinished automatic run. Separate manual archive jobs still use their original two-step confirmation flow and 50-recording limit.

### Configure another hard drive from the website

One-time Docker setup is necessary to expose the other physical drive. The website cannot mount Windows drives itself.

1. Create a dedicated folder on the other hard drive, for example `D:/ScannerBackups` (use your actual drive; do not choose another partition on the recording disk).
2. Copy `docker-compose.drive.yml.example` to `docker-compose.drive.yml`. Set `BACKUP_DRIVE_PATH=D:/ScannerBackups` in your local `.env`. The bind mount requires the folder to exist and does not create a host fallback folder.
3. Start with `docker compose -f docker-compose.yml -f docker-compose.drive.yml up -d --build`. This mounts the dedicated drive folder at `/backup-drive` and sets `ARCHIVE_SETTINGS_ROOT`.
4. Open **Archive → Configure backup drive / archive settings**, or `/archive/settings`. Enter a friendly name and a relative folder such as `ScannerArchive`. Choose **Save and test destination**. With your explicit confirmation, the app creates that subfolder and a unique identity file, tests writing/no-overwrite file publication, and saves settings in SQLite. It does not copy or delete recordings.
5. **Test existing folder** checks access and reports free space. No arbitrary Windows paths, parent traversal, or symlink folders are accepted. The UI can only choose folders beneath the configured mounted root.

Website settings survive restarts. Unfinished jobs must be completed or cancelled before changing destination. Each job retains its destination identity and container path; cleanup is refused after a configuration mismatch. Returning to an older backup folder requires selecting its original path, identity, and label. Never replace its identity file. A folder on a different physical disk is required for genuine backup protection; Docker cannot reliably infer physical disks from drive letters.

The following NAS setup is optional and is not needed for a second local hard drive.

### Two separate steps

1. Choose **Saved on or before (UTC)**, preview, and select up to 50 recordings. Choose **Back up and verify**. The job copies originals and all matching completed MP4 outputs, independently checks SHA-256 and sizes, and publishes a portable manifest. **Backup does not remove local files.**
2. When the job says **Backup verified — local files retained**, choose **Review local removal**. Confirm the displayed recording/file count and bytes. The app rechecks the exact manifest, unchanged sources, and NAS copies before removal. Converted copies are removed first; an MP4 failure retains the original. History then shows **Archived** with the stored destination/location, without a broken local playback link.

Busy conversions/deletions/archives block conflicting work. Changed files require a new verified backup; new MP4 files are not silently included in cleanup. Interrupted cleanup does not automatically resume after a restart: review the job and explicitly resume. An inactive, unconfirmed backup can be cancelled to release its recordings; cancellation retains local files and any NAS copies. Confirmed removal jobs cannot be cancelled this way. Do not manually clear reservations or edit job tables to bypass a safety error. If a conversion or ordinary deletion was interrupted and its recording remains reserved, stop work on that recording and obtain administrator review; this version deliberately does not automatically clear uncertain reservations. This version supports one app process with one archive worker; do not run multiple recorder instances against the same database.

### Windows Docker Desktop setup

Keep `data/recordings.sqlite3` and working videos on the local disk. Use a **separate NAS-backed volume** for `/archive`. A Windows Explorer mapped drive (`Z:`) or a UNC string is not automatically accessible inside a Linux container.

1. Have the NAS/Docker administrator provision an SMB/CIFS-backed Docker volume, or a tested host/WSL mount exposed to Docker. It must really be on the NAS, not a Docker-local fallback volume. Keep credentials in administrator-managed secret/config storage, not source files, screenshots, browser requests, or chat. SMB version, user permissions, and mount options depend on your NAS and Docker Desktop setup.
2. In a dedicated directory on that share, provision a text file named `.scanner-archive-id` containing a unique UUID. Set `ARCHIVE_DESTINATION_ID` to the same value. The app **never creates the archive root or this sentinel**. A missing/mismatched sentinel disables operations; the sentinel checks identity, not redundancy or guaranteed future availability.
3. Copy `docker-compose.archive.yml.example` to the gitignored `docker-compose.archive.yml`. Set `ARCHIVE_VOLUME_NAME` to the existing NAS-backed volume name, `ARCHIVE_LABEL` to a friendly display name, and `ARCHIVE_DESTINATION_ID` in the local `.env` or deployment environment. Do not place SMB passwords in this override.
4. Start with the optional override:

   ```powershell
   docker compose -f docker-compose.yml -f docker-compose.archive.yml up -d --build
   ```

5. Test with disposable videos first. Verify the backup files can be opened from a second NAS client before the first production cleanup. The archive publisher uses no-overwrite hard-link publication; the mounted share must support that operation. Unsupported NAS/filesystem behavior fails safely rather than overwriting a target or allowing cleanup. Actual NAS compatibility must be tested on your deployment.

Do not expose this local app directly to the public internet. Same-origin checks and per-job removal confirmation tokens prevent accidental/cross-site cleanup but do not replace authentication, NAS permissions, or trusted LAN access.

### Recovery and database backup

Portable job manifests contain recording/history metadata and checksums; keep them with the archived files. An offline NAS does not erase Archived history. Automatic restore and NAS streaming are not included; use the recorded relative paths to locate archived media manually.

**Video archiving does not back up the live SQLite database. Never copy an actively written SQLite file as a backup.** Use SQLite's online backup API to produce a consistent local snapshot, then copy that snapshot to separate storage and independently verify its SHA-256. For example, with the recorder stopped you can copy its database and video directories together; for an online backup, a Python maintenance script should open the live database with `sqlite3.connect`, open a new destination snapshot, call `source.backup(destination)`, and close both before transferring the snapshot. Keep snapshots private: they contain label history and application tokens. Retain multiple backup versions, and test restoration before relying on them.

Checksums establish matching bytes at verification time; they do not guarantee a NAS stays connected or that it cannot lose data afterward. Network validation and local unlink cannot be one atomic operation. Maintain another backup of important evidence.

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

Turn on `Start after match` when recording should begin after the operator confirms the product details.

Use `Stop webcam preview` to turn off the live camera preview after recording or when the workstation is idle. The next `Start recording` action will request and show the camera again.

HTTPS is also available through Caddy:

```text
https://<host-ip>:9443
```

Caddy accepts private LAN IP addresses dynamically. If the host computer's LAN IP changes, use the new IP address with the same `:9443` port; no code or rebuild is needed.

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
https://<host-ip>:9443
```

If the host computer's IP changes later, use the new `IPv4 Address` from `ipconfig`; no code or `LAN_HOST` setting needs to be changed. If Docker Desktop does not immediately accept the new address, restart the stack with `docker compose up -d`.

If the page does not load, allow inbound TCP traffic on port `9443` in Windows Firewall on the host computer:

```powershell
New-NetFirewallRule -DisplayName "Scanner HTTPS 9443" -Direction Inbound -Protocol TCP -LocalPort 9443 -Action Allow
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

## Scan And Record From A Phone

Use a current Android Chrome or iPhone Safari on the same trusted LAN as the host. Open `https://<host-ip>:9443`; `localhost` on the phone refers to the phone, not the host. The external HTTPS port is `9443`; Caddy's internal container listener remains `8443`.

Before using either camera, trust the host's local CA on the phone. Transfer **only** `data/caddy/data/caddy/pki/authorities/local/root.crt` from your own host, never `root.key`, intermediate keys, or any other private key. On Android, install it as a CA certificate in the device's security/certificate settings (wording varies by device). On iPhone, install the certificate profile, then enable **full trust** for it in **Settings â†’ General â†’ About â†’ Certificate Trust Settings**. Reopen the HTTPS page and grant camera permission. A warning bypass alone is not a substitute for trusting the CA.

Installing a CA is a security choice: the device will trust certificates issued by that CA, not just this app. Only install a CA you control and whose source you verified; protect the host's private keys and remove the trusted certificate when no longer needed. Do not install an unknown CA just to dismiss a browser warning.

1. Choose `Packing` or `Return unboxing`.
2. Tap **Scan with camera**, select the **rear** camera for the label (or **front** if needed), and hold the label steady in view.
3. Supported labels are QR codes containing **plain tracking text** (not a URL or structured payload), and linear **Code 128**, **Code 39**, and **ITF** barcodes. You can still type the tracking value or use a keyboard scanner when a label cannot be decoded.
4. Review the shipment details and confirm the products match. Use `Start after match` if recording should begin on confirmation; otherwise start recording yourself.
5. Record, stop, review, and save as usual. Keep the browser in the **foreground** and the phone **unlocked** throughout scanning, recording, and saving; backgrounding or locking the phone can interrupt camera capture.

## Barcode Scanner Notes

The Zebra LS2208 usually works as a USB HID keyboard. Put the cursor in the invoice field and scan.

Invoice and shipping receipt values are normalized to uppercase in the scanner field, API lookup, and database. Existing lowercase database entries are migrated to uppercase automatically when the application starts; recording filenames are left unchanged.

After the shipping receipt field stops receiving scanner input for a short moment, the dashboard automatically looks up the shipment and opens an order-details popup. Enter also opens it immediately when the scanner sends an Enter suffix. The popup shows the order number, package number, carrier, product variations, SKUs, quantities, and product images. The operator confirms that the details match the products collected from the shelf. When `Start after match` is enabled, clicking `Product details match` starts the recording; otherwise it confirms the order and closes the popup without starting.

The lookup is sent through the Flask server so the API key is never exposed to browser JavaScript. Create `.env` from `.env.example` and set:

```text
ORDER_LOOKUP_API_KEY=your-api-key
```

Restart the recorder after changing this setting:

```powershell
docker compose up -d --build recorder
```

Recommended scanner setting:

- Keep `Start after match` off when confirming the product should not start recording.
- Turn on `Start after match` when recording should begin immediately after a matching confirmation.
- Do not add random prefixes/suffixes unless your invoice format needs them.

## Upload Existing Videos

If the video was recorded outside the app:

1. Scan or type the invoice / shipping receipt.
2. Choose `Packing` or `Return unboxing`.
3. Select the video file in `Upload existing video`.
4. Click `Upload video`.

Supported file extensions include `.webm`, `.mp4`, `.mov`, `.m4v`, `.avi`, and `.mkv`.

## Recording Format

Camera recordings use native **WebM or MP4**, selected at runtime according to the browser's `MediaRecorder` support. Android Chrome commonly supports WebM; iPhone Safari commonly supports MP4. Exact containers, codecs, and optional audio depend on the browser/device version, so neither format is guaranteed on every device. The saved filename and MIME type follow the selected recording format; native MP4 does not require the WebM-to-MP4 converter.

WebM can keep file sizes smaller at similar quality, while MP4 is more widely compatible for playback outside browsers. Uploaded existing videos may also use common formats like `.mp4`, `.mov`, `.avi`, and `.mkv`.

The Docker image includes `ffmpeg` so saved `.webm` recordings can be remuxed after upload. This repairs duration and seek metadata that some browsers omit, which helps the playback progress bar match the real video position.

The app validates saved videos before adding them to the log. Webcam recordings are saved through a temporary file first, checked for a valid header when using `.webm`, a readable video stream, and matching duration for both WebM and MP4, then moved into the final video folder only after those checks pass. If the browser produces an incomplete file, the app rejects it and asks the operator to retry instead of saving a broken video.

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
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\scanner\scripts\start-scanner.ps1"
```

Startup logs are written to:

```text
data/startup.log
```

### Automatic LAN Port Forwarding With Kiri

Use this setup when Docker runs in the `kiri` WSL distribution on Windows. It refreshes the Windows forwarding rules for ports `9443` (HTTPS) and `8000` (HTTP) using Kiri's current IPv4 address.

1. In `docker-compose.yml`, ensure Caddy's published port is `"9443:8443"` and the recorder's is `"8000:8000"`. If you changed either mapping, apply it with `docker compose up -d`, using your usual Compose overrides.
2. Sign in to the Windows account that runs Kiri. Open **Windows PowerShell as administrator** using that same account, then change to the scanner repository directory.
3. Run the installation commands below. They copy the scripts to Program Files, register the task, and allow HTTPS from the local subnet on the Windows **Private** network profile.

```powershell
$scannerFolder = Join-Path $env:ProgramFiles 'Scanner Port Forwarding'
New-Item -ItemType Directory -Path $scannerFolder -Force | Out-Null
Copy-Item -LiteralPath '.\scripts\update-kiri-port-forwarding.ps1', '.\scripts\run-kiri-port-forwarding.vbs' -Destination $scannerFolder -Force

$taskName = 'Scanner Kiri Port Forwarding'
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$launcher = Join-Path $scannerFolder 'run-kiri-port-forwarding.vbs'
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wscript.exe" -Argument ('//B //NoLogo "' + $launcher + '"') -WorkingDirectory $scannerFolder
$logon = New-ScheduledTaskTrigger -AtLogOn -User $account
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$repeat.Repetition.StopAtDurationEnd = $false
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($logon, $repeat) -Principal $principal -Settings $settings -Force | Out-Null

$firewallRule = 'Scanner-LAN-HTTPS-9443'
if (-not (Get-NetFirewallRule -Name $firewallRule -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name $firewallRule -DisplayName 'Scanner HTTPS 9443 (Private LAN)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 9443 -Profile Private -RemoteAddress LocalSubnet | Out-Null
}
Start-ScheduledTask -TaskName $taskName
```

The task runs silently at sign-in and once a minute while that Windows account is signed in. It skips checks when Kiri is stopped and updates the rules when it finds a changed address or a missing rule. The Program Files location keeps the elevated task's scripts in an administrator-controlled folder.

In Task Scheduler, check that **Scanner Kiri Port Forwarding** has a last run result of `0x0`. With Kiri and the scanner running, `netsh interface portproxy show all` should show ports `9443` and `8000` forwarding to Kiri's current IP. From another LAN computer, open `https://<windows-lan-ip>:9443` using the Windows Ethernet or Wi-Fi IPv4 address from `ipconfig`.

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
