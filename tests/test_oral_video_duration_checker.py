import unittest

from model.OralVideoDurationChecker import (
    REPLACEMENT_VIDEO_TYPE,
    SOURCE_VIDEO_TYPE,
    check_oral_video_durations,
    format_duration,
)
from model.TaskTableSchema import normalize_task_table_schema


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _SheetsValues:
    def __init__(self, rows):
        self.rows = rows
        self.batch_updates = []

    def get(self, **_kwargs):
        return _Response({"values": self.rows})

    def batchUpdate(self, **kwargs):
        self.batch_updates.append(kwargs)
        return _Response({"totalUpdatedRows": len(kwargs["body"]["data"])})


class _Spreadsheets:
    def __init__(self, values, grid_response=None):
        self._values = values
        self._grid_response = grid_response or {"sheets": [{"data": []}]}

    def get(self, **kwargs):
        if kwargs.get("includeGridData"):
            return _Response(self._grid_response)
        return _Response({
            "sheets": [{"properties": {"title": "Tasks", "sheetId": 7}}]
        })

    def values(self):
        return self._values


class _SheetsService:
    def __init__(self, rows, grid_response=None):
        self.values_api = _SheetsValues(rows)
        self.spreadsheets_api = _Spreadsheets(self.values_api, grid_response)

    def spreadsheets(self):
        return self.spreadsheets_api


class _DriveFiles:
    def __init__(self, metadata_by_id):
        self.metadata_by_id = metadata_by_id
        self.requests = []

    def get(self, **kwargs):
        self.requests.append(kwargs)
        return _Response(self.metadata_by_id[kwargs["fileId"]])


class _DriveService:
    def __init__(self, metadata_by_id):
        self.files_api = _DriveFiles(metadata_by_id)

    def files(self):
        return self.files_api


def _schema():
    fields = {
        name: {"aliases": [name]}
        for name in (
            "requester",
            "chinese",
            "video_type",
            "creator",
            "completed_at",
            "review_status",
            "product_link",
        )
    }
    return normalize_task_table_schema({
        "submission_sheet": {
            "header_row": 1,
            "fields": fields,
        }
    })


class OralVideoDurationCheckerTests(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(60_000), "01:00")
        self.assertEqual(format_duration(61_250), "01:01.250")
        self.assertEqual(format_duration(3_661_000), "01:01:01")

    def test_updates_only_matching_creator_and_over_one_minute(self):
        headers = [
            "requester",
            "chinese",
            "video_type",
            "creator",
            "completed_at",
            "review_status",
            "product_link",
        ]
        long_link = "https://drive.google.com/file/d/long-video/view"
        short_link = "https://drive.google.com/file/d/short-video/view"
        rows = [
            headers,
            ["", "", SOURCE_VIDEO_TYPE, "Me", "", "", long_link],
            ["a", "short", SOURCE_VIDEO_TYPE, "Me", "", "", short_link],
            ["a", "other creator", SOURCE_VIDEO_TYPE, "Someone", "", "", long_link],
            ["a", "other type", "another type", "Me", "", "", long_link],
            ["a", "no link", SOURCE_VIDEO_TYPE, "Me", "", "", ""],
            ["a", "bad link", SOURCE_VIDEO_TYPE, "Me", "", "", "https://example.com/a"],
        ]
        sheets = _SheetsService(rows)
        drive = _DriveService({
            "long-video": {
                "id": "long-video",
                "name": "long.mp4",
                "mimeType": "video/mp4",
                "videoMediaMetadata": {"durationMillis": "60001"},
            },
            "short-video": {
                "id": "short-video",
                "name": "short.mp4",
                "mimeType": "video/mp4",
                "videoMediaMetadata": {"durationMillis": "60000"},
            },
        })
        messages = []

        result = check_oral_video_durations(
            {
                "task_submission_sheet_url": (
                    "https://docs.google.com/spreadsheets/d/sheet123/edit#gid=7"
                ),
                "task_submission_creator": "me",
            },
            messages.append,
            sheets_service_factory=lambda _config, _prefix: sheets,
            drive_service_factory=lambda: drive,
            schema=_schema(),
        )

        self.assertEqual(result["candidate_count"], 4)
        self.assertEqual(result["checked_count"], 2)
        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(result["updated_rows"][0]["row"], 2)
        self.assertEqual(result["updated_rows"][0]["new_video_type"], REPLACEMENT_VIDEO_TYPE)
        self.assertEqual(len(result["skipped_rows"]), 2)
        self.assertEqual(len(sheets.values_api.batch_updates), 1)
        update = sheets.values_api.batch_updates[0]
        self.assertEqual(update["body"]["valueInputOption"], "RAW")
        self.assertEqual(update["body"]["data"], [{
            "range": "'Tasks'!C2",
            "values": [[REPLACEMENT_VIDEO_TYPE]],
        }])
        self.assertEqual(
            [request["fileId"] for request in drive.files_api.requests],
            ["long-video", "short-video"],
        )
        self.assertIn("videoMediaMetadata", drive.files_api.requests[0]["fields"])
        self.assertTrue(any("已修改 1 行" in message for message in messages))

    def test_missing_video_type_column_stops_before_drive_access(self):
        schema = _schema()
        schema["submission_sheet"]["fields"]["video_type"]["aliases"] = []
        rows = [[
            "requester", "chinese", "creator", "completed_at",
            "review_status", "product_link",
        ]]
        sheets = _SheetsService(rows)

        with self.assertRaisesRegex(RuntimeError, "视频类型"):
            check_oral_video_durations(
                {
                    "task_submission_sheet_url": "sheet123",
                    "task_submission_creator": "me",
                },
                sheets_service_factory=lambda _config, _prefix: sheets,
                drive_service_factory=lambda: self.fail("不应加载 Drive 服务"),
                schema=schema,
            )

    def test_uses_url_behind_linked_video_name(self):
        rows = [
            [
                "requester", "chinese", "video_type", "creator",
                "completed_at", "review_status", "product_link",
            ],
            ["a", "task", SOURCE_VIDEO_TYPE, "me", "", "", "video-name.mp4"],
        ]
        rich_link = "https://drive.google.com/file/d/rich-video/view"
        grid_response = {
            "sheets": [{
                "data": [{
                    "startRow": 1,
                    "startColumn": 6,
                    "rowData": [{
                        "values": [{
                            "formattedValue": "video-name.mp4",
                            "textFormatRuns": [{
                                "format": {"link": {"uri": rich_link}}
                            }],
                        }]
                    }],
                }]
            }]
        }
        sheets = _SheetsService(rows, grid_response)
        drive = _DriveService({
            "rich-video": {
                "id": "rich-video",
                "name": "video-name.mp4",
                "mimeType": "video/mp4",
                "videoMediaMetadata": {"durationMillis": "90000"},
            }
        })

        result = check_oral_video_durations(
            {
                "task_submission_sheet_url": (
                    "https://docs.google.com/spreadsheets/d/sheet123/edit#gid=7"
                ),
                "task_submission_creator": "me",
            },
            sheets_service_factory=lambda _config, _prefix: sheets,
            drive_service_factory=lambda: drive,
            schema=_schema(),
        )

        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(result["updated_rows"][0]["link"], rich_link)
        self.assertEqual(drive.files_api.requests[0]["fileId"], "rich-video")


if __name__ == "__main__":
    unittest.main()
