import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from archive import ArchiveService, reserve_recordings


class ArchiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.nas = root / 'nas'
        self.nas.mkdir()
        (self.nas / '.scanner-archive-id').write_text('integration-nas')
        with patch.dict(os.environ, {'DATA_DIR': str(root / 'local'), 'ARCHIVE_WORKER_DISABLED': '1', 'ARCHIVE_DIR': '', 'ARCHIVE_DESTINATION_ID': ''}):
            spec = importlib.util.spec_from_file_location('archive_test_app', Path(__file__).resolve().parents[1] / 'app.py')
            self.app_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app_module)
        m = self.app_module
        m.app.config['TESTING'] = True
        self.client = m.app.test_client()
        m.archive_service = ArchiveService(m.db_connect, m.recording_path, m.converted_path, m.converted_filename,
                                          list(m.CONVERT_RESOLUTIONS), list(m.CONVERT_QUALITIES), self.nas, 'integration-nas', 'Test NAS')
        for name in ('validate_video_file', 'repair_webm_metadata'):
            mock = patch.object(m, name, return_value=None)
            mock.start()
            self.addCleanup(mock.stop)
        response = self.client.post('/api/recordings', data={'invoice': 'TEST-LABEL', 'mode': 'packing', 'video': (io.BytesIO(b'disposable-video'), 'test.webm')})
        self.assertEqual(response.status_code, 201)
        self.recording = response.get_json()
        self.original = m.recording_path(self.recording['filename'])
        self.converted = m.converted_path(m.converted_filename(self.recording['filename'], '720p', 'small'))
        self.converted.parent.mkdir(parents=True, exist_ok=True)
        self.converted.write_bytes(b'disposable-converted-video')

    def post(self, path, payload):
        return self.client.post('/api/archive' + path, json=payload, headers={'Origin': 'http://localhost'})

    def test_actual_month_paths_backup_cleanup_and_history(self):
        preview = self.client.get('/api/archive/preview', query_string={'cutoff': '2099-01-01T00:00:00+00:00'}).get_json()
        self.assertEqual(preview['total'], 1)
        self.assertIsNone(preview['items'][0]['error'])
        self.assertEqual(len(preview['items'][0]['files']), 2)
        job = self.post('/jobs', {'recording_ids': [self.recording['id']]}).get_json()
        service = self.app_module.archive_service
        job = service.run_job(job['id'])
        self.assertEqual(job['state'], 'verified', job.get('error'))
        self.assertTrue(self.original.exists())
        self.assertTrue(self.converted.exists())
        history_before = self.client.get('/api/scan-events').get_json()['items'][0]
        self.assertEqual(history_before['video_status'], 'available')
        self.assertEqual(history_before['backup_verified_label'], 'Test NAS')
        for file in job['items'][0]['files']:
            self.assertTrue((self.nas / file['target']).exists())
        confirmation = self.post('/jobs/' + job['id'] + '/removal-preview', {}).get_json()
        response = self.post('/jobs/' + job['id'] + '/confirm-removal', confirmation)
        self.assertEqual(response.status_code, 200, response.get_json())
        job = service.run_job(job['id'], 'remove')
        self.assertEqual(job['state'], 'completed', job.get('error'))
        self.assertFalse(self.original.exists())
        self.assertFalse(self.converted.exists())
        history = self.client.get('/api/scan-events?video_status=archived').get_json()
        self.assertEqual(history['total'], 1)
        self.assertEqual(history['items'][0]['video_status'], 'archived')
        self.assertIn('Test NAS', history['items'][0]['archive_location'])
        self.assertNotIn('video_url', history['items'][0])
        self.assertEqual(self.client.get('/api/recordings').get_json()['total'], 0)
        self.app_module.init_storage()
        self.assertEqual(self.client.get('/api/scan-events?video_status=archived').get_json()['total'], 1)

    def test_archive_json_same_origin_and_private_tokens(self):
        self.assertEqual(self.client.post('/api/archive/jobs', json={'recording_ids': [self.recording['id']]}, headers={'Origin': 'https://other.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/archive/jobs', data='plain', headers={'Origin': 'http://localhost'}).status_code, 400)
        self.assertEqual(self.client.post('/api/archive/jobs', json={}).status_code, 403)
        job = self.post('/jobs', {'recording_ids': [self.recording['id']]}).get_json()
        result = self.app_module.archive_service.run_job(job['id'])
        self.assertEqual(result['state'], 'verified')
        self.post('/jobs/' + job['id'] + '/removal-preview', {})
        listing = self.client.get('/api/archive/jobs').get_json()
        self.assertNotIn('token_hash', str(listing))
        self.assertNotIn('token_expires', str(listing))
        self.assertEqual(self.client.get('/archive').status_code, 200)

    def test_nested_source_directory_symlink_is_rejected(self):
        target = self.original.parent.with_name('moved-month')
        self.original.parent.rename(target)
        try:
            self.original.parent.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            target.rename(self.original.parent)
            self.skipTest('Directory symlinks unavailable')
        self.addCleanup(self.original.parent.unlink)
        result = self.client.get('/api/archive/preview', query_string={'cutoff': '2099-01-01T00:00:00+00:00'}).get_json()
        self.assertIn('symlink', result['items'][0]['error'])
        self.assertEqual(self.post('/jobs', {'recording_ids': [self.recording['id']]}).status_code, 400)

    def test_settings_api_same_origin_and_persistence(self):
        drive = Path(self.temp.name) / 'drive'
        drive.mkdir()
        self.app_module.archive_service.settings_root = drive
        self.assertEqual(self.client.get('/archive/settings').status_code, 200)
        self.assertTrue(self.client.get('/api/archive/settings').get_json()['editable'])
        response = self.post('/settings', {'folder': 'ScannerArchive', 'label': 'Second disk'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()['saved'])
        self.assertEqual(self.post('/settings/test', {'folder': 'ScannerArchive', 'label': 'Second disk'}).status_code, 200)
        self.assertEqual(self.client.post('/api/archive/settings', json={'folder': 'X', 'label': 'X'}, headers={'Origin': 'https://other.example'}).status_code, 403)
        self.assertFalse((drive / 'X').exists())

    def test_automatic_endpoint_requires_authority_and_ignores_client_cutoff(self):
        self.assertEqual(self.post('/automatic', {'rate_mbps':5}).status_code, 400)
        self.assertEqual(self.client.post('/api/archive/automatic', json={'authorize_removal':True}, headers={'Origin':'https://untrusted.example'}).status_code, 403)
        before = self.client.get('/api/archive/automatic/preview').get_json()
        response = self.post('/automatic', {'authorize_removal':True, 'rate_mbps':5, 'cutoff':'2099-01-01', 'recording_ids':[self.recording['id']]})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertLess(response.get_json()['cutoff'], '2099')
        self.assertEqual(response.get_json()['total_count'], 0)
        self.assertEqual(before['eligible_count'], 0)
        self.assertTrue(self.original.exists())

    def test_automatic_cutoff_includes_old_only_with_actual_month_paths(self):
        from datetime import datetime, timezone
        service = self.app_module.archive_service
        with self.app_module.db_connect() as db:
            db.execute("UPDATE recordings SET created_at='2025-01-01T00:00:00+00:00' WHERE id=?", (self.recording['id'],))
        run = service.create_automatic_run(rate_mbps=20, now=datetime(2026, 11, 6, tzinfo=timezone.utc))
        self.assertEqual(run['total_count'], 1)
        self.assertEqual(run['cutoff'], '2026-08-06T00:00:00+00:00')
        self.assertTrue(self.original.exists())
        self.assertEqual(self.post('/automatic/' + run['id'] + '/control', {'action':'pause'}).status_code, 200)
        self.assertTrue(self.original.exists())

    def test_upload_activity_tracking_clears_after_validation_failure(self):
        observed = []
        def busy_validation(*args, **kwargs):
            observed.append(self.app_module.recording_io_busy())
            return 'test rejection'
        with patch.object(self.app_module, 'validate_video_file', side_effect=busy_validation):
            response = self.client.post('/api/recordings', data={'invoice':'BUSY','mode':'packing','video':(io.BytesIO(b'test'),'test.webm')})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(observed, [True])
        self.assertFalse(self.app_module.recording_io_busy())

    def test_busy_recording_blocks_preview_delete_and_conversion(self):
        with self.app_module.db_connect() as db:
            reserve_recordings(db, [self.recording['id']], 'other-job', 'archive')
        self.assertEqual(self.client.delete('/api/recordings/' + str(self.recording['id'])).status_code, 409)
        self.assertEqual(self.client.post('/api/convert', json={'recording_id': self.recording['id']}).status_code, 409)
        self.assertEqual(self.post('/jobs', {'recording_ids': [self.recording['id']]}).status_code, 409)
        self.assertTrue(self.original.exists())


if __name__ == '__main__':
    unittest.main()
