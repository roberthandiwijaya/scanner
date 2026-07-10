from __future__ import annotations

import json
import os
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

from flask import Flask, jsonify, render_template, request, send_from_directory


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data")).resolve()
VIDEO_DIR = DATA_DIR / "videos"
CONVERTED_DIR = DATA_DIR / "converted"
DB_PATH = DATA_DIR / "recordings.sqlite3"

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "512"))
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


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def init_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
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


def db_connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def sanitize_invoice(value: str) -> str:
    value = value.strip()
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^A-Za-z0-9._-]", "", value)
    return value[:80]


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


def validate_video_file(path: Path, expected_duration_seconds: float | None = None) -> str | None:
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
        "format=duration:stream=codec_type",
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

    try:
        duration_seconds = float((payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration_seconds = 0

    if duration_seconds <= 0:
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


def run_conversion_job(
    job_id: str,
    recording_id: int,
    resolution_key: str,
    quality_key: str,
) -> None:
    temp_path: Path | None = None

    try:
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
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        app.logger.warning("Conversion job %s failed: %s", job_id, error)
        set_conversion_job(
            job_id,
            status="failed",
            progress=0,
            message=str(error) or "Conversion failed.",
        )


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
    query = request.args.get("q", "").strip()
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
    query = request.args.get("q", "").strip()
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
    with CONVERSION_JOBS_LOCK:
        CONVERSION_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "Queued for conversion.",
            "result": None,
        }

    thread = threading.Thread(
        target=run_conversion_job,
        args=(job_id, recording_id, resolution_key, quality_key),
        daemon=True,
    )
    thread.start()

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

    month_dir = VIDEO_DIR / datetime.now(timezone.utc).strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)

    filename = safe_filename(invoice, mode, safe_extension(video.filename))
    relative_name = f"{month_dir.name}/{filename}"
    target_path = month_dir / filename
    temp_path = month_dir / f".{Path(filename).stem}.uploading{Path(filename).suffix}"
    video.save(temp_path)

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

    with db_connect() as db:
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
                video.mimetype or "video/webm",
                size_bytes,
                created_at,
            ),
        )
        row = db.execute("SELECT * FROM recordings WHERE id = ?", (cursor.lastrowid,)).fetchone()

    return jsonify(row_to_dict(row)), 201


@app.delete("/api/recordings/<int:recording_id>")
def delete_recording(recording_id: int):
    with db_connect() as db:
        row = db.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()

    if row is None:
        return jsonify({"error": "Recording not found."}), 404

    try:
        path = recording_path(row["filename"])
        if path.exists():
            path.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        app.logger.warning("Could not delete recording file %s: %s", row["filename"], error)
        return jsonify({"error": "Could not delete recording file."}), 500

    with db_connect() as db:
        db.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))

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


@app.get("/api/caddy/allow-certificate")
def allow_caddy_certificate():
    domain = request.args.get("domain", "")

    if is_allowed_certificate_host(domain):
        return jsonify({"ok": True})

    return jsonify({"error": "Certificate host is not allowed."}), 403


init_storage()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
