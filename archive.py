"""Verified archive operations for a single app process (not multi-process safe).

All recording writers/deleters must use reserve_recordings before enqueue/work and
release_recordings only after work has stopped. Reservations never expire by age.
Filesystem unlink and SQLite commits cannot be atomic; persisted intent makes an
explicitly resumed cleanup reconcilable. Checksums do not promise future NAS uptime.
"""
import calendar
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone


class ArchiveError(ValueError):
    pass


class _AutomaticYield(Exception):
    pass


class ReservationError(ArchiveError):
    pass


def init_schema(db):
    """Call in app.init_storage's transaction, after creating scan_events."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(scan_events)")}
    for name in ("archive_job_id", "archived_at", "archive_location"):
        if name not in columns:
            db.execute(f"ALTER TABLE scan_events ADD COLUMN {name} TEXT")
    statements = [
        """CREATE TABLE IF NOT EXISTS archive_jobs (
            id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, destination_id TEXT NOT NULL, label TEXT NOT NULL,
            manifest_digest TEXT, token_hash TEXT, token_expires REAL,
            confirmed_at TEXT, error TEXT)""",
        """CREATE TABLE IF NOT EXISTS archive_items (
            job_id TEXT NOT NULL, recording_id INTEGER NOT NULL, snapshot TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(job_id, recording_id))""",
        """CREATE TABLE IF NOT EXISTS archive_files (
            id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, recording_id INTEGER NOT NULL,
            kind TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, source_stamp TEXT NOT NULL, sha256 TEXT,
            verified_at TEXT, removal_intent TEXT, removed_at TEXT, error TEXT,
            UNIQUE(job_id, kind, source), UNIQUE(job_id, target))""",
        """CREATE TABLE IF NOT EXISTS recording_reservations (
            recording_id INTEGER PRIMARY KEY, owner TEXT NOT NULL,
            operation TEXT NOT NULL, created_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_archive_jobs_date ON archive_jobs(created_at, id)",
    ]
    for statement in statements:
        db.execute(statement)
    db.execute('CREATE TABLE IF NOT EXISTS archive_settings (id INTEGER PRIMARY KEY CHECK(id=1), folder TEXT NOT NULL, label TEXT NOT NULL, destination_id TEXT NOT NULL)')
    if 'destination_root' not in {row[1] for row in db.execute('PRAGMA table_info(archive_jobs)')}:
        db.execute('ALTER TABLE archive_jobs ADD COLUMN destination_root TEXT')
    db.execute("""CREATE TABLE IF NOT EXISTS automatic_runs (
        id TEXT PRIMARY KEY, state TEXT NOT NULL, cutoff TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, authorized_at TEXT NOT NULL,
        rate_mbps REAL NOT NULL, current_job_id TEXT, stage TEXT NOT NULL DEFAULT 'queued',
        bytes_processed INTEGER NOT NULL DEFAULT 0, error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS automatic_candidates (
        run_id TEXT NOT NULL, recording_id INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        job_id TEXT, error TEXT, PRIMARY KEY(run_id,recording_id))""")
    if 'automatic_run_id' not in {row[1] for row in db.execute('PRAGMA table_info(archive_jobs)')}:
        db.execute('ALTER TABLE archive_jobs ADD COLUMN automatic_run_id TEXT')
    if 'history_snapshot' not in {row[1] for row in db.execute('PRAGMA table_info(archive_items)')}:
        db.execute('ALTER TABLE archive_items ADD COLUMN history_snapshot TEXT')


def _now():
    return datetime.now(timezone.utc).isoformat()


def reserve_recordings(db, recording_ids, owner, operation):
    """Atomic claim within caller's transaction; same owner/operation is idempotent."""
    ids = sorted(set(recording_ids))
    db.execute("SAVEPOINT archive_claim")
    try:
        for rid in ids:
            row = db.execute("SELECT owner, operation FROM recording_reservations WHERE recording_id=?", (rid,)).fetchone()
            if row and (row[0], row[1]) != (owner, operation):
                raise ReservationError(f"Recording {rid} is reserved")
            db.execute("INSERT OR IGNORE INTO recording_reservations VALUES (?, ?, ?, ?)",
                       (rid, owner, operation, _now()))
        db.execute("RELEASE archive_claim")
    except Exception:
        db.execute("ROLLBACK TO archive_claim")
        db.execute("RELEASE archive_claim")
        raise


def release_recordings(db, recording_ids, owner):
    for rid in recording_ids:
        db.execute("DELETE FROM recording_reservations WHERE recording_id=? AND owner=?", (rid, owner))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _cutoff(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return parsed.isoformat()
    except (ValueError, AttributeError, TypeError):
        raise ArchiveError("Cutoff must be an explicit UTC ISO timestamp") from None


def _paging(page, per_page):
    page, per_page = int(page), int(per_page)
    if page < 1 or not 1 <= per_page <= 100:
        raise ArchiveError("Invalid pagination")
    return page, per_page


class ArchiveService:
    CHUNK = 1024 * 1024

    def __init__(self, db_connect, recording_path, converted_path, converted_filename,
                 resolutions, qualities, archive_root=None, destination_id="", label="Archive", settings_root=None, io_busy=None):
        self.db_connect = db_connect
        self.recording_path = recording_path
        self.converted_path = converted_path
        self.converted_filename = converted_filename
        self.resolutions, self.qualities = tuple(resolutions), tuple(qualities)
        self.root = Path(archive_root).absolute() if archive_root is not None else None
        self.destination_id, self.label = str(destination_id), str(label)
        self.io_busy = io_busy or (lambda: False)
        self._automatic_context = threading.local()
        self._wake = threading.Event()
        self._queue = queue.Queue()
        self._worker = None
        self._lock = threading.Lock()
        self._work_lock = threading.Lock()
        self.settings_root = Path(settings_root).absolute() if settings_root else None
        saved = self._rows('SELECT * FROM archive_settings WHERE id=1')
        if self.settings_root and saved:
            try:
                self.root = self._settings_folder(saved[0]['folder'])
                self.destination_id, self.label = saved[0]['destination_id'], saved[0]['label']
            except ArchiveError:
                self.root, self.destination_id = None, ''

    def _settings_folder(self, folder):
        if not self.settings_root or not self.settings_root.is_dir() or self.settings_root.is_symlink():
            raise ArchiveError('Backup drive is not mounted. Configure ARCHIVE_SETTINGS_ROOT first.')
        if not isinstance(folder, str) or not folder or len(folder) > 160:
            raise ArchiveError('Enter a relative backup folder, for example ScannerArchive.')
        self._name(folder)
        path = self.settings_root.joinpath(*folder.split('/'))
        try:
            path.resolve().relative_to(self.settings_root.resolve())
        except ValueError:
            raise ArchiveError('Backup folder escapes the mounted drive') from None
        current = self.settings_root
        for part in folder.split('/'):
            current /= part
            if current.is_symlink():
                raise ArchiveError('Backup folders cannot use symlinks')
        return path

    def settings(self):
        saved = self._rows('SELECT * FROM archive_settings WHERE id=1')
        result = self.config()
        result.update(editable=bool(self.settings_root and self.settings_root.is_dir()),
                      folder=saved[0]['folder'] if saved else '',
                      mount_hint='Choose a folder inside the backup drive mounted by Docker.')
        return result

    def configure(self, folder, label, save=False):
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ArchiveError('Enter a destination name of 1 to 80 characters')
        if not self._work_lock.acquire(blocking=False):
            raise ArchiveError('An archive operation is running. Try again after it finishes.')
        try:
            with self._lock:
                path = self._settings_folder(folder)
                unfinished = self._rows("SELECT id FROM archive_jobs WHERE state NOT IN ('completed','cancelled') LIMIT 1")
                if save and (unfinished or self.automatic_busy):
                    raise ArchiveError('Finish or cancel unfinished archive jobs before changing destination settings.')
                if not path.is_dir() and not save:
                    raise ArchiveError('Folder does not exist yet. Save settings to create it on the mounted drive.')
                # Reject overlaps before any directory creation or write probe.
                for resolver in (self.recording_path, self.converted_path):
                    local = Path(resolver('archive-root-probe.webm')).parent.resolve()
                    try:
                        common = Path(os.path.commonpath((str(local), str(path.resolve()))))
                    except ValueError:  # Separate Windows drive letters.
                        continue
                    if common in (local, path.resolve()):
                        raise ArchiveError('Backup and local media roots must not overlap')
                if save:
                    path.mkdir(parents=True, exist_ok=True)
                self._settings_folder(folder)
                sentinel = path / '.scanner-archive-id'
                if sentinel.is_symlink():
                    raise ArchiveError('Backup identity cannot use a symlink')
                if not sentinel.exists():
                    if not save:
                        raise ArchiveError('Folder is not initialized. Save settings to initialize it.')
                    with sentinel.open('x', encoding='utf-8') as stream:
                        stream.write(str(uuid.uuid4()))
                        stream.flush()
                        os.fsync(stream.fileno())
                identity = sentinel.read_text(encoding='utf-8').strip()
                try:
                    uuid.UUID(identity)
                except (ValueError, AttributeError):
                    raise ArchiveError('Backup folder identity is invalid') from None
                probe = path / ('.scanner-test-' + uuid.uuid4().hex)
                linked = probe.with_suffix('.link')
                try:
                    with probe.open('xb') as stream:
                        stream.write(b'scanner-backup-test')
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(probe, linked)
                    if linked.read_bytes() != b'scanner-backup-test':
                        raise ArchiveError('Backup drive verification failed')
                finally:
                    for test_file in (linked, probe):
                        if test_file.exists():
                            test_file.unlink()
                usage = shutil.disk_usage(path)
                if save:
                    with self.db_connect() as db:
                        db.execute('INSERT OR REPLACE INTO archive_settings VALUES (1,?,?,?)', (folder, label.strip(), identity))
                    self.root, self.destination_id, self.label = path, identity, label.strip()
                return dict(available=True, label=label.strip(), folder=folder, destination_id=identity,
                            free_bytes=usage.free, total_bytes=usage.total, saved=save)
        finally:
            self._work_lock.release()

    def _rows(self, sql, args=()):
        with self.db_connect() as db:
            cursor = db.execute(sql, args)
            names = [col[0] for col in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _update(self, sql, args=()):
        with self.db_connect() as db:
            db.execute(sql, args)

    def _identity(self):
        if not self.root or not self.destination_id:
            raise ArchiveError("Archive destination is not configured")
        if self.root.is_symlink() or not self.root.is_dir():
            raise ArchiveError("Archive root unavailable")
        for resolver in (self.recording_path, self.converted_path):
            source_root = Path(resolver("archive-root-probe.webm")).parent.resolve()
            archive_root = self.root.resolve()
            try:
                common = Path(os.path.commonpath((str(source_root), str(archive_root))))
            except ValueError:  # Different Windows volumes cannot overlap.
                continue
            if common in (source_root, archive_root):
                raise ArchiveError("Archive and local media roots must not overlap")
        sentinel = self.root / ".scanner-archive-id"
        if sentinel.is_symlink() or not sentinel.is_file():
            raise ArchiveError("Archive identity sentinel missing")
        with sentinel.open("rb") as stream:
            identity = stream.read(4097)
        if len(identity) > 4096 or identity.decode("utf-8").strip() != self.destination_id:
            raise ArchiveError("Archive destination identity mismatch")

    def config(self):
        result = {"enabled": bool(self.root and self.destination_id), "label": self.label,
                  "destination_id": self.destination_id, "available": False}
        try:
            self._identity()
            result["available"] = True
        except (OSError, ValueError) as exc:
            result["error"] = self._safe_error(exc)
        return result

    @staticmethod
    def _safe_error(exc):
        # OSError strings can include mount paths/credentials. Never expose them.
        return str(exc) if isinstance(exc, ArchiveError) else type(exc).__name__

    @staticmethod
    def _name(name):
        if (not isinstance(name, str) or not name or any(c in name for c in ("\\", ":", "\x00"))
                or any(part in ("", ".", "..") for part in name.split("/"))):
            raise ArchiveError("Unsafe source filename")
        return name

    def _source(self, kind, name):
        self._name(name)
        resolver = self.recording_path if kind == "original" else self.converted_path
        path = Path(resolver(name))
        # app's resolvers resolve symlinks already; also check the unresolved name.
        source_root = Path(resolver("archive-root-probe.webm")).parent
        candidate = source_root / name
        if candidate.is_symlink() or path.resolve() != candidate.absolute():
            raise ArchiveError("Source symlink or escape is not allowed")
        return path

    @staticmethod
    def _stamp(path):
        stat = path.stat()
        if not path.is_file():
            raise ArchiveError("Source is not a regular file")
        return _json([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])

    def _inventory(self, snapshot, job_id):
        name = self._name(snapshot["filename"])
        month = datetime.fromisoformat(snapshot["created_at"].replace("Z", "+00:00")).strftime("%Y-%m")
        names = [("original", name)]
        names += [("converted", self.converted_filename(name, r, q))
                  for r in self.resolutions for q in self.qualities]
        result, seen = [], set()
        for kind, filename in names:
            path = self._source(kind, filename)
            if kind == "converted" and not path.exists():
                continue
            if (kind, filename) in seen:
                raise ArchiveError("Converted filename collision")
            seen.add((kind, filename))
            stamp = self._stamp(path)
            result.append({"kind": kind, "source": filename, "size_bytes": path.stat().st_size,
                           "source_stamp": stamp,
                           "target": f"jobs/{job_id}/{'videos' if kind == 'original' else 'converted'}/{filename if '/' in filename else month + '/' + filename}"})
        return result

    def preview(self, cutoff, page=1, per_page=50):
        cutoff = _cutoff(cutoff)
        page, per_page = _paging(page, per_page)
        # julianday compares UTC instants even for existing Z/offset representations.
        total = self._rows("SELECT COUNT(*) AS n FROM recordings WHERE julianday(created_at)<=julianday(?)", (cutoff,))[0]["n"]
        rows = self._rows("""SELECT r.*, reservation.owner AS busy_owner FROM recordings r
            LEFT JOIN recording_reservations reservation ON reservation.recording_id=r.id
            WHERE julianday(r.created_at)<=julianday(?) ORDER BY r.created_at,r.id LIMIT ? OFFSET ?""",
                          (cutoff, per_page, (page - 1) * per_page))
        # Advisory preview only: enumerate each converted directory once per page,
        # rather than resolving/stat'ing 15 mostly nonexistent paths per recording.
        # No cross-request cache: new conversions appear on the next request.
        directories = {}
        converted_root = Path(self.converted_path('archive-root-probe.webm')).parent
        for row in rows:
            busy = row.pop('busy_owner')
            row["files"], row["error"] = [], None
            try:
                source = self._source('original', row['filename'])
                if not source.is_file():
                    raise ArchiveError('Original video file is missing')
                row['files'].append(dict(kind='original', source=row['filename'], size_bytes=source.stat().st_size))
                seen = set()
                for resolution in self.resolutions:
                    for quality in self.qualities:
                        name = self._name(self.converted_filename(row['filename'], resolution, quality))
                        if name in seen:
                            raise ArchiveError('Converted filename collision')
                        seen.add(name)
                        relative = Path(name)
                        directory = converted_root / relative.parent
                        if directory not in directories:
                            # Match the full inventory's containment/symlink policy.
                            if directory.resolve() != directory.absolute():
                                raise ArchiveError('Source symlink or escape is not allowed')
                            try:
                                with os.scandir(directory) as entries:
                                    directories[directory] = {entry.name: entry for entry in entries}
                            except FileNotFoundError:
                                directories[directory] = {}
                        entry = directories[directory].get(relative.name)
                        if entry is None:
                            continue
                        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                            raise ArchiveError('Converted source is not a regular file')
                        row['files'].append(dict(kind='converted', source=name,
                                                 size_bytes=entry.stat(follow_symlinks=False).st_size))
                if busy:
                    row["error"] = "Recording is busy or requires job review"
            except (OSError, ValueError) as exc:
                row["error"] = self._safe_error(exc)
            row["original_bytes"] = sum(f["size_bytes"] for f in row["files"] if f["kind"] == "original")
            row["converted_bytes"] = sum(f["size_bytes"] for f in row["files"] if f["kind"] == "converted")
            row["total_bytes"] = row["original_bytes"] + row["converted_bytes"]
        return {"items": rows, "total": total, "page": page, "per_page": per_page, "cutoff": cutoff}

    def create_job(self, recording_ids, cutoff=None):
        with self._lock:
            return self._create_job(recording_ids, cutoff)

    def _create_job(self, recording_ids, cutoff=None, automatic_run_id=None):
        if not isinstance(recording_ids, (list, tuple)) or not recording_ids or len(recording_ids) > 50:
            raise ArchiveError("Select 1 to 50 recordings")
        if any(type(rid) is not int or rid <= 0 for rid in recording_ids) or len(set(recording_ids)) != len(recording_ids):
            raise ArchiveError("Invalid recording selection")
        cutoff = _cutoff(cutoff) if cutoff is not None else None
        self._identity()
        job_id, now = str(uuid.uuid4()), _now()
        # Persist the job and claims together: a crash must never leave invisible claims.
        snapshots = []
        with self.db_connect() as db:
            db.execute("BEGIN IMMEDIATE")
            reserve_recordings(db, recording_ids, job_id, "archive")
            db.execute("INSERT INTO archive_jobs(id,state,created_at,updated_at,destination_id,label) VALUES (?,'preparing',?,?,?,?)",
                       (job_id, now, now, self.destination_id, self.label))
            db.execute('UPDATE archive_jobs SET destination_root=?,automatic_run_id=? WHERE id=?', (str(self.root), automatic_run_id, job_id))
            if automatic_run_id:
                db.execute('UPDATE automatic_candidates SET job_id=? WHERE run_id=? AND recording_id=?', (job_id, automatic_run_id, recording_ids[0]))
            for rid in recording_ids:
                cursor = db.execute("SELECT * FROM recordings WHERE id=?", (rid,))
                row = cursor.fetchone()
                if row is None:
                    raise ArchiveError("Recording no longer exists")
                snapshot = dict(zip([col[0] for col in cursor.description], row))
                if cutoff and datetime.fromisoformat(snapshot["created_at"].replace("Z", "+00:00")) > datetime.fromisoformat(cutoff):
                    raise ArchiveError("Recording is newer than cutoff")
                snapshots.append(snapshot)
                history_cursor = db.execute('SELECT * FROM scan_events WHERE recording_id=?', (rid,))
                history_row = history_cursor.fetchone()
                history = dict(zip([col[0] for col in history_cursor.description], history_row)) if history_row else {}
                history.pop('mutation_token', None)
                db.execute("INSERT INTO archive_items(job_id,recording_id,snapshot,history_snapshot) VALUES (?,?,?,?)", (job_id, rid, _json(snapshot), _json(history)))
        try:
            items = [(snapshot, self._inventory(snapshot, job_id)) for snapshot in snapshots]
            with self.db_connect() as db:
                for snapshot, files in items:
                    for file in files:
                        db.execute("""INSERT INTO archive_files(job_id,recording_id,kind,source,target,size_bytes,source_stamp)
                                      VALUES (?,?,?,?,?,?,?)""", (job_id, snapshot["id"], file["kind"], file["source"], file["target"], file["size_bytes"], file["source_stamp"]))
                db.execute("UPDATE archive_jobs SET state='queued' WHERE id=?", (job_id,))
        except Exception as error:
            with self.db_connect() as db:
                release_recordings(db, recording_ids, job_id)
                db.execute("UPDATE archive_jobs SET state='error',error=? WHERE id=?", (self._safe_error(error), job_id))
            raise
        if not automatic_run_id:
            self._enqueue(job_id, "backup")
        return self.get_job(job_id)

    def get_job(self, job_id):
        rows = self._rows("SELECT * FROM archive_jobs WHERE id=?", (job_id,))
        if not rows:
            raise ArchiveError("Archive job not found")
        job = rows[0]
        job.pop("token_hash", None)
        job.pop("token_expires", None)
        job["items"] = self._rows("SELECT * FROM archive_items WHERE job_id=? ORDER BY recording_id", (job_id,))
        for item in job["items"]:
            item["snapshot"] = json.loads(item["snapshot"])
            item["history_snapshot"] = json.loads(item["history_snapshot"] or '{}')
            item["files"] = self._rows("SELECT * FROM archive_files WHERE job_id=? AND recording_id=? ORDER BY kind,id", (job_id, item["recording_id"]))
        files = [f for item in job["items"] for f in item["files"]]
        job["file_count"] = len(files)
        job["total_bytes"] = sum(f["size_bytes"] for f in files)
        job["verified_bytes"] = sum(f["size_bytes"] for f in files if f["verified_at"])
        job["removed_bytes"] = sum(f["size_bytes"] for f in files if f["removed_at"])
        return job

    def list_jobs(self, page=1, per_page=20):
        page, per_page = _paging(page, per_page)
        rows = self._rows("SELECT id FROM archive_jobs ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (per_page, (page - 1) * per_page))
        return {"items": [self.get_job(row["id"]) for row in rows], "page": page, "per_page": per_page,
                "total": self._rows("SELECT COUNT(*) AS n FROM archive_jobs")[0]["n"]}

    def _state(self, job_id, state, error=None):
        self._update("UPDATE archive_jobs SET state=?,updated_at=?,error=? WHERE id=?", (state, _now(), error, job_id))

    def _check_job(self, job):
        if (job["destination_id"] != self.destination_id or job["label"] != self.label
                or (job.get('destination_root') and job['destination_root'] != str(self.root))):
            raise ArchiveError("Archive configuration changed")
        self._identity()

    def _target(self, relative):
        self._identity()
        parts = relative.split("/")
        if any(p in ("", ".", "..") or "\\" in p or ":" in p for p in parts):
            raise ArchiveError("Unsafe archive path")
        target = self.root.joinpath(*parts)
        try:
            target.resolve().relative_to(self.root.resolve())
        except ValueError:
            raise ArchiveError("Archive path escapes destination") from None
        current = self.root
        for part in parts:
            current /= part
            if current.is_symlink():
                raise ArchiveError("Archive symlink is not allowed")
        return target

    def _read_chunk(self, stream):
        self._automatic_checkpoint()
        return stream.read(self.CHUNK)

    def _hash(self, path):
        before = self._stamp(path)
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: self._read_chunk(stream), b""):
                self._automatic_checkpoint(len(chunk), "hashing")
                digest.update(chunk)
                size += len(chunk)
        if self._stamp(path) != before:
            raise ArchiveError("File changed while hashing")
        return size, digest.hexdigest()

    def _verify(self, file, path):
        if self._hash(path) != (file["size_bytes"], file["sha256"]):
            raise ArchiveError("File checksum mismatch")

    def _publish(self, partial, final):
        self._identity()
        # Deliberately no rename/replace fallback: SMB must support atomic hard links.
        os.link(partial, final)
        if os.name != "nt":
            # Persist the directory entry on supported POSIX mounts. Failure keeps
            # the job unverified; a subsequent retry independently checks final.
            fd = os.open(str(final.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        partial.unlink()

    def _copy(self, file):
        source = self._source(file["kind"], file["source"])
        if self._stamp(source) != file["source_stamp"]:
            raise ArchiveError("Source changed since selection")
        target = self._target(file["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        self._target(file["target"])
        if target.exists():
            if not file["sha256"]:
                raise ArchiveError("Unowned archive target collision")
            self._verify(file, source)
            self._verify(file, target)
        else:
            partial = target.with_name(target.name + "." + uuid.uuid4().hex + ".part")
            try:
                digest, size = hashlib.sha256(), 0
                with source.open("rb") as src, partial.open("xb") as dst:
                    for chunk in iter(lambda: self._read_chunk(src), b""):
                        self._automatic_checkpoint(len(chunk), "copying")
                        dst.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
                if size != file["size_bytes"] or self._stamp(source) != file["source_stamp"]:
                    raise ArchiveError("Source changed during copy")
                checksum = digest.hexdigest()
                if file["sha256"] and file["sha256"] != checksum:
                    raise ArchiveError("Source checksum changed")
                file["sha256"] = checksum
                self._verify(file, partial)
                # Persist ownership/hash before publishing; crash after link is retryable.
                self._update("UPDATE archive_files SET sha256=? WHERE id=?", (checksum, file["id"]))
                self._target(file["target"])
                self._publish(partial, target)
                self._verify(file, target)
            finally:
                if partial.exists():
                    partial.unlink()
        if self._stamp(source) != file["source_stamp"]:
            raise ArchiveError("Source changed during verification")
        self._verify(file, source)
        self._update("UPDATE archive_files SET verified_at=?,error=NULL WHERE id=?", (_now(), file["id"]))

    def _manifest_bytes(self, job):
        value = {"version": 1, "job_id": job["id"], "destination_id": job["destination_id"],
                 "label": job["label"], "created_at": job["created_at"], "items": []}
        for item in job["items"]:
            exported = {"recording_id": item["recording_id"], "snapshot": item["snapshot"],
                        "files": [{k: file[k] for k in ("kind", "source", "target", "size_bytes", "sha256")}
                                  for file in item["files"]]}
            if item.get('history_snapshot'):
                exported['history'] = item['history_snapshot']
            value['items'].append(exported)
        return _json(value).encode("utf-8")

    def _manifest(self, job, publish=False):
        expected = self._manifest_bytes(job)
        digest = hashlib.sha256(expected).hexdigest()
        target = self._target(f"jobs/{job['id']}/manifest.json")
        if publish and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(uuid.uuid4().hex + ".part")
            try:
                with partial.open("xb") as stream:
                    stream.write(expected)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._publish(partial, target)
            finally:
                if partial.exists():
                    partial.unlink()
        if not publish and job["manifest_digest"] != digest:
            raise ArchiveError("Manifest revision mismatch")
        if self._hash(target) != (len(expected), digest):
            raise ArchiveError("Archive manifest checksum mismatch")
        return digest

    def _claim(self, job):
        with self.db_connect() as db:
            reserve_recordings(db, [i["recording_id"] for i in job["items"]], job["id"], "archive")

    def _release(self, job):
        with self.db_connect() as db:
            release_recordings(db, [i["recording_id"] for i in job["items"]], job["id"])

    def _unchanged_inventory(self, item, job_id, allow_intent=False):
        # Build expected remaining inventory, allowing absence ONLY with durable intent.
        expected = {(f["kind"], f["source"]): f for f in item["files"]}
        snapshot = item["snapshot"]
        names = [("original", snapshot["filename"])] + [
            ("converted", self.converted_filename(snapshot["filename"], r, q))
            for r in self.resolutions for q in self.qualities]
        for key in names:
            path = self._source(*key)
            file = expected.get(key)
            exists = path.exists()
            if not file:
                if exists:
                    raise ArchiveError("Recording inventory changed")
                continue
            if not exists:
                if allow_intent and file["removal_intent"]:
                    continue
                raise ArchiveError("Local file unexpectedly missing")
            if file["removed_at"] or self._stamp(path) != file["source_stamp"]:
                raise ArchiveError("Local file changed")
            self._verify(file, path)

    def removal_preview(self, job_id):
        with self._lock:
            job = self.get_job(job_id)
            self._manual_job(job)
            if job["state"] not in ("verified", "removal_pending") or job["confirmed_at"]:
                raise ArchiveError("Job is not awaiting removal confirmation")
            self._check_job(job)
            self._manifest(job)
            token = secrets.token_urlsafe(32)
            self._update("UPDATE archive_jobs SET state='removal_pending',token_hash=?,token_expires=?,updated_at=? WHERE id=?",
                         (hashlib.sha256(token.encode()).hexdigest(), time.time() + 900, _now(), job_id))
            return {"token": token, "digest": job["manifest_digest"], "job_id": job_id,
                    "file_count": job["file_count"], "total_bytes": job["total_bytes"],
                    "recording_count": len(job["items"]), "label": job["label"], "expires_in": 900}

    def confirm_removal(self, job_id, token, digest):
        with self._lock:
            job = self.get_job(job_id)
            self._manual_job(job)
            row = self._rows("SELECT token_hash,token_expires FROM archive_jobs WHERE id=?", (job_id,))[0]
            if (job["state"] != "removal_pending" or job["confirmed_at"] or not isinstance(token, str)
                    or not isinstance(digest, str) or not row["token_hash"] or not row["token_expires"]
                    or row["token_expires"] < time.time()
                    or not hmac.compare_digest(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
                    or not hmac.compare_digest(job["manifest_digest"] or "", digest)):
                raise ArchiveError("Invalid or expired removal confirmation")
            self._check_job(job)
            self._claim(job)
            # Large source/NAS rehashes run in the worker, never in this HTTP request.
            # _remove revalidates the complete recording before the first unlink.
            self._update("UPDATE archive_jobs SET confirmed_at=?,state='removal_pending',token_hash=NULL,token_expires=NULL,updated_at=? WHERE id=?", (_now(), _now(), job_id))
        self._enqueue(job_id, "remove")
        return self.get_job(job_id)

    def _remove(self, job):
        self._automatic_checkpoint(stage="removing")
        self._manifest(job)
        for item in job["items"]:
            if item["state"] == "completed":
                continue
            # Rehash ALL remaining sources and ALL backups before unlinking any.
            self._check_job(job)
            self._unchanged_inventory(item, job["id"], allow_intent=True)
            for file in item["files"]:
                self._verify(file, self._target(file["target"]))
            for file in sorted(item["files"], key=lambda f: f["kind"] == "original"):
                self._check_job(job)
                self._manifest(job)
                path = self._source(file["kind"], file["source"])
                if path.exists():
                    if file["removed_at"]:
                        raise ArchiveError("Removed local file reappeared")
                    if self._stamp(path) != file["source_stamp"]:
                        raise ArchiveError("Local file changed before unlink")
                    self._verify(file, path)
                    self._verify(file, self._target(file["target"]))
                    self._update("UPDATE archive_files SET removal_intent=? WHERE id=?", (_now(), file["id"]))
                    self._check_job(job)
                    with self._lock:
                        self._automatic_checkpoint(stage="removing", destructive=True)
                        path.unlink()
                elif not file["removal_intent"]:
                    raise ArchiveError("Local file missing without removal intent")
                self._update("UPDATE archive_files SET removed_at=?,error=NULL WHERE id=?", (_now(), file["id"]))
            location = f"{job['label']}: jobs/{job['id']}/"
            with self.db_connect() as db:
                # Keep recording_id/snapshot tombstones, never set video_deleted.
                updated = db.execute("UPDATE scan_events SET archive_job_id=?,archived_at=?,archive_location=? WHERE recording_id=?",
                                     (job["id"], _now(), location, item["recording_id"]))
                if not updated.rowcount:
                    raise ArchiveError("Recording history tombstone missing")
                db.execute("DELETE FROM recordings WHERE id=?", (item["recording_id"],))
                db.execute("UPDATE archive_items SET state='completed' WHERE job_id=? AND recording_id=?", (job["id"], item["recording_id"]))
        self._state(job["id"], "completed")
        self._release(job)

    def run_job(self, job_id, operation="backup"):
        """Synchronous worker entry point. Errors are persisted and returned in job."""
        with self._work_lock:
            job = self.get_job(job_id)
            if job.get("automatic_run_id") != getattr(self._automatic_context, "run_id", None):
                raise ArchiveError("Automatic child jobs require run controls")
            if job["state"] == "cancelled":
                return job
            if operation not in ("backup", "remove"):
                raise ArchiveError("Unknown archive operation")
            if job["state"] == "completed" or (operation == "backup" and job["state"] in ("verified", "removal_pending")):
                return job
            if operation == "remove" and not job["confirmed_at"]:
                raise ArchiveError("Removal has not been confirmed")
            if operation == "backup" and job["confirmed_at"]:
                raise ArchiveError("Confirmed removal cannot run as backup")
            try:
                with self._lock:
                    job = self.get_job(job_id)
                    if job["state"] == "cancelled":
                        return job
                    self._check_job(job)
                    self._claim(job)
                    self._state(job_id, "removing" if operation == "remove" else "backing_up")
                if operation == "remove":
                    self._remove(job)
                else:
                    self._state(job_id, "backing_up")
                    # Recover preparation interrupted before file inventory committed.
                    if not job["file_count"]:
                        inventories = [(item, self._inventory(item["snapshot"], job_id)) for item in job["items"]]
                        with self.db_connect() as db:
                            for item, files in inventories:
                                for file in files:
                                    db.execute("""INSERT INTO archive_files(job_id,recording_id,kind,source,target,size_bytes,source_stamp)
                                        VALUES (?,?,?,?,?,?,?)""", (job_id, item["recording_id"], file["kind"], file["source"], file["target"], file["size_bytes"], file["source_stamp"]))
                        job = self.get_job(job_id)
                    for item in job["items"]:
                        for file in item["files"]:
                            try:
                                self._copy(file)
                            except Exception as exc:
                                self._update("UPDATE archive_files SET error=? WHERE id=?", (self._safe_error(exc), file["id"]))
                                raise
                    job = self.get_job(job_id)
                    digest = self._manifest(job, publish=True)
                    self._update("UPDATE archive_jobs SET manifest_digest=? WHERE id=?", (digest, job_id))
                    self._update("UPDATE archive_items SET state='verified' WHERE job_id=?", (job_id,))
                    with self._lock:
                        self._release(job)
                        self._state(job_id, "verified")
            except _AutomaticYield:
                self._state(job_id, "interrupted")
                raise
            except Exception as exc:
                self._state(job_id, "interrupted" if job["confirmed_at"] else "error", self._safe_error(exc))
            return self.get_job(job_id)

    def resume(self, job_id):
        with self._lock:
            job = self.get_job(job_id)
            self._manual_job(job)
            if job["state"] not in ("error", "interrupted"):
                raise ArchiveError("Job does not require resume")
            self._check_job(job)
            self._claim(job)
            operation = "remove" if job["confirmed_at"] else "backup"
            self._state(job_id, "removing" if operation == "remove" else "queued")
        self._enqueue(job_id, operation)
        return self.get_job(job_id)

    def cancel(self, job_id):
        """Explicitly abandon unconfirmed work; never deletes local or NAS files."""
        with self._lock:
            job = self.get_job(job_id)
            self._manual_job(job)
            if job["confirmed_at"] or job["state"] not in ("error", "interrupted", "verified", "removal_pending"):
                raise ArchiveError("Only inactive, unconfirmed backups can be cancelled")
            self._release(job)
            self._update("UPDATE archive_jobs SET state='cancelled',token_hash=NULL,token_expires=NULL,updated_at=? WHERE id=?", (_now(), job_id))
            return self.get_job(job_id)

    @staticmethod
    def _calendar_cutoff(now=None):
        now = now or datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ArchiveError('Current time must be timezone aware')
        now = now.astimezone(timezone.utc)
        month_index = now.year * 12 + now.month - 1 - 3
        year, month = divmod(month_index, 12)
        month += 1
        return now.replace(year=year, month=month,
                           day=min(now.day, calendar.monthrange(year, month)[1])).isoformat()

    def automatic_preview(self, now=None):
        cutoff = self._calendar_cutoff(now)
        # Advisory metadata only: never walk the whole video library on UI polls.
        # The worker validates files and reservations one recording at a time.
        summary = self._rows('''SELECT COUNT(*) AS eligible_count,
            COALESCE(SUM(size_bytes),0) AS original_bytes FROM recordings
            WHERE julianday(created_at)<julianday(?)''', (cutoff,))[0]
        return dict(cutoff=cutoff, **summary)

    @property
    def automatic_busy(self):
        return bool(self._rows("""SELECT id FROM automatic_runs WHERE state NOT IN ('completed','stopped')
            OR EXISTS (SELECT 1 FROM archive_jobs j WHERE j.automatic_run_id=automatic_runs.id
                       AND j.confirmed_at IS NOT NULL AND j.state!='completed') LIMIT 1"""))

    def create_automatic_run(self, rate_mbps=5, now=None):
        if isinstance(rate_mbps, bool) or not isinstance(rate_mbps, (float, int)) or not 1 <= rate_mbps <= 20:
            raise ArchiveError('Rate must be between 1 and 20 MiB/s')
        cutoff = self._calendar_cutoff(now)
        with self._lock:
            self._identity()
            if self.automatic_busy:
                raise ArchiveError('Finish or stop the existing automatic run first')
            run_id, created = str(uuid.uuid4()), _now()
            with self.db_connect() as db:
                db.execute('INSERT INTO automatic_runs(id,state,cutoff,created_at,updated_at,authorized_at,rate_mbps) VALUES (?,\'queued\',?,?,?,?,?)',
                           (run_id, cutoff, created, created, created, rate_mbps))
                db.execute('INSERT INTO automatic_candidates(run_id,recording_id) SELECT ?,id FROM recordings WHERE julianday(created_at)<julianday(?)', (run_id, cutoff))
        self._enqueue(run_id, 'automatic')
        return self.get_automatic_run(run_id)

    def get_automatic_run(self, run_id):
        rows = self._rows('SELECT * FROM automatic_runs WHERE id=?', (run_id,))
        if not rows:
            raise ArchiveError('Automatic run not found')
        run = rows[0]
        counts = self._rows('SELECT state,COUNT(*) AS n FROM automatic_candidates WHERE run_id=? GROUP BY state', (run_id,))
        run.update(total_count=sum(row['n'] for row in counts), completed_count=0, skipped_count=0, error_count=0, pending_count=0)
        for row in counts:
            run[row['state'] + '_count'] = row['n']
        run['processed_count'] = run['completed_count'] + run['skipped_count'] + run['error_count']
        totals = self._rows('''SELECT COALESCE(SUM(CASE WHEN f.removed_at IS NOT NULL THEN f.size_bytes ELSE 0 END),0) AS removed_bytes,
            COALESCE(SUM(CASE WHEN f.verified_at IS NOT NULL THEN f.size_bytes ELSE 0 END),0) AS verified_bytes
            FROM archive_files f JOIN archive_jobs j ON j.id=f.job_id WHERE j.automatic_run_id=?''', (run_id,))[0]
        run.update(totals)
        return run

    def list_automatic_runs(self, page=1, per_page=20):
        page, per_page = _paging(page, per_page)
        rows = self._rows('SELECT id FROM automatic_runs ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (per_page, (page - 1) * per_page))
        return dict(items=[self.get_automatic_run(row['id']) for row in rows], page=page, per_page=per_page,
                    total=self._rows('SELECT COUNT(*) AS n FROM automatic_runs')[0]['n'])

    @staticmethod
    def _manual_job(job):
        if job.get('automatic_run_id'):
            raise ArchiveError('Use automatic run controls for this job')

    def control_automatic_run(self, run_id, action):
        with self._lock:
            run = self.get_automatic_run(run_id)
            if action not in ('pause', 'resume', 'stop'):
                raise ArchiveError('Unknown automatic run action')
            unresolved = self._rows("SELECT id FROM archive_jobs WHERE automatic_run_id=? AND confirmed_at IS NOT NULL AND state!='completed'", (run_id,))
            if run['state'] in ('completed', 'stopped') and not (action == 'resume' and unresolved):
                raise ArchiveError('Automatic run is already finished')
            if action == 'resume' and run['state'] not in ('paused', 'interrupted') and not unresolved:
                raise ArchiveError('Automatic run is not paused or interrupted')
            if action == 'resume':
                self._update("UPDATE archive_jobs SET state='queued' WHERE automatic_run_id=? AND state='cancelled' AND id IN (SELECT job_id FROM automatic_candidates WHERE run_id=? AND state='error')", (run_id, run_id))
                self._update("UPDATE automatic_candidates SET state='pending',error=NULL WHERE run_id=? AND state='error'", (run_id,))
            state = {'pause': 'paused', 'resume': 'queued', 'stop': 'stopped'}[action]
            self._update('UPDATE automatic_runs SET state=?,stage=?,updated_at=? WHERE id=?', (state, state, _now(), run_id))
            self._wake.set()
        if action == 'resume':
            self._enqueue(run_id, 'automatic')
        elif action == 'stop':
            self._retire_stopped_children(run_id)
        return self.get_automatic_run(run_id)

    def _retire_stopped_children(self, run_id):
        # Never drop a claim while its writer/unlinker is still running.
        if not self._work_lock.acquire(blocking=False):
            return
        try:
            for row in self._rows("SELECT id FROM archive_jobs WHERE automatic_run_id=? AND state!='completed'", (run_id,)):
                job = self.get_job(row['id'])
                if not any(f['removal_intent'] for i in job['items'] for f in i['files']):
                    self._release(job)
                    self._update('UPDATE archive_jobs SET confirmed_at=NULL WHERE id=?', (job['id'],))
                    self._state(job['id'], 'cancelled')
                else:
                    self._state(job['id'], 'interrupted', 'Stopped after removal intent; explicit resume required')
                    self._update("UPDATE automatic_runs SET state='interrupted',stage='stopped_requires_review',error='Stopped after removal intent; explicit resume required' WHERE id=?", (run_id,))
        finally:
            self._work_lock.release()

    def _automatic_eligible(self, run, job):
        if not run['authorized_at'] or job.get('automatic_run_id') != run['id']:
            raise ArchiveError('Automatic removal is not authorized')
        cutoff = datetime.fromisoformat(run['cutoff'])
        for item in job['items']:
            rows = self._rows('SELECT * FROM recordings WHERE id=?', (item['recording_id'],))
            if not rows or rows[0] != item['snapshot']:
                raise ArchiveError('Recording metadata changed')
            date = datetime.fromisoformat(rows[0]['created_at'].replace('Z', '+00:00'))
            if date.tzinfo is None or date >= cutoff:
                raise ArchiveError('Recording is not strictly older than three calendar months')

    def _automatic_checkpoint(self, size=0, stage=None, destructive=False):
        run_id = getattr(self._automatic_context, 'run_id', None)
        if not run_id:
            return
        run = self._rows('SELECT * FROM automatic_runs WHERE id=?', (run_id,))[0]
        if run['state'] not in ('running', 'queued', 'waiting_io'):
            raise _AutomaticYield()
        if self.io_busy():
            self._update("UPDATE automatic_runs SET state='waiting_io',stage='waiting_io',updated_at=? WHERE id=? AND state IN ('running','queued','waiting_io')", (_now(), run_id))
            raise _AutomaticYield()
        if destructive:
            self._automatic_eligible(run, self.get_job(run['current_job_id']))
        if stage:
            self._update('UPDATE automatic_runs SET stage=?,bytes_processed=bytes_processed+?,updated_at=? WHERE id=?', (stage, size, _now(), run_id))
        # Per-chunk pacing applies to reads for both copy and independent hashing.
        # Short interruptible waits ensure controls do not wait for a whole large chunk.
        remaining = size / (run['rate_mbps'] * 1024 * 1024)
        deadline = time.monotonic() + remaining
        while remaining > 0:
            self._wake.wait(min(0.05, remaining))
            self._wake.clear()
            latest = self._rows('SELECT state FROM automatic_runs WHERE id=?', (run_id,))[0]
            if latest['state'] not in ('running', 'queued', 'waiting_io'):
                raise _AutomaticYield()
            if self.io_busy():
                self._update("UPDATE automatic_runs SET state='waiting_io',stage='waiting_io' WHERE id=? AND state IN ('running','queued','waiting_io')", (run_id,))
                raise _AutomaticYield()
            remaining = deadline - time.monotonic()

    def run_automatic_run(self, run_id):
        """One recording per queue turn; explicit entry also supports deterministic tests."""
        run = self.get_automatic_run(run_id)
        if run['state'] not in ('queued', 'running', 'waiting_io'):
            return run
        self._automatic_context.run_id = run_id
        job = None
        try:
            self._automatic_checkpoint()
            self._update("UPDATE automatic_runs SET state='running',updated_at=? WHERE id=? AND state IN ('queued','running','waiting_io')", (_now(), run_id))
            candidates = self._rows("SELECT * FROM automatic_candidates WHERE run_id=? AND state='pending' ORDER BY recording_id LIMIT 1", (run_id,))
            if not candidates:
                unresolved = self._rows("SELECT id FROM archive_jobs WHERE automatic_run_id=? AND confirmed_at IS NOT NULL AND state!='completed'", (run_id,))
                state = 'interrupted' if unresolved or self.get_automatic_run(run_id)['error_count'] else 'completed'
                self._update("UPDATE automatic_runs SET state=?,stage=?,current_job_id=NULL,updated_at=? WHERE id=? AND state='running'", (state, state, _now(), run_id))
                return self.get_automatic_run(run_id)
            candidate = candidates[0]
            rid = candidate['recording_id']
            if candidate['job_id']:
                job = self.get_job(candidate['job_id'])
            else:
                rows = self._rows('SELECT * FROM recordings WHERE id=? AND julianday(created_at)<julianday(?)', (rid, run['cutoff']))
                if (not rows or not self._source('original', rows[0]['filename']).is_file()
                        or self._rows('SELECT recording_id FROM recording_reservations WHERE recording_id=?', (rid,))):
                    self._update("UPDATE automatic_candidates SET state='skipped' WHERE run_id=? AND recording_id=?", (run_id, rid))
                    return self.get_automatic_run(run_id)
                with self._lock:
                    job = self._create_job([rid], automatic_run_id=run_id)
            self._update('UPDATE automatic_runs SET current_job_id=? WHERE id=?', (job['id'], run_id))
            self._automatic_checkpoint()
            if job['state'] != 'completed':
                if not job['confirmed_at']:
                    job = self.run_job(job['id'])
                    if job['state'] != 'verified':
                        raise ArchiveError(job['error'] or 'Backup did not verify')
                    with self._lock:
                        self._automatic_checkpoint()
                        self._automatic_eligible(self.get_automatic_run(run_id), job)
                        self._claim(job)
                        self._update("UPDATE archive_jobs SET confirmed_at=?,state='removal_pending' WHERE id=? AND automatic_run_id=?", (_now(), job['id'], run_id))
                self._automatic_checkpoint()
                job = self.run_job(job['id'], 'remove')
                if job['state'] != 'completed':
                    raise ArchiveError(job['error'] or 'Removal did not complete')
            self._update("UPDATE automatic_candidates SET state='completed',error=NULL WHERE run_id=? AND recording_id=?", (run_id, rid))
        except _AutomaticYield:
            pass
        except Exception as exc:
            error = self._safe_error(exc)
            self._update('UPDATE automatic_runs SET error=?,updated_at=? WHERE id=?', (error, _now(), run_id))
            if 'candidate' in locals():
                self._update("UPDATE automatic_candidates SET state='error',error=? WHERE run_id=? AND recording_id=?", (error, run_id, candidate['recording_id']))
                # Failed backup claims can be safely released, destructive uncertainty cannot.
                linked = self._rows('SELECT job_id FROM automatic_candidates WHERE run_id=? AND recording_id=?', (run_id, candidate['recording_id']))
                if linked and linked[0]['job_id']:
                    failed = self.get_job(linked[0]['job_id'])
                    if not failed['confirmed_at']:
                        self._release(failed)
                        self._state(failed['id'], 'cancelled', error)
        finally:
            self._automatic_context.run_id = None
            current = self.get_automatic_run(run_id)
            if current['state'] == 'stopped':
                self._retire_stopped_children(run_id)
            elif current['state'] in ('running', 'queued'):
                self._enqueue(run_id, 'automatic')
        return self.get_automatic_run(run_id)

    def _enqueue(self, job_id, operation):
        if self._worker is not None:
            self._queue.put((job_id, operation))

    def start(self):
        """Once per process. Never auto-resume interrupted or confirmed removal."""
        with self._lock:
            if self._worker is not None:
                return
            self._update("UPDATE archive_jobs SET state='interrupted',updated_at=? WHERE state IN ('preparing','backing_up','removal_pending','removing')", (_now(),))
            self._update("UPDATE automatic_runs SET state='interrupted',stage='interrupted',updated_at=? WHERE state NOT IN ('completed','stopped','paused')", (_now(),))
            pending = self._rows("SELECT id FROM archive_jobs WHERE state='queued' AND confirmed_at IS NULL AND automatic_run_id IS NULL")
            def worker():
                while True:
                    try:
                        job_id, operation = self._queue.get(timeout=0.25)
                    except queue.Empty:
                        try:
                            for run in self._rows("SELECT id FROM automatic_runs WHERE state='waiting_io'"):
                                self._enqueue(run['id'], 'automatic')
                        except sqlite3.Error:
                            pass  # A temporarily unavailable database must not kill the worker.
                        continue
                    try:
                        if operation == 'automatic':
                            self.run_automatic_run(job_id)
                        else:
                            self.run_job(job_id, operation)
                    except Exception:
                        pass  # Operation errors are persisted; keep the sole worker alive.
                    finally:
                        self._queue.task_done()
            self._worker = threading.Thread(target=worker, name="archive-worker", daemon=True)
            self._worker.start()
            for job in pending:
                self._queue.put((job["id"], "backup"))
