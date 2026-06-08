from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_from_directory


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data")).resolve()
VIDEO_DIR = DATA_DIR / "videos"
DB_PATH = DATA_DIR / "recordings.sqlite3"

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "512"))
ALLOWED_MODES = {"packing", "return"}
ALLOWED_VIDEO_EXTENSIONS = {".webm", ".mp4", ".mov", ".m4v", ".avi", ".mkv"}


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
    }


def recording_path(filename: str) -> Path:
    path = (VIDEO_DIR / filename).resolve()
    try:
        path.relative_to(VIDEO_DIR)
    except ValueError as exc:
        raise ValueError("Invalid recording path.") from exc
    else:
        return path


@app.get("/")
def index() -> str:
    return render_template("index.html", max_upload_mb=MAX_UPLOAD_MB)


@app.get("/api/recordings")
def list_recordings():
    query = request.args.get("q", "").strip()
    params: list[Any] = []
    sql = "SELECT * FROM recordings"

    if query:
        sql += " WHERE invoice LIKE ?"
        params.append(f"%{query}%")

    sql += " ORDER BY created_at DESC LIMIT 100"

    with db_connect() as db:
        rows = db.execute(sql, params).fetchall()

    return jsonify([row_to_dict(row) for row in rows])


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
    return send_from_directory(VIDEO_DIR, filename, conditional=True)


@app.get("/health")
def health():
    return jsonify({"ok": True})


init_storage()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
