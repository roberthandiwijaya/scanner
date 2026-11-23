import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from archive import ArchiveError, ArchiveService, ReservationError, init_schema, reserve_recordings, release_recordings


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class ArchiveServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.videos = self.base / "videos"
        self.converted = self.base / "converted"
        self.nas = self.base / "nas"
        for directory in (self.videos, self.converted, self.nas):
            directory.mkdir()
        (self.nas / ".scanner-archive-id").write_text("test-nas\n")
        self.db_path = self.base / "test.sqlite"
        with self.connect() as db:
            db.execute("""CREATE TABLE recordings (id INTEGER PRIMARY KEY, invoice TEXT,
                filename TEXT UNIQUE, created_at TEXT, size_bytes INTEGER)""")
            db.execute("""CREATE TABLE scan_events (id INTEGER PRIMARY KEY, recording_id INTEGER UNIQUE,
                recording_snapshot TEXT, video_deleted INTEGER DEFAULT 0)""")
            init_schema(db)
            init_schema(db)
        self.service = self.make_service()
        self.add_recording(1)

    def connect(self):
        db = sqlite3.connect(self.db_path, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def converted_name(name, resolution, quality):
        return f"{Path(name).stem}_{resolution}_{quality}.mp4"

    def make_service(self, **kwargs):
        options = dict(db_connect=self.connect, recording_path=lambda n: self.videos / n,
                       converted_path=lambda n: self.converted / n,
                       converted_filename=self.converted_name, resolutions=["720", "original"],
                       qualities=["standard", "high"], archive_root=self.nas,
                       destination_id="test-nas", label="Test NAS")
        options.update(kwargs)
        return ArchiveService(**options)

    def add_recording(self, rid):
        name = f"video-{rid}.webm"
        (self.videos / name).write_bytes(b"original-data" * 100)
        snapshot = dict(id=rid, invoice="ORDER", filename=name, created_at="2025-01-02T10:00:00+00:00", size_bytes=1300)
        with self.connect() as db:
            db.execute("INSERT INTO recordings VALUES (:id,:invoice,:filename,:created_at,:size_bytes)", snapshot)
            db.execute("INSERT INTO scan_events(recording_id,recording_snapshot) VALUES (?,?)", (rid, json.dumps(snapshot)))
        return name

    def add_converted(self):
        name = self.converted_name("video-1.webm", "720", "standard")
        path = self.converted / name
        path.write_bytes(b"converted-bytes")
        return path

    def backup(self):
        job = self.service.create_job([1])
        job = self.service.run_job(job["id"])
        self.assertEqual(job["state"], "verified", job["error"])
        return job

    def confirm(self, job):
        prompt = self.service.removal_preview(job["id"])
        return self.service.confirm_removal(job["id"], prompt["token"], prompt["digest"])

    def test_schema_and_reservations_all_or_none(self):
        with self.connect() as db:
            reserve_recordings(db, [2], "conversion", "convert")
        with self.connect() as db:
            with self.assertRaises(ReservationError):
                reserve_recordings(db, [1, 2], "archive", "archive")
            self.assertIsNone(db.execute("SELECT * FROM recording_reservations WHERE recording_id=1").fetchone())
            reserve_recordings(db, [2], "conversion", "convert")
            release_recordings(db, [2], "wrong-owner")
            self.assertEqual(db.execute("SELECT count(*) FROM recording_reservations").fetchone()[0], 1)
            release_recordings(db, [2], "conversion")

    def test_missing_sentinel_does_not_create_root(self):
        missing = self.base / "missing"
        service = self.make_service(archive_root=missing)
        self.assertFalse(service.config()["available"])
        with self.assertRaises(ArchiveError):
            service.create_job([1])
        self.assertFalse(missing.exists())
        (self.nas / ".scanner-archive-id").unlink()
        with self.assertRaises(ArchiveError):
            self.service.create_job([1])

    def test_wrong_sentinel(self):
        (self.nas / ".scanner-archive-id").write_text("other-nas")
        with self.assertRaises(ArchiveError):
            self.service.create_job([1])

    def test_backup_only_and_exact_converted_grouping(self):
        converted = self.add_converted()
        extras = [self.converted / "video-1_720_standard.mp4.converting", self.converted / "video-10_720_standard.mp4"]
        for extra in extras:
            extra.write_bytes(b"untouched")
        preview = self.service.preview("2026-01-01T00:00:00Z")
        self.assertEqual(preview["total"], 1)
        self.assertEqual(len(preview["items"][0]["files"]), 2)
        job = self.backup()
        self.assertEqual(job["file_count"], 2)
        self.assertTrue(converted.exists())
        self.assertTrue((self.videos / "video-1.webm").exists())
        for file in job["items"][0]["files"]:
            self.assertEqual(hashlib.sha256((self.nas / file["target"]).read_bytes()).hexdigest(), file["sha256"])
        manifest = self.nas / "jobs" / job["id"] / "manifest.json"
        self.assertEqual(hashlib.sha256(manifest.read_bytes()).hexdigest(), job["manifest_digest"])
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM recording_reservations").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT archived_at FROM scan_events").fetchone()[0])

    def test_confirmed_removal_history_and_local_totals(self):
        self.add_converted()
        job = self.backup()
        self.confirm(job)
        job = self.service.run_job(job["id"], "remove")
        self.assertEqual(job["state"], "completed", job["error"])
        self.assertEqual(job["removed_bytes"], job["total_bytes"])
        self.assertFalse((self.videos / "video-1.webm").exists())
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT coalesce(sum(size_bytes),0) FROM recordings").fetchone()[0], 0)
            row = db.execute("SELECT * FROM scan_events").fetchone()
            self.assertEqual(row["recording_id"], 1)
            self.assertEqual(row["video_deleted"], 0)
            self.assertEqual(row["archive_job_id"], job["id"])
            self.assertIsNotNone(row["archived_at"])
            self.assertTrue(row["recording_snapshot"])
            self.assertIn("Test NAS", row["archive_location"])
        self.assertTrue((self.nas / job["items"][0]["files"][0]["target"]).exists())

    def test_collision_never_overwrites(self):
        job = self.service.create_job([1])
        target = self.nas / job["items"][0]["files"][0]["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"unrelated")
        result = self.service.run_job(job["id"])
        self.assertEqual(result["state"], "error")
        self.assertEqual(target.read_bytes(), b"unrelated")
        self.assertTrue((self.videos / "video-1.webm").exists())

    def test_link_unsupported_fails_closed_and_retry(self):
        job = self.service.create_job([1])
        with patch("archive.os.link", side_effect=OSError("unsupported")):
            result = self.service.run_job(job["id"])
        self.assertEqual(result["state"], "error")
        self.assertEqual(list(self.nas.rglob("*.part")), [])
        self.service.resume(job["id"])
        self.assertEqual(self.service.run_job(job["id"])["state"], "verified")

    def test_mutation_before_backup_and_during_copy(self):
        job = self.service.create_job([1])
        (self.videos / "video-1.webm").write_bytes(b"changed")
        result = self.service.run_job(job["id"])
        self.assertEqual(result["state"], "error")
        self.assertIsNone(result["items"][0]["files"][0]["verified_at"])

    def test_mutation_late_converted_and_backup_corruption_block_cleanup(self):
        job = self.backup()
        late = self.add_converted()
        self.confirm(job)
        result = self.service.run_job(job["id"], "remove")
        self.assertEqual(result["state"], "interrupted")
        self.assertTrue((self.videos / "video-1.webm").exists())
        late.unlink()
        file = job["items"][0]["files"][0]
        (self.nas / file["target"]).write_bytes(b"corrupt")
        self.service.resume(job["id"])
        self.assertEqual(self.service.run_job(job["id"], "remove")["state"], "interrupted")
        self.assertTrue((self.videos / "video-1.webm").exists())

    def test_manifest_tamper_and_bad_confirmation(self):
        job = self.backup()
        prompt = self.service.removal_preview(job["id"])
        for token, digest in (("wrong", prompt["digest"]), (prompt["token"], "wrong")):
            with self.assertRaises(ArchiveError):
                self.service.confirm_removal(job["id"], token, digest)
        with self.connect() as db:
            db.execute("UPDATE archive_jobs SET token_expires=0")
        with self.assertRaises(ArchiveError):
            self.service.confirm_removal(job["id"], prompt["token"], prompt["digest"])
        prompt = self.service.removal_preview(job["id"])
        (self.nas / "jobs" / job["id"] / "manifest.json").write_text("{}")
        self.service.confirm_removal(job["id"], prompt["token"], prompt["digest"])
        self.assertEqual(self.service.run_job(job["id"], "remove")["state"], "interrupted")
        self.assertTrue((self.videos / "video-1.webm").exists())

    def test_duplicate_confirmation_rejected(self):
        job = self.backup()
        prompt = self.service.removal_preview(job["id"])
        self.service.confirm_removal(job["id"], prompt["token"], prompt["digest"])
        with self.assertRaises(ArchiveError):
            self.service.confirm_removal(job["id"], prompt["token"], prompt["digest"])

    def test_converted_unlink_failure_retains_original_and_resume(self):
        converted = self.add_converted()
        job = self.backup()
        self.confirm(job)
        original_unlink = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == converted:
                raise PermissionError("locked")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink):
            result = self.service.run_job(job["id"], "remove")
        self.assertEqual(result["state"], "interrupted")
        self.assertTrue((self.videos / "video-1.webm").exists())
        self.service.resume(job["id"])
        self.assertEqual(self.service.run_job(job["id"], "remove")["state"], "completed")

    def test_crash_after_unlink_explicit_resume_and_claim_retention(self):
        job = self.backup()
        self.confirm(job)
        real_update = self.service._update
        def fail_outcome(sql, args=()):
            if "SET removed_at" in sql:
                raise RuntimeError("simulated crash")
            return real_update(sql, args)
        with patch.object(self.service, "_update", fail_outcome):
            result = self.service.run_job(job["id"], "remove")
        self.assertEqual(result["state"], "interrupted")
        self.assertFalse((self.videos / "video-1.webm").exists())
        restarted = self.make_service()
        restarted.start()
        self.assertEqual(restarted.get_job(job["id"])["state"], "interrupted")
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM recording_reservations").fetchone()[0], 1)
        # Avoid async worker in this synchronous test.
        resumed = self.make_service()
        resumed.resume(job["id"])
        self.assertEqual(resumed.run_job(job["id"], "remove")["state"], "completed")

    def test_unexpected_missing_file_not_authorized(self):
        job = self.backup()
        self.confirm(job)
        (self.videos / "video-1.webm").unlink()
        result = self.service.run_job(job["id"], "remove")
        self.assertEqual(result["state"], "interrupted")
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM recordings").fetchone()[0], 1)

    def test_destination_outage_and_changed_config(self):
        job = self.backup()
        self.confirm(job)
        (self.nas / ".scanner-archive-id").write_text("wrong")
        result = self.service.run_job(job["id"], "remove")
        self.assertEqual(result["state"], "interrupted")
        self.assertTrue((self.videos / "video-1.webm").exists())
        changed = self.make_service(destination_id="wrong")
        with self.assertRaises(ArchiveError):
            changed.resume(job["id"])

    def test_mid_copy_mutation_and_full_disk(self):
        job = self.service.create_job([1])
        real_fsync = __import__("os").fsync
        def mutate(fd):
            (self.videos / "video-1.webm").write_bytes(b"mutated during copy")
            real_fsync(fd)
        with patch("archive.os.fsync", mutate):
            result = self.service.run_job(job["id"])
        self.assertEqual(result["state"], "error")
        self.assertFalse(list(self.nas.rglob("*.webm")))
        self.assertFalse(list(self.nas.rglob("*.part")))
        with self.connect() as db:
            release_recordings(db, [1], job["id"])
        job = self.service.create_job([1])
        with patch("archive.os.fsync", side_effect=OSError("disk full")):
            result = self.service.run_job(job["id"])
        self.assertEqual(result["state"], "error")
        self.assertTrue((self.videos / "video-1.webm").exists())

    def test_local_mutation_and_corrupted_partial(self):
        job = self.service.create_job([1])
        real_verify = self.service._verify
        def corrupt(file, path):
            if str(path).endswith(".part"):
                path.write_bytes(b"bad copy")
            real_verify(file, path)
        with patch.object(self.service, "_verify", corrupt):
            result = self.service.run_job(job["id"])
        self.assertEqual(result["state"], "error")
        self.assertTrue((self.videos / "video-1.webm").exists())
        self.service.resume(job["id"])
        job = self.service.run_job(job["id"])
        self.assertEqual(job["state"], "verified")
        (self.videos / "video-1.webm").write_bytes(b"new local bytes")
        self.confirm(job)
        self.assertEqual(self.service.run_job(job["id"], "remove")["state"], "interrupted")
        self.assertTrue((self.videos / "video-1.webm").exists())

    def test_finalize_transaction_failure_can_reconcile(self):
        job = self.backup()
        self.confirm(job)
        with self.connect() as db:
            db.execute("""CREATE TRIGGER fail_finalize BEFORE DELETE ON recordings
                BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        result = self.service.run_job(job["id"], "remove")
        self.assertEqual(result["state"], "interrupted")
        with self.connect() as db:
            row = db.execute("SELECT archived_at FROM scan_events").fetchone()
            self.assertIsNone(row[0])
            self.assertEqual(db.execute("SELECT count(*) FROM recordings").fetchone()[0], 1)
            db.execute("DROP TRIGGER fail_finalize")
        self.service.resume(job["id"])
        self.assertEqual(self.service.run_job(job["id"], "remove")["state"], "completed")

    def test_startup_removing_requires_explicit_resume(self):
        job = self.backup()
        self.confirm(job)
        restarted = self.make_service()
        restarted.start()
        self.assertEqual(restarted.get_job(job["id"])["state"], "interrupted")
        self.assertTrue((self.videos / "video-1.webm").exists())

    def test_cancel_unconfirmed_error_releases_claim_without_deleting(self):
        job = self.service.create_job([1])
        with patch('archive.os.link', side_effect=OSError('unsupported')):
            self.assertEqual(self.service.run_job(job['id'])['state'], 'error')
        self.assertEqual(self.service.cancel(job['id'])['state'], 'cancelled')
        self.assertEqual(self.service.run_job(job['id'])['state'], 'cancelled')
        self.assertTrue((self.videos / 'video-1.webm').exists())
        with self.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recording_reservations').fetchone()[0], 0)

    def test_prepare_failure_is_durable_and_retryable(self):
        with patch.object(self.service, '_inventory', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.service.create_job([1])
        job = self.service.list_jobs()['items'][0]
        self.assertEqual(job['state'], 'error')
        self.assertEqual(job['file_count'], 0)
        self.service.resume(job['id'])
        self.assertEqual(self.service.run_job(job['id'])['state'], 'verified')

    def test_confirmation_queues_large_hash_work_without_hashing_sources(self):
        job = self.backup()
        prompt = self.service.removal_preview(job['id'])
        with patch.object(self.service, '_verify', side_effect=AssertionError('must run in worker')):
            result = self.service.confirm_removal(job['id'], prompt['token'], prompt['digest'])
        self.assertEqual(result['state'], 'removal_pending')
        self.assertTrue((self.videos / 'video-1.webm').exists())
        self.assertEqual(self.service.run_job(job['id'], 'remove')['state'], 'completed')

    def test_website_settings_save_test_restart_and_job_protection(self):
        drive = self.base / 'backup-drive'
        drive.mkdir()
        service = self.make_service(settings_root=drive)
        result = service.configure('Scanner/OldVideos', 'Second hard drive', save=True)
        self.assertTrue(result['saved'])
        self.assertGreater(result['free_bytes'], 0)
        self.assertTrue((drive / 'Scanner/OldVideos/.scanner-archive-id').exists())
        self.assertEqual(service.configure('Scanner/OldVideos', 'Second hard drive')['destination_id'], result['destination_id'])
        restarted = self.make_service(settings_root=drive)
        self.assertEqual(restarted.root, drive / 'Scanner/OldVideos')
        self.assertTrue(restarted.config()['available'])
        job = restarted.create_job([1])
        with self.assertRaises(ArchiveError):
            restarted.configure('Other', 'Other drive', save=True)
        restarted.run_job(job['id'])
        restarted.cancel(job['id'])
        restarted.configure('Other', 'Other folder', save=True)
        with self.assertRaises(ArchiveError):
            restarted._check_job(job)

    def test_website_settings_reject_escape_missing_mount_and_overlap(self):
        service = self.make_service(settings_root=self.base)
        for folder in ('../outside', '/outside', 'C:/backup', 'bad\\\\path', ''):
            with self.assertRaises(ArchiveError):
                service.configure(folder, 'Backup', save=True)
        with self.assertRaises(ArchiveError):
            service.configure('videos', 'Local videos', save=True)
        self.assertFalse((self.videos / '.scanner-archive-id').exists())
        missing = self.make_service(settings_root=self.base / 'unmounted')
        with self.assertRaises(ArchiveError):
            missing.configure('Backup', 'Drive', save=True)
        self.assertFalse((self.base / 'unmounted').exists())

    def test_preview_batches_database_and_directory_reads(self):
        self.add_converted()
        self.add_recording(2)
        with self.connect() as db:
            reserve_recordings(db, [2], 'active', 'archive')
        real_scan = __import__('os').scandir
        with patch.object(self.service, '_inventory', side_effect=AssertionError('preview must not run full inventory')):
            with patch.object(self.service, '_rows', wraps=self.service._rows) as queries:
                with patch('archive.os.scandir', wraps=real_scan) as scans:
                    result = self.service.preview('2026-01-01T00:00:00Z')
        self.assertEqual(queries.call_count, 2)
        self.assertEqual(scans.call_count, 1)
        self.assertEqual(len(result['items'][0]['files']), 2)
        self.assertIn('busy', result['items'][1]['error'])
        # A full inventory is still mandatory when a backup is actually started.
        with patch.object(self.service, '_inventory', wraps=self.service._inventory) as inventory:
            self.service.create_job([1])
        self.assertEqual(inventory.call_count, 1)

    def test_preview_refreshes_converted_listing_without_stale_cache(self):
        self.assertEqual(self.service.preview('2026-01-01T00:00:00Z')['items'][0]['converted_bytes'], 0)
        converted = self.add_converted()
        self.assertEqual(self.service.preview('2026-01-01T00:00:00Z')['items'][0]['converted_bytes'], converted.stat().st_size)
        converted.unlink()
        self.assertEqual(self.service.preview('2026-01-01T00:00:00Z')['items'][0]['converted_bytes'], 0)

    def test_root_overlap_rejected(self):
        service = self.make_service(archive_root=self.videos)
        with self.assertRaises(ArchiveError):
            service.create_job([1])

    def test_no_unconfirmed_removal_and_path_validation(self):
        job = self.backup()
        with self.assertRaises(ArchiveError):
            self.service.run_job(job["id"], "remove")
        with self.assertRaises(ArchiveError):
            self.service._target("../outside")
        with self.assertRaises(ArchiveError):
            self.service._source("original", "../outside")
        with self.assertRaises(ArchiveError):
            self.service.preview("2026-01-01")
        with self.assertRaises(ArchiveError):
            self.service.create_job([1, 1])


class AutomaticArchiveTests(unittest.TestCase):
    setUp = ArchiveServiceTests.setUp
    connect = ArchiveServiceTests.connect
    converted_name = staticmethod(ArchiveServiceTests.converted_name)
    make_service = ArchiveServiceTests.make_service
    add_recording = ArchiveServiceTests.add_recording
    add_converted = ArchiveServiceTests.add_converted

    def automatic(self, date='2025-05-31T10:00:00+00:00', **kwargs):
        from datetime import datetime
        return self.service.create_automatic_run(now=datetime.fromisoformat(date), **kwargs)

    def drain(self, run):
        for _ in range(20):
            run = self.service.run_automatic_run(run['id'])
            if run['state'] not in ('queued', 'running'):
                return run
        self.fail('Automatic run did not finish')

    def test_calendar_clamp_leap_and_strict_boundary(self):
        from datetime import datetime
        for now, expected in [('2024-05-31', '2024-02-29'), ('2025-05-31', '2025-02-28'),
                              ('2025-01-31', '2024-10-31'), ('2025-06-30', '2025-03-30')]:
            actual = self.service._calendar_cutoff(datetime.fromisoformat(now + 'T10:00:00+00:00'))
            self.assertTrue(actual.startswith(expected), actual)
        self.add_recording(2)
        self.add_recording(3)
        with self.connect() as db:
            db.execute("UPDATE recordings SET created_at='2025-02-28T10:00:00+00:00' WHERE id=2")
            db.execute("UPDATE recordings SET created_at='2025-02-28T10:00:00.001+00:00' WHERE id=3")
        run = self.drain(self.automatic())
        self.assertEqual(run['completed_count'], 1)
        self.assertTrue((self.videos / 'video-2.webm').exists())
        self.assertTrue((self.videos / 'video-3.webm').exists())
        self.assertEqual(run['cutoff'], '2025-02-28T10:00:00+00:00')

    def test_automatic_preview_is_metadata_only(self):
        with patch.object(self.service, '_source', side_effect=AssertionError('preview must not scan disk')):
            result = self.service.automatic_preview()
        self.assertEqual(result['eligible_count'], 1)
        self.assertGreater(result['original_bytes'], 0)

    def test_automatic_converted_history_and_progress(self):
        converted = self.add_converted()
        run = self.drain(self.automatic())
        self.assertEqual(run['state'], 'completed')
        self.assertEqual(run['completed_count'], 1)
        self.assertFalse(converted.exists())
        self.assertGreater(run['bytes_processed'], run['removed_bytes'])
        self.assertEqual(run['verified_bytes'], run['removed_bytes'])
        self.assertEqual(run['removed_bytes'], 1300 + len(b'converted-bytes'))
        job = self.service.list_jobs()['items'][0]
        self.assertIsNotNone(job['confirmed_at'])
        with self.connect() as db:
            event = db.execute('SELECT * FROM scan_events').fetchone()
            self.assertEqual(event['archive_job_id'], job['id'])
            self.assertEqual(event['video_deleted'], 0)

    def test_skips_busy_missing_and_continues_after_fault(self):
        self.add_recording(2)
        self.add_recording(3)
        self.add_recording(4)
        (self.videos / 'video-2.webm').unlink()
        with self.connect() as db:
            reserve_recordings(db, [3], 'upload', 'save')
        run = self.automatic()
        real_copy = self.service._copy
        def fail_first(file):
            if file['recording_id'] == 1:
                raise OSError('credentials and private mount must not leak')
            return real_copy(file)
        with patch.object(self.service, '_copy', side_effect=fail_first):
            run = self.drain(run)
        self.assertEqual(run['skipped_count'], 2)
        self.assertEqual(run['error_count'], 1)
        self.assertEqual(run['completed_count'], 1)
        self.assertEqual(run['error'], 'OSError')
        self.assertTrue((self.videos / 'video-1.webm').exists())
        with self.connect() as db:
            self.assertIsNone(db.execute('SELECT * FROM recording_reservations WHERE recording_id=1').fetchone())

    def test_pause_during_copy_resume_and_manual_bypass_rejected(self):
        run = self.automatic()
        real_checkpoint = self.service._automatic_checkpoint
        def pause(size=0, stage=None, **kwargs):
            if stage == 'copying':
                self.service.control_automatic_run(run['id'], 'pause')
            return real_checkpoint(size, stage, **kwargs)
        with patch.object(self.service, '_automatic_checkpoint', side_effect=pause):
            run = self.service.run_automatic_run(run['id'])
        self.assertEqual(run['state'], 'paused')
        self.assertTrue((self.videos / 'video-1.webm').exists())
        self.assertFalse(list(self.nas.rglob('*.part')))
        job = self.service.get_job(run['current_job_id'])
        for method in (self.service.resume, self.service.cancel, self.service.removal_preview):
            with self.assertRaises(ArchiveError):
                method(job['id'])
        with self.assertRaises(ArchiveError):
            self.service.confirm_removal(job['id'], 'token', 'digest')
        with self.assertRaises(ArchiveError):
            self.service.run_job(job['id'])
        self.service.control_automatic_run(run['id'], 'resume')
        self.assertEqual(self.drain(run)['state'], 'completed')

    def test_upload_yields_without_blocking_manual_work(self):
        import threading
        busy = threading.Event()
        self.service.io_busy = busy.is_set
        run = self.automatic()
        busy.set()
        self.assertEqual(self.service.run_automatic_run(run['id'])['state'], 'waiting_io')
        self.assertEqual(self.service.list_jobs()['total'], 0)
        self.add_recording(2)
        manual = self.service.create_job([2])
        self.assertEqual(self.service.run_job(manual['id'])['state'], 'verified')
        self.service.cancel(manual['id'])
        busy.clear()
        self.assertEqual(self.drain(run)['state'], 'completed')

    def test_stop_during_hash_preserves_sources_and_copies(self):
        run = self.automatic()
        real_checkpoint = self.service._automatic_checkpoint
        def stop(size=0, stage=None, **kwargs):
            if stage == 'hashing':
                self.service.control_automatic_run(run['id'], 'stop')
            return real_checkpoint(size, stage, **kwargs)
        with patch.object(self.service, '_automatic_checkpoint', side_effect=stop):
            run = self.service.run_automatic_run(run['id'])
        self.assertEqual(run['state'], 'stopped')
        self.assertTrue((self.videos / 'video-1.webm').exists())
        self.assertEqual(self.service.run_automatic_run(run['id'])['state'], 'stopped')
        with self.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM recording_reservations').fetchone()[0], 0)

    def test_pause_after_converted_unlink_reconciles_on_explicit_resume(self):
        converted = self.add_converted()
        run = self.automatic()
        real_update = self.service._update
        def pause_after_unlink(sql, args=()):
            result = real_update(sql, args)
            if 'SET removed_at' in sql:
                self.service.control_automatic_run(run['id'], 'pause')
            return result
        with patch.object(self.service, '_update', side_effect=pause_after_unlink):
            run = self.service.run_automatic_run(run['id'])
        self.assertEqual(run['state'], 'paused')
        self.assertFalse(converted.exists())
        self.assertTrue((self.videos / 'video-1.webm').exists())
        self.service.control_automatic_run(run['id'], 'resume')
        self.assertEqual(self.drain(run)['state'], 'completed')

    def test_restart_never_requeues_automatic_children(self):
        run = self.automatic()
        # Simulate a crash after queued child was persisted, before worker begins.
        job = self.service._create_job([1], automatic_run_id=run['id'])
        restarted = self.make_service()
        with patch('archive.threading.Thread'):
            restarted.start()
        self.assertEqual(restarted.get_automatic_run(run['id'])['state'], 'interrupted')
        self.assertTrue(restarted._queue.empty())
        self.assertEqual(restarted.get_job(job['id'])['state'], 'queued')
        self.service.control_automatic_run(run['id'], 'resume')
        self.assertEqual(self.drain(run)['state'], 'completed')

    def test_corruption_never_deletes_and_explicit_retry(self):
        run = self.automatic()
        real_remove = self.service._remove
        def corrupt(job):
            file = job['items'][0]['files'][0]
            (self.nas / file['target']).write_bytes(b'bad backup')
            return real_remove(job)
        with patch.object(self.service, '_remove', side_effect=corrupt):
            run = self.drain(run)
        self.assertEqual(run['state'], 'interrupted')
        self.assertTrue((self.videos / 'video-1.webm').exists())
        job = self.service.list_jobs()['items'][0]
        file = job['items'][0]['files'][0]
        (self.nas / file['target']).write_bytes((self.videos / 'video-1.webm').read_bytes())
        self.service.control_automatic_run(run['id'], 'resume')
        self.assertEqual(self.drain(run)['state'], 'completed')

    def test_stop_after_intent_requires_explicit_recovery(self):
        converted = self.add_converted()
        run = self.automatic()
        real_update = self.service._update
        def stop_after_unlink(sql, args=()):
            result = real_update(sql, args)
            if 'SET removed_at' in sql:
                self.service.control_automatic_run(run['id'], 'stop')
            return result
        with patch.object(self.service, '_update', side_effect=stop_after_unlink):
            run = self.service.run_automatic_run(run['id'])
        self.assertEqual(run['state'], 'interrupted')
        self.assertEqual(run['stage'], 'stopped_requires_review')
        self.assertTrue(self.service.automatic_busy)
        self.assertFalse(converted.exists())
        self.assertTrue((self.videos / 'video-1.webm').exists())
        self.service.control_automatic_run(run['id'], 'resume')
        self.assertEqual(self.drain(run)['state'], 'completed')

    def test_age_rechecked_immediately_before_unlink(self):
        run = self.automatic()
        real_checkpoint = self.service._automatic_checkpoint
        def mutate(size=0, stage=None, **kwargs):
            if kwargs.get('destructive'):
                with self.connect() as db:
                    db.execute("UPDATE recordings SET created_at='2025-05-31T10:00:00+00:00' WHERE id=1")
            return real_checkpoint(size, stage, **kwargs)
        with patch.object(self.service, '_automatic_checkpoint', side_effect=mutate):
            run = self.drain(run)
        self.assertEqual(run['state'], 'interrupted')
        self.assertTrue((self.videos / 'video-1.webm').exists())
        self.assertEqual(run['removed_bytes'], 0)

    def test_throttle_covers_copy_and_hash_only_for_automatic(self):
        run = self.automatic(rate_mbps=5)
        with patch.object(self.service._wake, 'wait') as waits:
            # Monotonic still advances but no disk test is large: inspect bounded waits.
            run = self.drain(run)
        self.assertTrue(waits.called)
        self.assertTrue(all(0 < call[0][0] <= 0.05 for call in waits.call_args_list))
        self.assertEqual(run['state'], 'completed')
        self.add_recording(2)
        job = self.service.create_job([2])
        with patch.object(self.service._wake, 'wait', side_effect=AssertionError('manual throttled')):
            self.assertEqual(self.service.run_job(job['id'])['state'], 'verified')

    def test_rate_single_run_and_destination_protection(self):
        for rate in (0, 21, float('nan'), True, '5'):
            with self.assertRaises(ArchiveError):
                self.automatic(rate_mbps=rate)
        run = self.automatic(rate_mbps=20)
        self.service.control_automatic_run(run['id'], 'pause')
        self.assertTrue(self.service.automatic_busy)
        with self.assertRaises(ArchiveError):
            self.automatic()
        service = self.make_service(settings_root=self.base)
        with self.assertRaises(ArchiveError):
            service.configure('different', 'Other', save=True)
        self.service.control_automatic_run(run['id'], 'stop')
        self.assertFalse(self.service.automatic_busy)


if __name__ == "__main__":
    unittest.main()
