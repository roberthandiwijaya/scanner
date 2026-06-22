from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, render_template, request, send_from_directory


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data")).resolve()
VIDEO_DIR = DATA_DIR / "videos"
DB_PATH = DATA_DIR / "recordings.sqlite3"

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "512"))
ALLOWED_MODES = {"packing", "return"}
ALLOWED_VIDEO_EXTENSIONS = {".webm", ".mp4", ".mov", ".m4v", ".avi", ".mkv"}
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def init_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
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


def parse_positive_int(value: Optional[str], default: int, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        parsed = default

    parsed = max(parsed, 1)
    if maximum is not None:
        parsed = min(parsed, maximum)
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
    video.save(target_path)
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


@app.get("/health")
def health():
    return jsonify({"ok": True})


init_storage()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
