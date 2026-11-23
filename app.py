from __future__ import annotations

import json
import math
import mimetypes
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import uuid
from ipaddress import ip_address
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request as UrlRequest, urlopen

from flask import Flask, jsonify, render_template, request, send_from_directory
from archive import ArchiveService, ReservationError, init_schema as init_archive_schema, reserve_recordings, release_recordings


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data")).resolve()
VIDEO_DIR = DATA_DIR / "videos"
CONVERTED_DIR = DATA_DIR / "converted"
DB_PATH = DATA_DIR / "recordings.sqlite3"

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "512"))
ORDER_LOOKUP_API_URL = os.environ.get(
    "ORDER_LOOKUP_API_URL",
    "https://shopee.techly.id/api/orders/by-tracking-number",
).strip()
ORDER_LOOKUP_API_KEY = os.environ.get("ORDER_LOOKUP_API_KEY", "").strip()
ORDER_LOOKUP_TIMEOUT_SECONDS = 10
ALLOWED_MODES = {"packing", "return"}
ALLOWED_VIDEO_EXTENSIONS = {".webm", ".mp4", ".mov", ".m4v", ".avi", ".mkv"}
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50
CONVERT_RESOLUTIONS = {
    "source": {"label": "Original resolution", "height": None},
    "1080p": {"label": "1080p", "height": 1080},
    "720p": {"label": "720p", "height": 720},
    "480p": {"label": "480p", "height": 480},
    "360p": {"label": "360p", "height": 360},
}
CONVERT_QUALITIES = {
    "small": {"label": "Small file", "video_bitrate_kbps": 900, "audio_bitrate_kbps": 96, "preset": "veryfast"},
    "standard": {"label": "Standard", "video_bitrate_kbps": 1800, "audio_bitrate_kbps": 128, "preset": "veryfast"},
    "high": {"label": "High quality", "video_bitrate_kbps": 3500, "audio_bitrate_kbps": 160, "preset": "fast"},
}
CONVERSION_JOBS: dict[str, dict[str, Any]] = {}
CONVERSION_JOBS_LOCK = threading.Lock()
CONVERSION_QUEUE: queue.Queue[tuple[str, int, str, str]] = queue.Queue()
CONVERSION_WORKER_LOCK = threading.Lock()
CONVERSION_WORKER_STARTED = False
ACTIVE_UPLOADS = 0
ACTIVE_UPLOADS_LOCK = threading.Lock()



app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


@app.before_request
def track_recording_upload():
    global ACTIVE_UPLOADS
    # Mark busy before accessing request.form: multipart parsing also writes disk.
    if request.method == 'POST' and request.path == '/api/recordings':
        request.environ['scanner.upload_counted'] = True
        with ACTIVE_UPLOADS_LOCK:
            ACTIVE_UPLOADS += 1


@app.teardown_request
def finish_recording_upload(error=None):
    global ACTIVE_UPLOADS
    if request.environ.pop('scanner.upload_counted', False):
        with ACTIVE_UPLOADS_LOCK:
            ACTIVE_UPLOADS = max(0, ACTIVE_UPLOADS - 1)


def recording_io_busy():
    with ACTIVE_UPLOADS_LOCK:
        return ACTIVE_UPLOADS > 0


def init_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, factory=StorageConnection) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice TEXT NOT NULL,
                mode TEXT NOT NULL,
                filename TEXT NOT NULL UNIQUE,
                original_name TEXT,
                content_type TEXT,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_recordings_invoice ON recordings(invoice)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_recordings_created_at ON recordings(created_at)")
        db.execute("UPDATE recordings SET invoice = UPPER(invoice) WHERE invoice <> UPPER(invoice)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS scan_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id TEXT NOT NULL UNIQUE,
                mutation_token TEXT NOT NULL,
                tracking_number TEXT NOT NULL,
                mode TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                source TEXT NOT NULL,
                timestamp_basis TEXT NOT NULL,
                lookup_status TEXT NOT NULL DEFAULT 'not_requested',
                lookup_error TEXT,
                lookup_http_status INTEGER,
                order_snapshot TEXT,
                recording_id INTEGER UNIQUE,
                recording_snapshot TEXT,
                video_deleted INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_scan_events_date ON scan_events(submitted_at DESC, id DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_scan_events_label ON scan_events(tracking_number)")
        # Retain recording IDs after deletion so startup never resurrects imports.
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM recordings WHERE id NOT IN (SELECT recording_id FROM scan_events WHERE recording_id IS NOT NULL)").fetchall()
        for row in rows:
            db.execute("""
                INSERT OR IGNORE INTO scan_events
                    (submission_id, mutation_token, tracking_number, mode, submitted_at,
                     source, timestamp_basis, recording_id, recording_snapshot)
                VALUES (?, ?, ?, ?, ?, 'imported_recording', 'saved_at', ?, ?)
            """, (f"legacy-recording:{row['id']}", uuid.uuid4().hex,
                  row["invoice"], row["mode"], row["created_at"], row["id"],
                  json.dumps(row_to_dict(row))))
        init_archive_schema(db)


class StorageConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def db_connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, factory=StorageConnection)
    db.row_factory = sqlite3.Row
    db.create_function("video_exists", 1, video_exists)
    # Do not require SQLite's optional JSON1 extension (older Windows builds).
    db.create_function("order_field", 2, lambda value, key: json.loads(value or "{}").get(key, ""))
    return db


def video_exists(filename: str | None) -> int:
    if not filename:
        return 0
    try:
        return int(recording_path(filename).is_file())
    except (ValueError, OSError):
        return 0


def sanitize_invoice(value: str) -> str:
    value = value.strip()
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^A-Za-z0-9._-]", "", value)
    return value[:80].upper()


def validate_tracking_number(value: str) -> str | None:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value):
        return None
    return value.upper()


def safe_image_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""

    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    return value.strip()


def normalize_order_payload(payload: Any, tracking_number: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Order service returned an invalid response.")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raw_items = []

    items = []
    for item in raw_items[:100]:
        if not isinstance(item, dict):
            continue

        quantity = item.get("model_quantity_purchased", 0)
        if not isinstance(quantity, (int, float)) or isinstance(quantity, bool):
            quantity = 0

        items.append(
            {
                "item_name": str(item.get("item_name") or ""),
                "model_name": str(item.get("model_name") or ""),
                "model_quantity_purchased": quantity,
                "model_sku": str(item.get("model_sku") or ""),
                "image_url": safe_image_url(item.get("image_url")),
            }
        )

    return {
        "tracking_number": tracking_number,
        "order_sn": str(payload.get("order_sn") or ""),
        "package_number": str(payload.get("package_number") or ""),
        "shipping_carrier": str(payload.get("shipping_carrier") or ""),
        "items": items,
    }


def safe_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in ALLOWED_VIDEO_EXTENSIONS:
        return suffix
    return ".webm"


def safe_filename(invoice: str, mode: str, extension: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}_{mode}_{invoice}{extension}"


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    filename = row["filename"]
    return {
        "id": row["id"],
        "invoice": row["invoice"],
        "mode": row["mode"],
        "filename": filename,
        "file_extension": Path(filename).suffix.lower().lstrip("."),
        "content_type": row["content_type"],
        "size_bytes": row["size_bytes"],
        "created_at": row["created_at"],
        "video_url": f"/videos/{filename}",
        "download_url": f"/videos/{filename}?download=1",
    }


def recording_path(filename: str) -> Path:
    path = (VIDEO_DIR / filename).resolve()
    try:
        path.relative_to(VIDEO_DIR)
    except ValueError as exc:
        raise ValueError("Invalid recording path.") from exc
    else:
        return path


def has_webm_header(path: Path) -> bool:
    with path.open("rb") as file:
        return file.read(4) == b"\x1a\x45\xdf\xa3"


def repair_webm_metadata(path: Path) -> bool:
    if path.suffix.lower() != ".webm":
        return False

    if not has_webm_header(path):
        app.logger.warning("Skipping WebM metadata repair for %s because the file header is invalid.", path.name)
        return False

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        app.logger.info("Skipping WebM metadata repair because ffmpeg is not installed.")
        return False

    fixed_path = path.with_name(f"{path.stem}.fixed{path.suffix}")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(path),
        "-map",
        "0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(fixed_path),
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
        fixed_path.replace(path)
    except (OSError, subprocess.SubprocessError) as error:
        if fixed_path.exists():
            fixed_path.unlink(missing_ok=True)
        app.logger.warning("Could not repair WebM metadata for %s: %s", path.name, error)
        return False

    return True


def validate_video_file(
    path: Path,
    expected_duration_seconds: float | None = None,
    *,
    allow_missing_webm_duration: bool = False,
) -> str | None:
    if path.suffix.lower() == ".webm" and not has_webm_header(path):
        return "Recording file is incomplete. Please retry the recording."

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        app.logger.info("Skipping video validation because ffprobe is not installed.")
        return None

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,duration",
        "-of",
        "json",
        str(path),
    ]

    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        payload = json.loads(result.stdout or "{}")
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        app.logger.warning("Could not validate video file %s: %s", path.name, error)
        return "Video file could not be read. Please retry the recording."

    streams = payload.get("streams") or []
    if not any(stream.get("codec_type") == "video" for stream in streams):
        return "Video file does not contain a readable video stream."

    # Some mobile containers expose duration on the video stream, not the format.
    # Ignore audio-only duration: it must not conceal a truncated video track.
    duration_values = [(payload.get("format") or {}).get("duration")]
    duration_values.extend(stream.get("duration") for stream in streams if stream.get("codec_type") == "video")
    duration_seconds = 0
    for value in duration_values:
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(candidate) and candidate > 0:
            duration_seconds = candidate
            break

    if duration_seconds <= 0:
        # Only the pre-remux WebM check may defer missing duration. The final
        # check must always verify a real media duration against the client timer.
        if allow_missing_webm_duration and path.suffix.lower() == ".webm":
            return None
        app.logger.warning("Missing video duration for %s (format=%s, streams=%s).", path.name, payload.get("format"), streams)
        return "Video duration could not be verified. Please retry the recording."

    if expected_duration_seconds is not None and expected_duration_seconds >= 1:
        lower_bound = max(0.5, expected_duration_seconds * 0.55)
        upper_bound = max(expected_duration_seconds + 10, expected_duration_seconds * 1.8)
        if duration_seconds < lower_bound or duration_seconds > upper_bound:
            app.logger.warning(
                "Rejected %s because duration %.3fs did not match expected %.3fs.",
                path.name,
                duration_seconds,
                expected_duration_seconds,
            )
            return "Recording duration could not be verified. Please retry the recording."

    return None


def probe_video(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed.")

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration,size:stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    duration = float((payload.get("format") or {}).get("duration") or 0)
    size = int((payload.get("format") or {}).get("size") or path.stat().st_size)

    return {
        "duration_seconds": duration,
        "size_bytes": size,
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "video_codec": video_stream.get("codec_name") or "video",
        "has_audio": audio_stream is not None,
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
    }


def format_duration(seconds: float) -> str:
    total = max(int(round(seconds)), 0)
    minutes, rest = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{rest:02d}"
    return f"{minutes}:{rest:02d}"


def estimate_mp4_size(duration_seconds: float, quality_key: str, has_audio: bool = True) -> int:
    quality = CONVERT_QUALITIES[quality_key]
    audio_bitrate = quality["audio_bitrate_kbps"] if has_audio else 0
    total_kbps = quality["video_bitrate_kbps"] + audio_bitrate
    return int((total_kbps * 1000 / 8) * duration_seconds * 1.03)


def converted_filename(source: str, resolution_key: str, quality_key: str) -> str:
    source_path = Path(source)
    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", source_path.stem)
    return f"{source_path.parent.name}/{safe_stem}_{resolution_key}_{quality_key}.mp4"


def converted_path(filename: str) -> Path:
    path = (CONVERTED_DIR / filename).resolve()
    try:
        path.relative_to(CONVERTED_DIR)
    except ValueError as exc:
        raise ValueError("Invalid converted video path.") from exc
    else:
        return path


def set_conversion_job(job_id: str, **updates: Any) -> None:
    with CONVERSION_JOBS_LOCK:
        if job_id in CONVERSION_JOBS:
            CONVERSION_JOBS[job_id].update(updates)


def get_conversion_job(job_id: str) -> dict[str, Any] | None:
    with CONVERSION_JOBS_LOCK:
        job = CONVERSION_JOBS.get(job_id)
        return dict(job) if job else None


def parse_ffmpeg_time(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None

    if value.isdigit():
        return max(int(value) / 1_000_000, 0)

    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, TypeError):
        return None


def ffmpeg_convert_command(
    ffmpeg: str,
    source_path: Path,
    temp_path: Path,
    resolution_key: str,
    quality_key: str,
) -> list[str]:
    quality = CONVERT_QUALITIES[quality_key]
    resolution = CONVERT_RESOLUTIONS[resolution_key]
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        quality["preset"],
        "-threads",
        "2",
        "-b:v",
        f"{quality['video_bitrate_kbps']}k",
        "-maxrate",
        f"{int(quality['video_bitrate_kbps'] * 1.35)}k",
        "-bufsize",
        f"{quality['video_bitrate_kbps'] * 2}k",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        f"{quality['audio_bitrate_kbps']}k",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
    ]

    if resolution["height"]:
        command.extend(["-vf", f"scale=-2:{resolution['height']}"])

    command.append(str(temp_path))
    return command


def conversion_worker_loop() -> None:
    while True:
        job_id, recording_id, resolution_key, quality_key = CONVERSION_QUEUE.get()
        try:
            run_conversion_job(job_id, recording_id, resolution_key, quality_key)
        finally:
            CONVERSION_QUEUE.task_done()


def ensure_conversion_worker() -> None:
    global CONVERSION_WORKER_STARTED

    with CONVERSION_WORKER_LOCK:
        if CONVERSION_WORKER_STARTED:
            return

        thread = threading.Thread(target=conversion_worker_loop, daemon=True)
        thread.start()
        CONVERSION_WORKER_STARTED = True


def run_conversion_job(
    job_id: str,
    recording_id: int,
    resolution_key: str,
    quality_key: str,
) -> None:
    temp_path: Path | None = None
    process = None
    safe_to_release = True

    try:
        with db_connect() as db:
            # Same owner is idempotent; a different operation cannot access this source.
            db.execute("BEGIN IMMEDIATE")
            reserve_recordings(db, [recording_id], job_id, "conversion")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("FFmpeg is not installed in this environment.")

        with db_connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()

        if row is None:
            raise ValueError("Recording not found.")

        if Path(row["filename"]).suffix.lower() != ".webm":
            raise ValueError("Only .webm recordings can be converted to MP4.")

        source_path = recording_path(row["filename"])
        if not source_path.exists():
            raise FileNotFoundError("Source video file is missing.")

        validation_error = validate_video_file(source_path)
        if validation_error:
            raise ValueError(validation_error)

        metadata = probe_video(source_path)
        duration_seconds = metadata["duration_seconds"]
        estimate_bytes = estimate_mp4_size(duration_seconds, quality_key, metadata["has_audio"])
        relative_output = converted_filename(row["filename"], resolution_key, quality_key)
        output_path = converted_path(relative_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_name(f".{output_path.stem}.converting{output_path.suffix}")

        set_conversion_job(
            job_id,
            status="running",
            progress=1,
            message="Converting with FFmpeg...",
            duration_seconds=duration_seconds,
            estimated_size_bytes=estimate_bytes,
        )

        process = subprocess.Popen(
            ffmpeg_convert_command(ffmpeg, source_path, temp_path, resolution_key, quality_key),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        if process.stdout is not None:
            for line in process.stdout:
                line = line.strip()
                if "=" not in line:
                    continue

                key, value = line.split("=", 1)
                if key in {"out_time_ms", "out_time_us", "out_time"}:
                    elapsed = parse_ffmpeg_time(value)
                    if elapsed is not None and duration_seconds > 0:
                        progress = min(99, max(1, int((elapsed / duration_seconds) * 100)))
                        set_conversion_job(job_id, progress=progress)
                elif key == "progress" and value == "end":
                    set_conversion_job(job_id, progress=99)

        return_code = process.wait(timeout=30)
        if return_code != 0:
            raise RuntimeError("FFmpeg conversion failed.")

        validation_error = validate_video_file(temp_path)
        if validation_error:
            raise RuntimeError(validation_error)

        temp_path.replace(output_path)
        set_conversion_job(
            job_id,
            status="complete",
            progress=100,
            message="MP4 conversion finished.",
            result={
                "ok": True,
                "recording": row_to_dict(row),
                "filename": relative_output,
                "download_url": f"/converted/{relative_output}?download=1",
                "video_url": f"/converted/{relative_output}",
                "size_bytes": output_path.stat().st_size,
                "estimated_size_bytes": estimate_bytes,
                "duration_seconds": duration_seconds,
                "resolution": CONVERT_RESOLUTIONS[resolution_key],
                "quality": CONVERT_QUALITIES[quality_key],
                "uses_ffmpeg": True,
            },
        )
    except Exception as error:
        if process is not None and process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=10)
            except Exception:
                safe_to_release = False
                app.logger.exception("FFmpeg termination uncertain; keeping recording reservation")
        if temp_path is not None and safe_to_release:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                app.logger.warning("Could not remove conversion temporary file")
        app.logger.warning("Conversion job %s failed: %s", job_id, error)
        set_conversion_job(
            job_id,
            status="failed",
            progress=0,
            message=str(error) or "Conversion failed.",
        )
    finally:
        if safe_to_release:
            with db_connect() as db:
                release_recordings(db, [recording_id], job_id)


def is_allowed_certificate_host(domain: str) -> bool:
    domain = domain.strip().strip("[]").lower()

    if domain in {"localhost", "scanner.local"}:
        return True

    if domain.endswith(".local") or domain.endswith(".home.arpa"):
        return True

    try:
        parsed = ip_address(domain)
    except ValueError:
        return False

    return parsed.is_private or parsed.is_loopback or parsed.is_link_local


def parse_positive_int(value: Optional[str], default: int, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        parsed = default

    parsed = max(parsed, 1)
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def parse_optional_positive_float(value: Optional[str]) -> float | None:
    if value in {None, ""}:
        return None

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None

    if parsed <= 0:
        return None

    return parsed


def parse_required_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None

    if parsed <= 0:
        return None

    return parsed


def parse_date_bound(value: str, end_of_day: bool = False) -> str | None:
    if not value:
        return None

    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    if end_of_day:
        parsed += timedelta(days=1)

    return parsed.isoformat()


def recording_filters() -> tuple[str, list[Any]]:
    query = request.args.get("q", "").strip().upper()
    date_from = parse_date_bound(request.args.get("date_from", "").strip())
    date_to = parse_date_bound(request.args.get("date_to", "").strip(), end_of_day=True)
    clauses: list[str] = []
    params: list[Any] = []

    if query:
        clauses.append("invoice LIKE ?")
        params.append(f"%{query}%")

    if date_from:
        clauses.append("created_at >= ?")
        params.append(date_from)

    if date_to:
        clauses.append("created_at < ?")
        params.append(date_to)

    if not clauses:
        return "", params

    return " WHERE " + " AND ".join(clauses), params


@app.get("/")
def index() -> str:
    return render_template("index.html", max_upload_mb=MAX_UPLOAD_MB)


@app.get("/convert")
def convert_page() -> str:
    return render_template(
        "convert.html",
        resolutions=CONVERT_RESOLUTIONS,
        qualities=CONVERT_QUALITIES,
    )


@app.get("/history")
def history_page():
    return render_template("history.html")


def scan_event_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = {key: row[key] for key in (
        "id", "tracking_number", "mode", "submitted_at", "source",
        "timestamp_basis", "lookup_status", "lookup_error")}
    snapshot = json.loads(row["order_snapshot"] or "{}")
    result.update({key: snapshot.get(key, [] if key == "items" else "")
                   for key in ("order_sn", "package_number", "shipping_carrier", "items")})
    recording = json.loads(row["recording_snapshot"] or "null")
    status = "not_recorded"
    if recording:
        status = "archived" if row["archived_at"] else "deleted" if row["video_deleted"] else (
            "available" if row["live_recording_id"] and video_exists(row["live_filename"]) else "missing")
        if status != "available":
            recording.pop("video_url", None)
            recording.pop("download_url", None)
        else:
            result["video_url"] = recording["video_url"]
            result["download_url"] = recording["download_url"]
    result.update(video_status=status, recording=recording,
                  recording_size_bytes=recording["size_bytes"] if recording else None,
                  archive_job_id=row["archive_job_id"], archived_at=row["archived_at"],
                  archive_location=row["archive_location"],
                  backup_verified_label=row["verified_backup_label"] if status == "available" else None)
    return result


@app.get("/api/scan-events")
def list_scan_events():
    clauses, params = [], []
    query = request.args.get("q", "").strip()
    if query:
        clauses.append("(e.tracking_number LIKE ? OR order_field(e.order_snapshot, 'order_sn') LIKE ? OR order_field(e.order_snapshot, 'package_number') LIKE ? OR order_field(e.order_snapshot, 'shipping_carrier') LIKE ?)")
        params.extend([f"%{query}%"] * 4)
    for key in ("mode", "lookup_status", "video_status"):
        value = request.args.get(key, "").strip()
        allowed = {"mode": ALLOWED_MODES,
                   "lookup_status": {"not_requested", "pending", "success", "not_found", "error"},
                   "video_status": {"available", "not_recorded", "deleted", "missing", "archived"}}[key]
        if value:
            if value not in allowed:
                return jsonify({"error": f"Invalid {key} filter."}), 400
            clauses.append(f"{key if key == 'video_status' else 'e.' + key} = ?")
            params.append(value)
    for key, operator in (("date_from", ">="), ("date_to", "<")):
        value = request.args.get(key, "").strip()
        if value:
            bound = parse_date_bound(value, end_of_day=key == "date_to")
            if bound is None:
                return jsonify({"error": f"Invalid {key} date."}), 400
            clauses.append(f"e.submitted_at {operator} ?")
            params.append(bound)
    # File availability participates in the SQL predicate before pagination.
    base = """FROM scan_events e LEFT JOIN recordings r ON r.id = e.recording_id"""
    status_sql = """CASE WHEN e.archived_at IS NOT NULL THEN 'archived'
        WHEN e.recording_id IS NULL THEN 'not_recorded'
        WHEN e.video_deleted = 1 THEN 'deleted'
        WHEN r.id IS NOT NULL AND video_exists(r.filename) = 1 THEN 'available'
        ELSE 'missing' END"""
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    where = where.replace("video_status =", f"({status_sql}) =")
    per_page = parse_positive_int(request.args.get("per_page"), DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    with db_connect() as db:
        total = db.execute(f"SELECT COUNT(*) {base}{where}", params).fetchone()[0]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = parse_positive_int(request.args.get("page"), 1, total_pages)
        rows = db.execute(f"""SELECT e.*, r.id AS live_recording_id, r.filename AS live_filename,
            (SELECT j.label FROM archive_jobs j JOIN archive_items ai ON ai.job_id=j.id
             WHERE ai.recording_id=e.recording_id AND j.state IN ('verified','removal_pending')
             AND j.confirmed_at IS NULL ORDER BY j.created_at DESC LIMIT 1) AS verified_backup_label
            {base}{where} ORDER BY e.submitted_at DESC, e.id DESC LIMIT ? OFFSET ?""",
            [*params, per_page, (page - 1) * per_page]).fetchall()
    return jsonify(items=[scan_event_dict(row) for row in rows], page=page,
                   per_page=per_page, total=total, total_pages=total_pages)


@app.post("/api/scan-events")
def create_scan_event():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A JSON object is required."}), 400
    submission_id = payload.get("submission_id")
    tracking = payload.get("tracking_number")
    mode = payload.get("mode")
    source = payload.get("source", "scan")
    if (not isinstance(submission_id, str) or not 1 <= len(submission_id) <= 200
            or not isinstance(tracking, str) or validate_tracking_number(tracking) is None
            or not isinstance(mode, str) or mode not in ALLOWED_MODES
            or not isinstance(source, str) or source not in {"scan", "manual_recording", "manual_upload"}):
        return jsonify({"error": "Invalid submission, tracking number, mode, or source."}), 400
    tracking = validate_tracking_number(tracking)
    with db_connect() as db:
        db.execute("""INSERT OR IGNORE INTO scan_events
            (submission_id, mutation_token, tracking_number, mode, submitted_at, source, timestamp_basis)
            VALUES (?, ?, ?, ?, ?, ?, 'submitted_at')""",
            (submission_id, uuid.uuid4().hex, tracking, mode, datetime.now(timezone.utc).isoformat(), source))
        inserted = db.execute("SELECT changes()").fetchone()[0]
        row = db.execute("SELECT * FROM scan_events WHERE submission_id = ?", (submission_id,)).fetchone()
        if (row["tracking_number"], row["mode"], row["source"]) != (tracking, mode, source):
            return jsonify({"error": "Submission identifier already belongs to another submission."}), 409
    return jsonify(id=row["id"], mutation_token=row["mutation_token"],
                   lookup_status=row["lookup_status"]), 201 if inserted else 200


@app.post("/api/scan-events/<int:event_id>/lookup")
def lookup_scan_event(event_id: int):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A JSON object is required."}), 400
    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM scan_events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            return jsonify({"error": "Scan event not found."}), 404
        if payload.get("mutation_token") != row["mutation_token"]:
            return jsonify({"error": "Invalid scan event token."}), 403
        if row["lookup_status"] == "pending":
            return jsonify({"error": "Lookup is pending or was interrupted."}), 409
        if row["lookup_status"] != "not_requested":
            if row["lookup_status"] == "success":
                return jsonify(json.loads(row["order_snapshot"]))
            return jsonify({"error": row["lookup_error"]}), row["lookup_http_status"]
        db.execute("UPDATE scan_events SET lookup_status = 'pending' WHERE id = ?", (event_id,))
    # Commit the claim before network I/O. Interrupted calls remain pending.
    result, status = lookup_order(row["tracking_number"])
    lookup_status = "success" if status == 200 else "not_found" if status == 404 else "error"
    with db_connect() as db:
        db.execute("""UPDATE scan_events SET lookup_status = ?, lookup_error = ?,
            lookup_http_status = ?, order_snapshot = ? WHERE id = ? AND lookup_status = 'pending'""",
            (lookup_status, result.get("error"), status,
             json.dumps(result) if status == 200 else None, event_id))
    return jsonify(result), status


@app.get("/api/recordings")
def list_recordings():
    where_sql, params = recording_filters()
    page = parse_positive_int(request.args.get("page"), 1)
    per_page = parse_positive_int(request.args.get("per_page"), DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    offset = (page - 1) * per_page

    with db_connect() as db:
        total = db.execute(f"SELECT COUNT(*) FROM recordings{where_sql}", params).fetchone()[0]
        rows = db.execute(
            f"""
            SELECT * FROM recordings{where_sql}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        ).fetchall()

    total_pages = max((total + per_page - 1) // per_page, 1)
    return jsonify(
        {
            "items": [row_to_dict(row) for row in rows],
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }
    )


@app.get("/api/convert/recordings")
def list_convertible_recordings():
    query = request.args.get("q", "").strip().upper()
    clauses = ["filename LIKE ?"]
    params: list[Any] = ["%.webm"]

    if query:
        clauses.append("invoice LIKE ?")
        params.append(f"%{query}%")

    with db_connect() as db:
        rows = db.execute(
            f"""
            SELECT * FROM recordings
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT 100
            """,
            params,
        ).fetchall()

    return jsonify({"items": [row_to_dict(row) for row in rows]})


@app.get("/api/convert/estimate")
def estimate_conversion():
    recording_id = parse_required_positive_int(request.args.get("recording_id"))
    resolution_key = request.args.get("resolution", "source")
    quality_key = request.args.get("quality", "standard")

    if recording_id is None:
        return jsonify({"error": "Choose a recording to convert."}), 400

    if resolution_key not in CONVERT_RESOLUTIONS:
        return jsonify({"error": "Choose a valid resolution."}), 400

    if quality_key not in CONVERT_QUALITIES:
        return jsonify({"error": "Choose a valid quality setting."}), 400

    with db_connect() as db:
        row = db.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()

    if row is None:
        return jsonify({"error": "Recording not found."}), 404

    if Path(row["filename"]).suffix.lower() != ".webm":
        return jsonify({"error": "Only .webm recordings can be converted to MP4."}), 400

    source_path = recording_path(row["filename"])
    if not source_path.exists():
        return jsonify({"error": "Source video file is missing."}), 404

    try:
        metadata = probe_video(source_path)
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
        app.logger.warning("Could not probe %s: %s", row["filename"], error)
        return jsonify({"error": "Could not read source video details."}), 400

    estimate_bytes = estimate_mp4_size(metadata["duration_seconds"], quality_key, metadata["has_audio"])

    return jsonify(
        {
            "recording": row_to_dict(row),
            "duration_seconds": metadata["duration_seconds"],
            "duration_label": format_duration(metadata["duration_seconds"]),
            "source_size_bytes": metadata["size_bytes"],
            "source_width": metadata["width"],
            "source_height": metadata["height"],
            "has_audio": metadata["has_audio"],
            "estimated_size_bytes": estimate_bytes,
            "resolution": CONVERT_RESOLUTIONS[resolution_key],
            "quality": CONVERT_QUALITIES[quality_key],
            "uses_ffmpeg": True,
        }
    )


@app.post("/api/convert")
def convert_recording():
    payload = request.get_json(silent=True) or request.form
    recording_id = parse_required_positive_int(payload.get("recording_id"))
    resolution_key = payload.get("resolution", "source")
    quality_key = payload.get("quality", "standard")

    if recording_id is None:
        return jsonify({"error": "Choose a recording to convert."}), 400

    if resolution_key not in CONVERT_RESOLUTIONS:
        return jsonify({"error": "Choose a valid resolution."}), 400

    if quality_key not in CONVERT_QUALITIES:
        return jsonify({"error": "Choose a valid quality setting."}), 400

    job_id = uuid.uuid4().hex
    try:
        with db_connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT filename FROM recordings WHERE id = ?", (recording_id,)).fetchone()
            if row is None or not video_exists(row["filename"]):
                return jsonify({"error": "Recording is unavailable."}), 404
            reserve_recordings(db, [recording_id], job_id, "conversion")
    except ReservationError as error:
        return jsonify({"error": str(error)}), 409
    with CONVERSION_JOBS_LOCK:
        CONVERSION_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "Queued for conversion. Waiting for the current conversion to finish.",
            "result": None,
        }

    try:
        ensure_conversion_worker()
        CONVERSION_QUEUE.put((job_id, recording_id, resolution_key, quality_key))
    except Exception:
        with db_connect() as db:
            release_recordings(db, [recording_id], job_id)
        raise

    return jsonify(get_conversion_job(job_id)), 202


@app.get("/api/convert/jobs/<job_id>")
def conversion_job_status(job_id: str):
    job = get_conversion_job(job_id)
    if job is None:
        return jsonify({"error": "Conversion job not found."}), 404

    return jsonify(job)


@app.get("/api/recordings/duplicate")
def duplicate_recording():
    invoice = sanitize_invoice(request.args.get("invoice", ""))

    if not invoice:
        return jsonify({"invoice": "", "count": 0, "latest": None})

    with db_connect() as db:
        count = db.execute("SELECT COUNT(*) FROM recordings WHERE invoice = ?", (invoice,)).fetchone()[0]
        latest = db.execute(
            "SELECT * FROM recordings WHERE invoice = ? ORDER BY created_at DESC LIMIT 1",
            (invoice,),
        ).fetchone()

    return jsonify(
        {
            "invoice": invoice,
            "count": count,
            "latest": row_to_dict(latest) if latest else None,
        }
    )


def lookup_order(tracking_number: str) -> tuple[dict[str, Any], int]:
    if not ORDER_LOOKUP_API_KEY:
        return {"error": "Shipping order lookup is not configured."}, 503

    query = urlencode({"tracking_number": tracking_number})
    upstream_request = UrlRequest(
        f"{ORDER_LOOKUP_API_URL}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "scanner-order-lookup/1.0",
            "X-API-Key": ORDER_LOOKUP_API_KEY,
        },
    )

    try:
        with urlopen(upstream_request, timeout=ORDER_LOOKUP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
        return normalize_order_payload(payload, tracking_number), 200
    except HTTPError as error:
        if error.code == 404:
            return {"error": "Tracking number not found."}, 404
        if error.code in {401, 403}:
            app.logger.warning("Shipping order API rejected its configured credentials.")
            return {"error": "Shipping order lookup authentication failed."}, 502
        if error.code == 429:
            return {"error": "Shipping order lookup is busy. Please try again shortly."}, 503
        app.logger.warning("Shipping order API returned HTTP %s.", error.code)
        return {"error": "Shipping order lookup is temporarily unavailable."}, 502
    except (TimeoutError, URLError) as error:
        app.logger.warning("Shipping order API could not be reached: %s", error)
        return {"error": "Could not reach the shipping order service."}, 502
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        app.logger.warning("Shipping order API returned invalid data: %s", error)
        return {"error": "Shipping order service returned invalid data."}, 502


@app.get("/api/orders/by-tracking-number")
def order_by_tracking_number():
    tracking_number = validate_tracking_number(request.args.get("tracking_number", ""))
    if tracking_number is None:
        return jsonify({"error": "Enter a valid shipping receipt or tracking number."}), 400
    payload, status = lookup_order(tracking_number)
    return jsonify(payload), status


@app.get("/api/summary")
def recording_summary():
    today = datetime.now(timezone.utc).date()
    today_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    tomorrow_start = (
        datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)
    ).isoformat()
    daily_start = (
        datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=6)
    ).isoformat()

    with db_connect() as db:
        totals = db.execute(
            "SELECT COUNT(*) AS total_recordings, COALESCE(SUM(size_bytes), 0) AS total_size_bytes FROM recordings"
        ).fetchone()
        today_recordings = db.execute(
            "SELECT COUNT(*) FROM recordings WHERE created_at >= ? AND created_at < ?",
            (today_start, tomorrow_start),
        ).fetchone()[0]
        daily_rows = db.execute(
            """
            SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count
            FROM recordings
            WHERE created_at >= ?
            GROUP BY day
            ORDER BY day DESC
            LIMIT 7
            """,
            (daily_start,),
        ).fetchall()

    daily_counts = {row["day"]: row["count"] for row in daily_rows}
    days = [
        {
            "date": (today - timedelta(days=offset)).isoformat(),
            "count": daily_counts.get((today - timedelta(days=offset)).isoformat(), 0),
        }
        for offset in range(7)
    ]

    return jsonify(
        {
            "total_recordings": totals["total_recordings"],
            "total_size_bytes": totals["total_size_bytes"],
            "today_recordings": today_recordings,
            "daily_counts": days,
        }
    )


def validate_upload_event(event, token: str, invoice: str, mode: str) -> str | None:
    if event is None or event["mutation_token"] != token:
        return "Invalid scan event or token."
    raw_tracking = validate_tracking_number(request.form.get("invoice", ""))
    if event["tracking_number"] != raw_tracking or sanitize_invoice(event["tracking_number"]) != invoice or event["mode"] != mode:
        return "Recording label and mode must match the scan event."
    if event["recording_id"] is not None:
        return "Scan event already has a recording."
    return None


@app.post("/api/recordings")
def create_recording():
    invoice = sanitize_invoice(request.form.get("invoice", ""))
    mode = request.form.get("mode", "").strip().lower()
    video = request.files.get("video")
    expected_duration_ms = parse_optional_positive_float(request.form.get("duration_ms"))
    expected_duration_seconds = expected_duration_ms / 1000 if expected_duration_ms else None

    if not invoice:
        return jsonify({"error": "Invoice or shipping receipt is required."}), 400

    if mode not in ALLOWED_MODES:
        return jsonify({"error": "Mode must be packing or return."}), 400

    if video is None or video.filename == "":
        return jsonify({"error": "Video file is required."}), 400

    event_id_raw = request.form.get("scan_event_id")
    event_token = request.form.get("scan_event_token")
    event_id = parse_required_positive_int(event_id_raw)
    if event_id_raw is not None or event_token is not None:
        if event_id is None or not event_token:
            return jsonify({"error": "Scan event ID and token are required together."}), 400
        with db_connect() as db:
            event = db.execute("SELECT * FROM scan_events WHERE id = ?", (event_id,)).fetchone()
        error = validate_upload_event(event, event_token, invoice, mode)
        if error:
            return jsonify({"error": error}), 409

    month_dir = VIDEO_DIR / datetime.now(timezone.utc).strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)

    filename = safe_filename(invoice, mode, safe_extension(video.filename))
    relative_name = f"{month_dir.name}/{filename}"
    target_path = month_dir / filename
    temp_path = month_dir / f".{Path(filename).stem}.uploading{Path(filename).suffix}"
    video.save(temp_path)

    # Browser WebM blobs often omit duration until remuxed. Still verify the
    # header and readable video before repair, then enforce duration afterward.
    if temp_path.suffix.lower() == ".webm":
        validation_error = validate_video_file(temp_path, expected_duration_seconds, allow_missing_webm_duration=True)
    else:
        validation_error = validate_video_file(temp_path, expected_duration_seconds)
    if validation_error:
        temp_path.unlink(missing_ok=True)
        return jsonify({"error": validation_error}), 400

    repair_webm_metadata(temp_path)

    validation_error = validate_video_file(temp_path, expected_duration_seconds)
    if validation_error:
        temp_path.unlink(missing_ok=True)
        return jsonify({"error": validation_error}), 400

    temp_path.replace(target_path)
    size_bytes = target_path.stat().st_size
    created_at = datetime.now(timezone.utc).isoformat()

    try:
        with db_connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if event_id is not None:
                event = db.execute("SELECT * FROM scan_events WHERE id = ?", (event_id,)).fetchone()
                error = validate_upload_event(event, event_token, invoice, mode)
                if error:
                    raise ValueError(error)
            cursor = db.execute(
                """
                INSERT INTO recordings
                    (invoice, mode, filename, original_name, content_type, size_bytes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invoice,
                    mode,
                    relative_name,
                    video.filename,
                    video.mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream",
                    size_bytes,
                    created_at,
                ),
            )
            row = db.execute("SELECT * FROM recordings WHERE id = ?", (cursor.lastrowid,)).fetchone()

            if event_id is None:
                cursor = db.execute("""INSERT INTO scan_events
                    (submission_id, mutation_token, tracking_number, mode, submitted_at, source, timestamp_basis)
                    VALUES (?, ?, ?, ?, ?, 'manual_upload', 'saved_at')""",
                    (f"legacy-upload:{uuid.uuid4().hex}", uuid.uuid4().hex, invoice, mode, created_at))
                event_id = cursor.lastrowid
            db.execute("UPDATE scan_events SET recording_id = ?, recording_snapshot = ? WHERE id = ?",
                       (row["id"], json.dumps(row_to_dict(row)), event_id))
    except (ValueError, sqlite3.Error) as error:
        try:
            target_path.unlink()
        except FileNotFoundError:
            pass
        app.logger.warning("Could not associate uploaded recording: %s", error)
        return jsonify({"error": str(error) if isinstance(error, ValueError) else "Could not save recording history."}), 409 if isinstance(error, ValueError) else 500

    return jsonify(row_to_dict(row)), 201


@app.delete("/api/recordings/<int:recording_id>")
def delete_recording(recording_id: int):
    owner = uuid.uuid4().hex
    try:
        with db_connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
            if row is None:
                return jsonify({"error": "Recording not found."}), 404
            reserve_recordings(db, [recording_id], owner, "delete")
    except ReservationError as error:
        return jsonify({"error": str(error)}), 409

    removed = False
    try:
        path = recording_path(row["filename"])
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        removed = True
        with db_connect() as db:
            db.execute("UPDATE scan_events SET video_deleted = 1 WHERE recording_id = ?", (recording_id,))
            db.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
            release_recordings(db, [recording_id], owner)
    except (OSError, sqlite3.Error, ValueError) as error:
        app.logger.warning("Could not delete recording %s: %s", recording_id, error)
        # If unlink succeeded but metadata failed, retain the reservation for review.
        if not removed:
            with db_connect() as db:
                release_recordings(db, [recording_id], owner)
        return jsonify({"error": "Could not complete deletion. Refresh and review the recording before retrying."}), 500
    return jsonify({"ok": True})


@app.get("/videos/<path:filename>")
def serve_video(filename: str):
    as_attachment = request.args.get("download") == "1"
    return send_from_directory(
        VIDEO_DIR,
        filename,
        as_attachment=as_attachment,
        download_name=Path(filename).name if as_attachment else None,
        conditional=True,
    )


@app.get("/converted/<path:filename>")
def serve_converted_video(filename: str):
    as_attachment = request.args.get("download") == "1"
    return send_from_directory(
        CONVERTED_DIR,
        filename,
        as_attachment=as_attachment,
        download_name=Path(filename).name if as_attachment else None,
        conditional=True,
    )


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/archive")
def archive_page():
    return render_template("archive.html")


def archive_json_action(action):
    # The app also runs behind Caddy; compare Origin with the browser-facing Host.
    origin = request.headers.get("Origin")
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return jsonify({"error": "Cross-site archive requests are not allowed."}), 403
    if origin:
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != request.host.lower():
            return jsonify({"error": "Archive requests must use the same origin."}), 403
    elif request.headers.get("Sec-Fetch-Site") != "same-origin":
        return jsonify({"error": "A same-origin browser request is required."}), 403
    payload = request.get_json(silent=True)
    if not request.is_json or not isinstance(payload, dict):
        return jsonify({"error": "A JSON object is required."}), 400
    try:
        return jsonify(action(payload))
    except ReservationError as error:
        return jsonify({"error": str(error)}), 409
    except (ValueError, FileNotFoundError) as error:
        return jsonify({"error": str(error)}), 400
    except OSError:
        app.logger.exception("Archive storage operation failed")
        return jsonify({"error": "Archive storage is unavailable. Local files were not authorized for removal by this request."}), 503


@app.get('/api/archive/automatic/preview')
def automatic_archive_preview():
    return jsonify(archive_service.automatic_preview())


@app.route('/api/archive/automatic', methods=['GET', 'POST'])
def automatic_archive_runs():
    if request.method == 'POST':
        def start(payload):
            if payload.get('authorize_removal') is not True:
                raise ValueError('Explicit automatic local-removal authorization is required.')
            return archive_service.create_automatic_run(rate_mbps=payload.get('rate_mbps', 5))
        return archive_json_action(start)
    return jsonify(archive_service.list_automatic_runs())


@app.post('/api/archive/automatic/<run_id>/control')
def automatic_archive_control(run_id):
    return archive_json_action(lambda payload: archive_service.control_automatic_run(run_id, payload.get('action')))


@app.get('/archive/settings')
def archive_settings_page():
    return render_template('archive_settings.html')


@app.route('/api/archive/settings', methods=['GET', 'POST'])
def archive_settings():
    if request.method == 'GET':
        return jsonify(archive_service.settings())
    return archive_json_action(lambda payload: archive_service.configure(
        payload.get('folder'), payload.get('label'), save=True))


@app.post('/api/archive/settings/test')
def archive_settings_test():
    return archive_json_action(lambda payload: archive_service.configure(
        payload.get('folder'), payload.get('label'), save=False))


@app.get("/api/archive/config")
def archive_config():
    return jsonify(archive_service.config())


@app.get("/api/archive/preview")
def archive_preview():
    try:
        return jsonify(archive_service.preview(
            request.args.get("cutoff", ""),
            parse_positive_int(request.args.get("page"), 1),
            parse_positive_int(request.args.get("per_page"), 25, 50)))
    except (ValueError, OSError) as error:
        return jsonify({"error": str(error)}), 400


@app.route("/api/archive/jobs", methods=["GET", "POST"])
def archive_jobs():
    if request.method == "POST":
        return archive_json_action(lambda payload: archive_service.create_job(payload.get("recording_ids"), payload.get("cutoff")))
    return jsonify(archive_service.list_jobs(
        parse_positive_int(request.args.get("page"), 1),
        parse_positive_int(request.args.get("per_page"), 10, 50)))


@app.get("/api/archive/jobs/<job_id>")
def archive_job(job_id):
    try:
        return jsonify(archive_service.get_job(job_id))
    except ValueError as error:
        return jsonify({"error": str(error)}), 404


@app.post("/api/archive/jobs/<job_id>/removal-preview")
def archive_removal_preview(job_id):
    return archive_json_action(lambda payload: archive_service.removal_preview(job_id))


@app.post("/api/archive/jobs/<job_id>/confirm-removal")
def archive_confirm_removal(job_id):
    return archive_json_action(lambda payload: archive_service.confirm_removal(
        job_id, payload.get("token", ""), payload.get("digest", "")))


@app.post("/api/archive/jobs/<job_id>/cancel")
def archive_cancel(job_id):
    return archive_json_action(lambda payload: archive_service.cancel(job_id))


@app.post("/api/archive/jobs/<job_id>/resume")
def archive_resume(job_id):
    return archive_json_action(lambda payload: archive_service.resume(job_id))


@app.get("/api/caddy/allow-certificate")
def allow_caddy_certificate():
    domain = request.args.get("domain", "")

    if is_allowed_certificate_host(domain):
        return jsonify({"ok": True})

    return jsonify({"error": "Certificate host is not allowed."}), 403


init_storage()
_archive_root = os.environ.get("ARCHIVE_DIR", "").strip()
archive_service = ArchiveService(
    db_connect, recording_path, converted_path, converted_filename,
    list(CONVERT_RESOLUTIONS), list(CONVERT_QUALITIES),
    archive_root=Path(_archive_root) if _archive_root else None,
    destination_id=os.environ.get("ARCHIVE_DESTINATION_ID", "").strip(),
    label=os.environ.get("ARCHIVE_LABEL", "Backup drive").strip(),
    settings_root=os.environ.get('ARCHIVE_SETTINGS_ROOT', '').strip() or None,
    io_busy=recording_io_busy,
)
if os.environ.get("ARCHIVE_WORKER_DISABLED") != "1":
    archive_service.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
