import json
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from model.VideoUploadHistory import (
    load_video_upload_history,
    normalize_video_identity,
    record_video_uploads,
)


class FakeRequest:
    def __init__(self, result=None):
        self.result = result or {}

    def execute(self):
        return self.result


class FakeFiles:
    def __init__(self):
        self.update_calls = []

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        return FakeRequest({"id": kwargs["fileId"], "trashed": True})


class FakeDriveService:
    def __init__(self):
        self.files_api = FakeFiles()

    def files(self):
        return self.files_api


def uploaded_record(file_id, file_name, md5):
    return {
        "id": file_id,
        "name": file_name,
        "mimeType": "video/mp4",
        "md5Checksum": md5,
        "modifiedTime": "2026-09-14T08:00:00Z",
        "size": "12345",
        "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
        "action": "uploaded",
        "local_file": f"C:/result/{file_name}",
        "relative_path": f"Alice/{file_name}",
        "local_task_date": "0910",
        "local_task_id": "7",
        "local_task_name": "title",
        "local_admin": "Alice",
        "local_video_duration_millis": 61_250,
    }


def confirmed_report(file_name):
    return {
        "attempted": True,
        "successful_files": [file_name],
        "failed_files": [],
    }


class VideoUploadHistoryTests(unittest.TestCase):
    def config(self, root):
        return {
            "video_upload_history_file": str(root / "VideoUploadHistory.json"),
            "video_upload_history_archive_dir": str(root / "archives"),
            "task_submission_log_file": str(root / "missing-audit.jsonl"),
            "video_upload_history_retention_days": 31,
            "video_upload_replace_old_enabled": True,
        }

    def test_compressed_prefix_does_not_change_video_identity(self):
        self.assertEqual(
            normalize_video_identity("[SHANA]Alice-0910-7-title.mp4"),
            normalize_video_identity("Alice-0910-7-title.mp4"),
        )

    def test_later_upload_trashes_old_same_video_and_links_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            file_name = "[SHANA]AliceMARKER-0910-7-title.mp4"
            old = uploaded_record("old-file", file_name, "old-md5")
            new = uploaded_record("new-file", file_name, "new-md5")
            record_video_uploads(
                config,
                [old],
                "2026-09-10",
                "02",
                confirmed_report(file_name),
                now=datetime.fromisoformat("2026-09-10T12:00:00+02:00"),
            )
            drive = FakeDriveService()

            result = record_video_uploads(
                config,
                [new],
                "2026-09-14",
                "01",
                confirmed_report(file_name),
                drive_service=drive,
                now=datetime.fromisoformat("2026-09-14T09:00:00+02:00"),
            )

            self.assertEqual(result["trashed"], ["old-file"])
            self.assertEqual(
                drive.files_api.update_calls[0],
                {
                    "fileId": "old-file",
                    "body": {"trashed": True},
                    "fields": "id,trashed",
                    "supportsAllDrives": True,
                },
            )
            records = load_video_upload_history(config)["records"]
            old_saved = next(item for item in records if item["drive_file_id"] == "old-file")
            new_saved = next(item for item in records if item["drive_file_id"] == "new-file")
            self.assertEqual(new_saved["duration_millis"], 61_250)
            self.assertEqual(old_saved["replacement"]["state"], "replaced")
            self.assertEqual(
                new_saved["replacement"]["replaces_file_ids"],
                ["old-file"],
            )

    def test_old_file_is_kept_when_new_task_sheet_link_is_not_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            file_name = "AliceMARKER-0910-7-title.mp4"
            record_video_uploads(
                config,
                [uploaded_record("old-file", file_name, "old-md5")],
                "2026-09-10",
                "02",
                confirmed_report(file_name),
                now=datetime.fromisoformat("2026-09-10T12:00:00+02:00"),
            )
            drive = FakeDriveService()
            failed_report = {
                "attempted": True,
                "successful_files": [],
                "failed_files": [{"file_name": file_name, "reason": "write failed"}],
            }

            result = record_video_uploads(
                config,
                [uploaded_record("new-file", file_name, "new-md5")],
                "2026-09-14",
                "01",
                failed_report,
                drive_service=drive,
                now=datetime.fromisoformat("2026-09-14T09:00:00+02:00"),
            )

            self.assertEqual(result["trashed"], [])
            self.assertEqual(drive.files_api.update_calls, [])
            records = load_video_upload_history(config)["records"]
            new_saved = next(item for item in records if item["drive_file_id"] == "new-file")
            self.assertEqual(new_saved["replacement"]["state"], "cleanup_deferred")

    def test_records_older_than_one_month_are_archived_as_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            old_name = "AliceMARKER-0701-1-old.mp4"
            new_name = "AliceMARKER-0914-2-new.mp4"
            record_video_uploads(
                config,
                [uploaded_record("old-file", old_name, "old-md5")],
                "2026-07-01",
                "01",
                confirmed_report(old_name),
                now=datetime.fromisoformat("2026-07-01T09:00:00+02:00"),
            )

            result = record_video_uploads(
                config,
                [uploaded_record("new-file", new_name, "new-md5")],
                "2026-09-14",
                "01",
                confirmed_report(new_name),
                now=datetime.fromisoformat("2026-09-14T09:00:00+02:00"),
            )

            self.assertEqual(len(result["archived"]), 1)
            archive_path = Path(result["archived"][0])
            self.assertTrue(archive_path.is_file())
            with zipfile.ZipFile(archive_path, "r") as archive:
                payload = json.loads(archive.read(archive.namelist()[0]).decode("utf-8"))
            self.assertEqual(payload["records"][0]["drive_file_id"], "old-file")
            active_ids = {
                item["drive_file_id"]
                for item in load_video_upload_history(config)["records"]
            }
            self.assertEqual(active_ids, {"new-file"})

    def test_replacement_can_find_an_old_version_inside_monthly_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            file_name = "AliceMARKER-0701-1-title.mp4"
            record_video_uploads(
                config,
                [uploaded_record("old-file", file_name, "old-md5")],
                "2026-07-01",
                "01",
                confirmed_report(file_name),
                now=datetime.fromisoformat("2026-07-01T09:00:00+02:00"),
            )
            unrelated_name = "AliceMARKER-0914-2-other.mp4"
            record_video_uploads(
                config,
                [uploaded_record("unrelated", unrelated_name, "other-md5")],
                "2026-09-14",
                "01",
                confirmed_report(unrelated_name),
                now=datetime.fromisoformat("2026-09-14T09:00:00+02:00"),
            )
            drive = FakeDriveService()

            result = record_video_uploads(
                config,
                [uploaded_record("replacement", file_name, "new-md5")],
                "2026-09-15",
                "01",
                confirmed_report(file_name),
                drive_service=drive,
                now=datetime.fromisoformat("2026-09-15T09:00:00+02:00"),
            )

            self.assertEqual(result["trashed"], ["old-file"])

    def test_similar_but_different_video_name_never_trashes_old_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            old_name = "AliceMARKER-0910-7-title.mp4"
            new_name = "AliceMARKER-0910-7-title-fixed.mp4"
            record_video_uploads(
                config,
                [uploaded_record("old-file", old_name, "old-md5")],
                "2026-09-10",
                "02",
                confirmed_report(old_name),
                now=datetime.fromisoformat("2026-09-10T12:00:00+02:00"),
            )
            drive = FakeDriveService()

            result = record_video_uploads(
                config,
                [uploaded_record("new-file", new_name, "new-md5")],
                "2026-09-14",
                "01",
                confirmed_report(new_name),
                drive_service=drive,
                now=datetime.fromisoformat("2026-09-14T09:00:00+02:00"),
            )

            self.assertEqual(result["trashed"], [])
            self.assertEqual(drive.files_api.update_calls, [])

    def test_existing_task_submission_audit_is_migrated_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            audit_path = root / "audit.jsonl"
            config["task_submission_log_file"] = str(audit_path)
            event = {
                "logged_at": "2026-09-13T22:29:14+02:00",
                "run_id": "run-1",
                "status": "write_failed",
                "sheet_name": "Tasks",
                "row": 20,
                "requester": "Alice",
                "file_name": "AliceMARKER-0910-7-title.mp4",
                "video_info": {"task_date": "0910", "task_id": "7", "title": "title"},
                "drive": {
                    "file_id": "old-file",
                    "link": "https://drive.google.com/file/d/old-file/view",
                    "local_file": "C:/result/video.mp4",
                },
                "error": "grid full",
            }
            audit_path.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")

            first = record_video_uploads(
                config,
                [],
                "2026-09-14",
                "01",
                now=datetime.fromisoformat("2026-09-14T09:00:00+02:00"),
            )
            second = record_video_uploads(
                config,
                [],
                "2026-09-14",
                "01",
                now=datetime.fromisoformat("2026-09-14T09:05:00+02:00"),
            )

            self.assertEqual(first["migrated"], 1)
            self.assertEqual(second["migrated"], 0)
            records = load_video_upload_history(config)["records"]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["task_submission"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
