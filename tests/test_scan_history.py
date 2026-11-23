import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError


class ScanHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Set storage BEFORE executing app.py: startup must never touch real data.
        with patch.dict(os.environ, {"DATA_DIR": self.temp.name, "ARCHIVE_WORKER_DISABLED": "1", "ARCHIVE_DIR": "", "ARCHIVE_DESTINATION_ID": ""}):
            spec = importlib.util.spec_from_file_location("isolated_scanner", Path(__file__).resolve().parents[1] / "app.py")
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        self.module.app.config["TESTING"] = True
        self.client = self.module.app.test_client()
        self.validate_video_file = self.module.validate_video_file
        self.repair_webm_metadata = self.module.repair_webm_metadata
        for name in ("validate_video_file", "repair_webm_metadata"):
            mock = patch.object(self.module, name, return_value=None)
            mock.start()
            self.addCleanup(mock.stop)

    def event(self, submission="one", tracking="ABC", mode="packing", source="scan"):
        response = self.client.post("/api/scan-events", json=dict(submission_id=submission, tracking_number=tracking, mode=mode, source=source))
        self.assertIn(response.status_code, (200, 201))
        return response.get_json()

    def events(self, **filters):
        response = self.client.get("/api/scan-events", query_string=filters)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def upload(self, event=None, invoice="ABC", mode="packing"):
        data = dict(invoice=invoice, mode=mode, video=(io.BytesIO(b"test video"), "test.webm"))
        if event:
            data.update(scan_event_id=str(event["id"]), scan_event_token=event["mutation_token"])
        return self.client.post("/api/recordings", data=data)

    def lookup(self, event):
        return self.client.post(f"/api/scan-events/{event['id']}/lookup", json={"mutation_token": event["mutation_token"]})

    def test_schema_repeated_startup_and_idempotent_submissions(self):
        self.module.init_storage()
        first = self.event()
        self.assertEqual(first, self.event())
        other = self.event("two")
        self.assertNotEqual(first["id"], other["id"])
        self.assertEqual(self.client.post("/api/scan-events", json=dict(submission_id="one", tracking_number="XYZ", mode="packing")).status_code, 409)
        items = self.events()["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["lookup_status"], "not_requested")
        self.assertEqual(items[0]["video_status"], "not_recorded")
        self.assertNotIn("mutation_token", str(items))

    def test_lookup_success_is_persisted_and_not_repeated(self):
        event = self.event()
        normalized = self.module.normalize_order_payload({"order_sn": "ORDER", "package_number": "PKG", "items": [{"item_name": "Product", "model_quantity_purchased": 2}]}, "ABC")
        def service(tracking):
            self.assertEqual(self.events()["items"][0]["lookup_status"], "pending")
            return normalized, 200
        with patch.object(self.module, "lookup_order", side_effect=service) as helper:
            self.assertEqual(self.lookup(event).get_json(), normalized)
            self.assertEqual(self.lookup(event).get_json(), normalized)
            self.assertEqual(helper.call_count, 1)
        self.assertEqual(self.events(q="ORDER")["items"][0]["items"], normalized["items"])
        self.assertEqual(self.events(lookup_status="success")["total"], 1)

    def test_lookup_errors_tokens_and_interrupted_claim(self):
        for code in (404, 503, 502):
            event = self.event(str(code))
            with patch.object(self.module, "lookup_order", return_value=({"error": "Safe failure"}, code)) as helper:
                self.assertEqual(self.lookup(event).status_code, code)
                self.assertEqual(self.lookup(event).status_code, code)
                self.assertEqual(helper.call_count, 1)
        event = self.event("interrupt")
        with patch.object(self.module, "lookup_order", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.lookup(event)
        self.assertEqual(self.lookup(event).status_code, 409)
        self.assertEqual(self.client.post(f"/api/scan-events/{event['id']}/lookup", json={"mutation_token": "wrong"}).status_code, 403)
        self.assertEqual(self.events(lookup_status="pending")["total"], 1)

    def test_upstream_helper_and_legacy_get(self):
        with patch.object(self.module, "ORDER_LOOKUP_API_KEY", ""):
            self.assertEqual(self.module.lookup_order("ABC")[1], 503)
            self.assertEqual(self.client.get("/api/orders/by-tracking-number?tracking_number=ABC").status_code, 503)
        with patch.object(self.module, "ORDER_LOOKUP_API_KEY", "secret"):
            for error, code in [(HTTPError("url", 404, "", {}, None), 404), (HTTPError("url", 401, "", {}, None), 502), (HTTPError("url", 429, "", {}, None), 503), (URLError("offline"), 502)]:
                with patch.object(self.module, "urlopen", side_effect=error):
                    self.assertEqual(self.module.lookup_order("ABC")[1], code)
            with patch.object(self.module, "urlopen") as upstream:
                upstream.return_value.__enter__.return_value.read.return_value = b'{"order_sn":"O"}'
                self.assertEqual(self.module.lookup_order("ABC")[0]["order_sn"], "O")
                upstream.return_value.__enter__.return_value.read.return_value = b'invalid'
                self.assertEqual(self.module.lookup_order("ABC")[1], 502)

    def test_link_upload_delete_preserves_snapshot_and_size(self):
        event = self.event()
        with patch.object(self.module, "lookup_order", return_value=({"order_sn": "ORDER", "items": []}, 200)):
            self.lookup(event)
        recording = self.upload(event)
        self.assertEqual(recording.status_code, 201)
        item = self.events()["items"][0]
        self.assertEqual(item["video_status"], "available")
        self.assertEqual(item["recording"]["size_bytes"], 10)
        self.assertEqual(self.upload(event).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/recordings/{recording.get_json()['id']}").status_code, 200)
        self.module.init_storage()
        item = self.events()["items"][0]
        self.assertEqual(item["video_status"], "deleted")
        self.assertEqual(item["order_sn"], "ORDER")
        self.assertEqual(item["recording"]["size_bytes"], 10)
        self.assertNotIn("video_url", item)
        self.assertNotIn("video_url", item["recording"])
        self.assertEqual(self.upload(event).status_code, 409)

    def test_mp4_upload_mime_extension_and_event_association(self):
        for index, content_type in enumerate(("video/mp4", "")):
            with self.subTest(content_type=content_type):
                event = self.event(f"mp4-{index}")
                response = self.client.post("/api/recordings", data=dict(
                    invoice="ABC", mode="packing", duration_ms="2500",
                    scan_event_id=str(event["id"]), scan_event_token=event["mutation_token"],
                    video=(io.BytesIO(b"test mp4 video"), "camera.MP4", content_type),
                ))
                self.assertEqual(response.status_code, 201)
                recording = response.get_json()
                self.assertTrue(recording["filename"].endswith(".mp4"))
                self.assertEqual(recording["content_type"], "video/mp4")
                with self.module.db_connect() as db:
                    saved = db.execute("SELECT original_name FROM recordings WHERE id = ?", (recording["id"],)).fetchone()
                self.assertEqual(saved["original_name"], "camera.MP4")
                self.assertTrue(self.module.recording_path(recording["filename"]).is_file())
                item = next(item for item in self.events()["items"] if item["id"] == event["id"])
                self.assertEqual(item["video_status"], "available")
                self.assertEqual(item["recording"]["id"], recording["id"])
                self.assertEqual(item["recording"]["content_type"], "video/mp4")
                self.module.validate_video_file.assert_called_with(unittest.mock.ANY, 2.5)
                self.assertEqual(self.module.validate_video_file.call_count, (index + 1) * 2)

    def test_mp4_upload_still_requires_ffprobe_validation(self):
        cases = (
            ('{"streams": [], "format": {"duration": "2.5"}}', "readable video stream"),
            ('{"streams": [{"codec_type": "video"}], "format": {"duration": "0"}}', "duration could not be verified"),
            ('{"streams": [{"codec_type": "video"}], "format": {"duration": "60"}}', "duration could not be verified"),
            ('invalid JSON', "could not be read"),
        )
        for index, (probe_output, error) in enumerate(cases):
            with self.subTest(error=error, probe_output=probe_output):
                event = self.event(f"invalid-mp4-{index}")
                probe = self.module.subprocess.CompletedProcess([], 0, stdout=probe_output)
                with patch.object(self.module, "validate_video_file", side_effect=self.validate_video_file), \
                     patch.object(self.module.shutil, "which", return_value="ffprobe"), \
                     patch.object(self.module.subprocess, "run", return_value=probe) as run:
                    response = self.client.post("/api/recordings", data=dict(
                        invoice="ABC", mode="packing", duration_ms="2500",
                        scan_event_id=str(event["id"]), scan_event_token=event["mutation_token"],
                        video=(io.BytesIO(b"invalid mp4"), "camera.mp4", "video/mp4"),
                    ))
                self.assertEqual(response.status_code, 400)
                self.assertIn(error, response.get_json()["error"])
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][0], "ffprobe")
                self.assertTrue(run.call_args.args[0][-1].endswith(".mp4"))
                self.assertEqual(list(self.module.VIDEO_DIR.rglob("*.mp4")), [])
                self.assertEqual(self.client.get("/api/recordings").get_json()["total"], 0)
                self.assertTrue(all(item["video_status"] == "not_recorded" for item in self.events()["items"]))

    def test_webm_missing_duration_is_repaired_before_final_validation(self):
        import json
        event = self.event("phone-webm")
        order = []
        outputs = [
            {"streams": [{"codec_type": "video"}], "format": {}},
            {"streams": [{"codec_type": "video"}], "format": {"duration": "2.5"}},
        ]
        def probe(*args, **kwargs):
            order.append("probe")
            return self.module.subprocess.CompletedProcess([], 0, stdout=json.dumps(outputs.pop(0)))
        with patch.object(self.module, "validate_video_file", side_effect=self.validate_video_file), \
             patch.object(self.module.shutil, "which", return_value="ffprobe"), \
             patch.object(self.module.subprocess, "run", side_effect=probe), \
             patch.object(self.module, "repair_webm_metadata", side_effect=lambda path: order.append("repair")):
            response = self.client.post("/api/recordings", data=dict(
                invoice="ABC", mode="packing", duration_ms="2500",
                scan_event_id=str(event["id"]), scan_event_token=event["mutation_token"],
                video=(io.BytesIO(b"\x1a\x45\xdf\xa3test"), "phone.webm", "video/webm"),
            ))
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(order, ["probe", "repair", "probe"])
        self.assertEqual(self.events()["items"][0]["video_status"], "available")

    def test_webm_unverifiable_or_mismatched_duration_after_repair_is_rejected(self):
        import json
        for index, duration in enumerate((None, "0", "NaN", "Infinity", "60")):
            with self.subTest(duration=duration):
                event = self.event(f"bad-phone-{index}")
                payload = {"streams": [{"codec_type": "video"}], "format": {"duration": duration}}
                result = self.module.subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))
                with patch.object(self.module, "validate_video_file", side_effect=self.validate_video_file), \
                     patch.object(self.module.shutil, "which", return_value="ffprobe"), \
                     patch.object(self.module.subprocess, "run", return_value=result):
                    response = self.client.post("/api/recordings", data=dict(
                        invoice="ABC", mode="packing", duration_ms="2500",
                        scan_event_id=str(event["id"]), scan_event_token=event["mutation_token"],
                        video=(io.BytesIO(b"\x1a\x45\xdf\xa3test"), "phone.webm", "video/webm"),
                    ))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(list(self.module.VIDEO_DIR.rglob("*.webm")), [])
                self.assertEqual(self.client.get("/api/recordings").get_json()["total"], 0)

    def test_mobile_video_stream_duration_fallback_never_uses_audio_only_duration(self):
        import json
        path = Path(self.temp.name) / "phone.mp4"
        path.write_bytes(b"test")
        for streams, expected in [
            ([{"codec_type": "video", "duration": "2.5"}], None),
            ([{"codec_type": "video"}, {"codec_type": "audio", "duration": "2.5"}], "Video duration"),
        ]:
            result = self.module.subprocess.CompletedProcess([], 0, stdout=json.dumps({"streams": streams, "format": {}}))
            with patch.object(self.module.shutil, "which", return_value="ffprobe"), \
                 patch.object(self.module.subprocess, "run", return_value=result):
                error = self.validate_video_file(path, 2.5)
            if expected is None:
                self.assertIsNone(error)
            else:
                self.assertIn(expected, error)

    def test_real_browser_style_webm_without_duration_can_be_saved(self):
        import shutil
        import subprocess
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg and ffprobe required")
        # A non-seekable WebM pipe, like MediaRecorder, cannot write duration.
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x120:r=10",
            "-t", "2", "-an", "-c:v", "libvpx", "-f", "webm", "pipe:1",
        ], check=True, capture_output=True, timeout=30)
        event = self.event("real-phone-webm")
        with patch.object(self.module, "validate_video_file", side_effect=self.validate_video_file), \
             patch.object(self.module, "repair_webm_metadata", wraps=self.repair_webm_metadata):
            response = self.client.post("/api/recordings", data=dict(
                invoice="ABC", mode="packing", duration_ms="2000",
                scan_event_id=str(event["id"]), scan_event_token=event["mutation_token"],
                video=(io.BytesIO(result.stdout), "phone.webm", "video/webm"),
            ))
        self.assertEqual(response.status_code, 201, response.get_json())
        saved_path = self.module.recording_path(response.get_json()["filename"])
        self.assertIsNone(self.validate_video_file(saved_path, 2.0))

    def test_upload_rejects_wrong_identity_and_falls_back_for_old_callers(self):
        event = self.event(source="manual_recording")
        self.assertEqual(self.upload(event, invoice="DIFFERENT").status_code, 409)
        self.assertEqual(self.upload(event, mode="return").status_code, 409)
        bad = dict(event, mutation_token="bad")
        self.assertEqual(self.upload(bad).status_code, 409)
        self.assertEqual(self.upload(event, invoice="A B C").status_code, 409)
        self.assertEqual(self.upload().status_code, 201)
        self.assertEqual(self.events()["items"][0]["source"], "manual_upload")
        self.assertEqual(self.events()["items"][0]["lookup_status"], "not_requested")

    def test_missing_filter_runs_before_pagination(self):
        for i in range(6):
            event = self.event(str(i))
            self.assertEqual(self.upload(event).status_code, 201)
        items = self.events()["items"]
        for item in items[::2]:
            self.module.recording_path(item["recording"]["filename"]).unlink()
        result = self.events(video_status="missing", per_page=2)
        self.assertEqual(result["total"], 3)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all(item["video_status"] == "missing" and "video_url" not in item for item in result["items"]))
        self.assertEqual(self.events(video_status="missing", per_page=2, page=999999999999999999999)["page"], 2)
        self.assertEqual(self.events(per_page=999)["per_page"], 50)

    def test_legacy_backfill_idempotent_after_original_deletion(self):
        with self.module.db_connect() as db:
            db.execute("INSERT INTO recordings (invoice, mode, filename, size_bytes, created_at) VALUES ('old', 'packing', 'old.webm', 123, '2020-01-01T12:00:00+00:00')")
        self.module.init_storage()
        self.module.init_storage()
        item = self.events()["items"][0]
        self.assertEqual(item["source"], "imported_recording")
        self.assertEqual(item["timestamp_basis"], "saved_at")
        self.assertEqual(item["tracking_number"], "OLD")
        self.assertEqual(item["video_status"], "missing")
        self.client.delete(f"/api/recordings/{item['recording']['id']}")
        self.module.init_storage()
        self.assertEqual(self.events()["total"], 1)
        self.assertEqual(self.events()["items"][0]["video_status"], "deleted")

    def test_filters_utc_bounds_stable_pages_and_invalid_input(self):
        for i in range(4):
            self.event(str(i), tracking=f"LABEL{i}", mode="return" if i == 3 else "packing")
        with self.module.db_connect() as db:
            db.execute("UPDATE scan_events SET submitted_at = '2025-01-01T00:00:00+00:00'")
            db.execute("UPDATE scan_events SET submitted_at = '2025-01-02T00:00:00+00:00' WHERE id = 4")
        first = self.events(per_page=2)["items"]
        second = self.events(per_page=2, page=2)["items"]
        self.assertEqual([i["id"] for i in first + second], [4, 3, 2, 1])
        self.assertEqual(self.events(date_from="2025-01-01", date_to="2025-01-01")["total"], 3)
        self.assertEqual(self.events(q="label3", mode="return")["total"], 1)
        for query in (dict(date_from="bad"), dict(mode="bad"), dict(lookup_status="bad"), dict(video_status="bad")):
            self.assertEqual(self.client.get("/api/scan-events", query_string=query).status_code, 400)
        for payload in ([], {}, dict(submission_id="x", tracking_number="bad space", mode="packing"), dict(submission_id="x", tracking_number="ABC", mode=[])):
            self.assertEqual(self.client.post("/api/scan-events", json=payload).status_code, 400)
        self.assertEqual(self.client.get("/api/orders/by-tracking-number?tracking_number=bad%20space").status_code, 400)

    def test_failed_link_transaction_removes_new_file(self):
        event = self.event()
        original = self.module.validate_upload_event
        calls = 0
        def validate(*args):
            nonlocal calls
            calls += 1
            return original(*args) if calls == 1 else "Concurrent association conflict"
        with patch.object(self.module, "validate_upload_event", side_effect=validate):
            self.assertEqual(self.upload(event).status_code, 409)
        self.assertEqual(list(self.module.VIDEO_DIR.rglob("*.webm")), [])
        self.assertEqual(self.client.get("/api/recordings").get_json()["total"], 0)
        self.assertEqual(self.events()["items"][0]["video_status"], "not_recorded")


if __name__ == "__main__":
    unittest.main()
