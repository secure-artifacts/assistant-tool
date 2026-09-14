import json
import tempfile
import unittest
from pathlib import Path

from model.TaskSubmissionAudit import (
    correct_self_check_dates,
    latest_upload_records,
    repair_missing_submissions,
    scan_task_submission_sheet,
)
from model.TaskTableSchema import normalize_task_table_schema


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Values:
    def __init__(self, rows):
        self.rows = rows
        self.batch_updates = []

    def get(self, **kwargs):
        if kwargs.get("dateTimeRenderOption") == "FORMATTED_STRING":
            return _Response({
                "values": [
                    [row[5] if len(row) > 5 else ""]
                    for row in self.rows[1:]
                ]
            })
        return _Response({"values": self.rows})

    def batchUpdate(self, **kwargs):
        self.batch_updates.append(kwargs)
        return _Response({"totalUpdatedRows": len(kwargs["body"]["data"])})


class _Spreadsheets:
    def __init__(self, rows):
        self._values = _Values(rows)

    def values(self):
        return self._values

    def get(self, **kwargs):
        if kwargs.get("includeGridData"):
            return _Response({"sheets": [{"data": []}]})
        return _Response({
            "sheets": [{"properties": {"title": "Tasks", "sheetId": 7}}]
        })


class _SheetsService:
    def __init__(self, rows):
        self._spreadsheets = _Spreadsheets(rows)

    def spreadsheets(self):
        return self._spreadsheets


def _schema():
    names = (
        "task_date",
        "requester",
        "chinese",
        "video_type",
        "creator",
        "completed_at",
        "review_status",
        "product_link",
    )
    return normalize_task_table_schema({
        "submission_sheet": {
            "header_row": 1,
            "fields": {name: {"aliases": [name]} for name in names},
        }
    })


def _history(file_id, file_name, task_type, recorded_at, **extra):
    record = {
        "recorded_at": recorded_at,
        "logical_key": file_name.casefold(),
        "file_name": file_name,
        "drive_file_id": file_id,
        "drive_link": f"https://drive.google.com/file/d/{file_id}/view",
        "batch_date": "2026-08-21",
        "mime_type": "video/mp4",
        "task": {
            "date": "0914",
            "id": "7",
            "name": "task",
            "admin": "Alice",
            "row": 9,
            "type": task_type,
            "is_oral": False,
        },
        "replacement": {"state": "current"},
    }
    record.update(extra)
    return record


class TaskSubmissionAuditTests(unittest.TestCase):
    def test_latest_upload_record_uses_newest_current_version(self):
        old = _history("old", "AliceMARKER-0914-7-task.mp4", "reels", "2026-09-13T08:00:00+02:00")
        old["replacement"] = {"state": "replaced"}
        new = _history("new", old["file_name"], "reels", "2026-09-14T08:00:00+02:00")

        result = latest_upload_records([old, new])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["drive_file_id"], "new")

    def test_scan_covers_all_types_and_lists_missing_and_blank_type(self):
        missing = _history(
            "missing",
            "AliceMARKER-0914-7-reel.mp4",
            "reels",
            "2026-09-14T08:00:00+02:00",
        )
        blank = _history(
            "blank",
            "AliceMARKER-0914-8-other.mp4",
            "product demo",
            "2026-09-14T09:00:00+02:00",
        )
        present = _history(
            "present",
            "AliceMARKER-0914-9-present.mp4",
            "reels",
            "2026-09-14T10:00:00+02:00",
        )
        rows = [[
            "task_date", "requester", "chinese", "video_type", "creator",
            "completed_at", "review_status", "product_link",
        ], [
            "", "Alice", "other", "", "me", "", "",
            '=HYPERLINK("https://drive.google.com/file/d/blank/view",'
            '"AliceMARKER-0914-8-other.mp4")',
        ], [
            "", "Alice", "present", "reels", "me", "", "",
            '=HYPERLINK("https://drive.google.com/file/d/present/view",'
            '"AliceMARKER-0914-9-present.mp4")',
        ], [
            "", "Alice", "old manual row", "", "me", "", "",
            '=HYPERLINK("https://drive.google.com/file/d/orphan/view",'
            '"old-manual-video.mp4")',
        ]]

        result = scan_task_submission_sheet(
            {
                "task_submission_sheet_url": "https://docs.google.com/spreadsheets/d/sheet/edit#gid=7",
                "task_submission_creator": "me",
            },
            records=[missing, blank, present],
            sheets_service_factory=lambda _config, _prefix: _SheetsService(rows),
            schema=_schema(),
        )

        self.assertEqual([item["file_name"] for item in result["missing"]], [missing["file_name"]])
        self.assertEqual(
            [item["file_name"] for item in result["blank_type"]],
            [blank["file_name"], "old-manual-video.mp4"],
        )
        self.assertIn("reels", result["type_values"])
        self.assertIn("product demo", result["type_values"])

    def test_repair_writes_only_missing_items_in_current_filter(self):
        missing = _history(
            "missing",
            "AliceMARKER-0914-7-reel.mp4",
            "reels",
            "2026-09-14T08:00:00+02:00",
        )
        blank = _history(
            "blank",
            "AliceMARKER-0914-8-other.mp4",
            "product demo",
            "2026-09-14T09:00:00+02:00",
        )
        captured = []

        def writer(_config, records, result_report=None, drive_service=None):
            captured.extend(records)
            result_report.update({
                "attempted": True,
                "successful_files": [item["name"] for item in records],
                "failed_files": [],
            })
            self.assertIsNone(drive_service)
            return len(records)

        result = repair_missing_submissions(
            {},
            [
                {"status": "未填写", "file_name": missing["file_name"], "history_record": missing},
                {"status": "视频类型为空", "file_name": blank["file_name"], "history_record": blank},
            ],
            writer=writer,
        )

        self.assertEqual(result["write_count"], 1)
        self.assertEqual([item["name"] for item in captured], [missing["file_name"]])
        self.assertEqual(captured[0]["local_task_type"], "reels")
        self.assertEqual(
            captured[0]["task_submission_completed_at"],
            "2026-08-21",
        )

    def test_detects_and_repairs_only_dates_written_by_old_self_check(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            file_name = "AliceMARKER-0914-7-reel.mp4"
            event = {
                "logged_at": "2026-09-14T12:00:00+02:00",
                "status": "success",
                "row": 2,
                "file_name": file_name,
                "drive": {"action": "history_self_check_repair"},
                "planned_after": {"completed_at": "2026-09-14"},
            }
            audit_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            record = _history(
                "video",
                file_name,
                "reels",
                "2026-09-14T08:00:00+02:00",
            )
            record["batch_date"] = "2026-08-21"
            rows = [[
                "task_date", "requester", "chinese", "video_type", "creator",
                "completed_at", "review_status", "product_link",
            ], [
                "", "Alice", "reel", "reels", "me", "2026-09-14", "",
                '=HYPERLINK("https://drive.google.com/file/d/video/view",'
                f'"{file_name}")',
            ]]
            service = _SheetsService(rows)
            config = {
                "task_submission_sheet_url": (
                    "https://docs.google.com/spreadsheets/d/sheet/edit#gid=7"
                ),
                "task_submission_creator": "me",
                "task_submission_log_file": str(audit_path),
            }

            scan = scan_task_submission_sheet(
                config,
                records=[record],
                sheets_service_factory=lambda _config, _prefix: service,
                schema=_schema(),
            )

            self.assertEqual(len(scan["wrong_date"]), 1)
            self.assertEqual(
                scan["wrong_date"][0]["expected_completed_at"],
                "2026-08-21",
            )
            repaired = correct_self_check_dates(
                config,
                scan["wrong_date"],
                sheets_service_factory=lambda _config, _prefix: service,
                schema=_schema(),
            )
            self.assertEqual(repaired["updated_count"], 1)
            update = service.spreadsheets().values().batch_updates[0]
            self.assertEqual(update["body"]["data"], [{
                "range": "'Tasks'!F2",
                "values": [["2026-08-21"]],
            }])


if __name__ == "__main__":
    unittest.main()
