import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odf.opendocument import OpenDocumentSpreadsheet
from odf.table import Table as OdfTable, TableCell, TableRow
from odf.text import A, P

from model.GoogleSheetMonitor import (
    GoogleSheetMonitorThread,
    extract_links_from_grid_response,
    link_key,
    normalize_monitor_settings,
    read_monitor_state,
)
from model.GoogleDriveHelper import (
    collect_person_folder_links,
    get_or_create_remote_folder_path_with_root,
)
from model.InventoryManager import (
    InventoryStore,
    STATUS_CRITICAL,
    STATUS_INITIAL,
    STATUS_MODERATE,
    STATUS_NORMAL,
    current_quantity,
    item_status,
)
from model.OdsHelper import (
    ReadTaskOds2,
    normalize_review_required,
    normalize_subcategory_path,
)
from model.TaskTableSchema import normalize_task_table_schema
from model.TaskSubmissionHelper import (
    build_task_sheet_updates,
    column_letter,
    read_task_sheet_rows,
    record_needs_review,
    resolve_task_submission_layout,
)
from model.TaskReferenceDownloader import (
    TaskReferenceDownloadCache,
    TaskReferenceJob,
    partition_cached_reference_jobs,
)
from model.TaskResultExporter import export_task
from model.TaskResultOrganizer import (
    attach_local_task_metadata,
    build_routed_batches,
    clear_pending_changed_file_batches,
    compress_routed_batches,
    file_identity,
    is_review_upload_record,
    load_pending_changed_file_batches,
    merge_changed_file_batches,
    resolve_ask_detection_mode,
    run_task_result_organizer,
    save_pending_changed_file_batches,
    updated_files_from_batches,
)
from model.VideoElementDetector import (
    MANUAL_ACTION_SKIP_UPLOAD,
    detect_video_element,
    get_report_name,
    get_target_name,
    manual_review_video,
    read_cached_detection,
    save_detection_report,
    set_runtime_config,
    tighten_detection_result,
)


TEST_TASK_TABLE_SCHEMA = normalize_task_table_schema(
    {
        "labels": {
            "task_id": "Record ID",
            "admin": "Owner",
            "creator": "Operator",
            "task_name": "Title",
            "task_type": "Category",
            "task_date": "Date",
            "task_reference_link": "Reference",
            "task_audio_text": "Content",
            "task_audio_type": "Voice profile",
            "review_required": "Needs review",
            "subcategory": "Subcategory",
        },
        "fields": {
            "task_id": {"aliases": ["record_id", "legacy_id"], "default": "{row}"},
            "admin": {"aliases": ["owner", "requester", "legacy_owner"]},
            "creator": {"aliases": ["operator", "legacy_operator"]},
            "task_name": {"aliases": ["title", "legacy_title"], "default": "{task_id}"},
            "task_type": {"aliases": ["category", "legacy_category"], "default": "short-video"},
            "task_date": {"aliases": ["date", "legacy_date"]},
            "task_reference_link": {"aliases": ["reference", "legacy_reference"]},
            "task_audio_text": {"aliases": ["content", "source_text", "legacy_content"]},
            "task_audio_type": {"aliases": ["voice_profile", "legacy_voice"]},
            "review_required": {"aliases": ["needs_review"]},
            "subcategory": {"aliases": ["subcategory"]},
        },
        "submission_sheet": {
            "labels": {
                "requester": "Requester",
                "chinese": "Source text",
                "video_type": "Video type",
                "creator": "Operator",
                "completed_at": "Completed at",
                "review_status": "Status",
                "product_link": "Result URL",
            },
            "fields": {
                "requester": {"aliases": ["requester", "legacy_requester"]},
                "chinese": {"aliases": ["source_text", "legacy_source"]},
                "video_type": {"aliases": ["category", "video_type"]},
                "creator": {"aliases": ["operator", "legacy_operator"]},
                "completed_at": {"aliases": ["completed_at", "legacy_completed"]},
                "review_status": {"aliases": ["status", "legacy_status"]},
                "product_link": {"aliases": ["result_url", "legacy_result"]},
            },
        },
    }
)


class InventoryTests(unittest.TestCase):
    def test_elapsed_time_and_thresholds(self):
        now = time.time()
        item = {
            "quantity": 10,
            "daily_usage": 4,
            "updated_at": now - 12 * 60 * 60,
        }
        self.assertAlmostEqual(current_quantity(item, now), 8.0, places=3)
        self.assertEqual(item_status(item, now), STATUS_NORMAL)
        item["quantity"] = 7
        self.assertEqual(item_status(item, now), STATUS_INITIAL)
        item["quantity"] = 5
        self.assertEqual(item_status(item, now), STATUS_MODERATE)
        item["quantity"] = 2
        self.assertEqual(item_status(item, now), STATUS_CRITICAL)

    def test_store_add_replenish_edit_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InventoryStore(Path(directory) / "inventory.json")
            created = store.add_item("测试库存", 10, 2)
            store.add_stock(created["id"], 5)
            item = store.summary()["items"][0]
            self.assertAlmostEqual(item["current_quantity"], 15, places=2)
            store.update_item(created["id"], "改名库存", 8, 4)
            self.assertEqual(store.list_items()[0]["name"], "改名库存")
            store.delete_item(created["id"])
            self.assertEqual(store.list_items(), [])


class SheetMonitorTests(unittest.TestCase):
    def test_extract_plain_formula_and_rich_text_links(self):
        response = {
            "sheets": [
                {
                    "data": [
                        {
                            "rowData": [
                                {
                                    "values": [
                                        {"formattedValue": "https://drive.google.com/file/d/abc123/view"},
                                        {
                                            "userEnteredValue": {
                                                "formulaValue": '=HYPERLINK("https://docs.google.com/document/d/doc456/edit","文件")'
                                            }
                                        },
                                        {
                                            "formattedValue": "点这里",
                                            "textFormatRuns": [
                                                {
                                                    "format": {
                                                        "link": {
                                                            "uri": "https://docs.google.com/spreadsheets/d/sheet789/edit"
                                                        }
                                                    }
                                                }
                                            ],
                                        },
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        links = extract_links_from_grid_response(response)
        self.assertEqual(len(links), 3)
        self.assertEqual(link_key(links[0]), "file:abc123")
        self.assertEqual(link_key(links[1]), "document:doc456")
        self.assertEqual(link_key(links[2]), "spreadsheets:sheet789")

    def test_settings_are_bounded(self):
        settings = normalize_monitor_settings(
            {"enabled": True, "poll_seconds": 1, "download_folder": ""}
        )
        self.assertEqual(settings["poll_seconds"], 15)
        self.assertEqual(settings["download_folder"], "谷歌表格下载")

    def test_first_check_is_baseline_and_later_link_is_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "monitor.json"
            thread = GoogleSheetMonitorThread(
                {"sheet_url": "https://docs.google.com/spreadsheets/d/table123/edit#gid=0"},
                state_path=state_path,
            )
            state = read_monitor_state(state_path)
            old_link = "https://drive.google.com/file/d/old123/view"
            new_link = "https://drive.google.com/file/d/new456/view"
            thread._detect(state, [old_link])
            self.assertEqual(read_monitor_state(state_path)["pending"], [])
            thread._detect(state, [old_link, new_link])
            saved = read_monitor_state(state_path)
            self.assertEqual([item["url"] for item in saved["pending"]], [new_link])

    def test_removed_link_is_new_when_pasted_back(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "monitor.json"
            thread = GoogleSheetMonitorThread(
                {"sheet_url": "https://docs.google.com/spreadsheets/d/table123/edit#gid=0"},
                state_path=state_path,
            )
            link = "https://drive.google.com/file/d/reused123/view"
            state = read_monitor_state(state_path)

            thread._detect(state, [link])
            thread._detect(state, [])
            removed = read_monitor_state(state_path)
            self.assertEqual(removed["seen_keys"], [])
            self.assertEqual(removed["pending"], [])

            thread._detect(state, [link])
            restored = read_monitor_state(state_path)
            self.assertEqual(
                [item["url"] for item in restored["pending"]],
                [link],
            )

    def test_replacing_link_in_same_cell_queues_only_the_new_link(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "monitor.json"
            thread = GoogleSheetMonitorThread(
                {"sheet_url": "https://docs.google.com/spreadsheets/d/table123/edit#gid=0"},
                state_path=state_path,
            )
            old_link = "https://drive.google.com/file/d/old123/view"
            new_link = "https://drive.google.com/file/d/new456/view"
            state = read_monitor_state(state_path)

            thread._detect(state, [old_link])
            thread._detect(state, [new_link])

            saved = read_monitor_state(state_path)
            self.assertEqual(saved["seen_keys"], [link_key(new_link)])
            self.assertEqual([item["url"] for item in saved["pending"]], [new_link])

    def test_repeated_reappearance_does_not_duplicate_pending_item(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "monitor.json"
            thread = GoogleSheetMonitorThread(
                {"sheet_url": "https://docs.google.com/spreadsheets/d/table123/edit#gid=0"},
                state_path=state_path,
            )
            link = "https://drive.google.com/file/d/reused123/view"
            state = read_monitor_state(state_path)

            thread._detect(state, [])
            thread._detect(state, [link])
            thread._detect(state, [])
            thread._detect(state, [link])

            saved = read_monitor_state(state_path)
            self.assertEqual(len(saved["pending"]), 1)

    def test_old_monitor_state_is_migrated_without_losing_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "monitor.json"
            old_key = "file:old123"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "source": "source",
                        "initialized": True,
                        "seen_keys": [old_key],
                        "pending": [],
                        "history": [],
                    }
                ),
                encoding="utf-8",
            )

            migrated = read_monitor_state(state_path)

            self.assertEqual(migrated["version"], 2)
            self.assertEqual(migrated["seen_keys"], [old_key])


class TaskReferenceCacheTests(unittest.TestCase):
    def test_cached_file_skips_network_queue_and_missing_file_requeues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "reels" / "001"
            output_dir.mkdir(parents=True)
            target = output_dir / "reference.mp4"
            target.write_bytes(b"video")
            cache_path = root / "cache.json"
            job = TaskReferenceJob(
                "001",
                "https://drive.google.com/file/d/file123/view",
                output_dir,
            )

            cache = TaskReferenceDownloadCache(cache_path)
            cache.remember(job, target)
            pending, cached, warnings = partition_cached_reference_jobs(
                [job], cache_path
            )
            self.assertEqual(pending, [])
            self.assertEqual(cached[0][1], target.resolve())
            self.assertEqual(warnings, [])

            target.unlink()
            pending, cached, warnings = partition_cached_reference_jobs(
                [job], cache_path
            )
            self.assertEqual(pending, [job])
            self.assertEqual(cached, [])
            self.assertEqual(warnings, [])

    def test_same_drive_file_with_different_query_uses_same_cache_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "task"
            output_dir.mkdir()
            target = output_dir / "doc.docx"
            target.write_bytes(b"doc")
            cache_path = root / "cache.json"
            original = TaskReferenceJob(
                "1",
                "https://docs.google.com/document/d/doc123/edit?usp=sharing",
                output_dir,
            )
            equivalent = TaskReferenceJob(
                "1",
                "https://docs.google.com/document/d/doc123/view",
                output_dir,
            )
            TaskReferenceDownloadCache(cache_path).remember(original, target)
            pending, cached, _warnings = partition_cached_reference_jobs(
                [equivalent], cache_path
            )
            self.assertEqual(pending, [])
            self.assertEqual(len(cached), 1)


class TaskTableSchemaTests(unittest.TestCase):
    @staticmethod
    def _add_row(table, values, link_column=None):
        row = TableRow()
        for index, value in enumerate(values):
            cell = TableCell()
            paragraph = P()
            if link_column == index and value:
                paragraph.addElement(A(href=value, text="查看参考"))
            elif value:
                paragraph.addText(str(value))
            cell.addElement(paragraph)
            row.addElement(cell)
        table.addElement(row)

    def test_new_table_headers_map_and_missing_id_uses_row_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new-table.ods"
            document = OpenDocumentSpreadsheet()
            table = OdfTable(name="sample")
            document.spreadsheet.addElement(table)
            headers = [
                "date", "requester", "title", "source_text", "content",
                "reference", "approach", "priority", "operator", "category",
                "completed_at", "progress", "result_url", "status", "issue", "reviewer",
                "needs_review",
                "subcategory",
            ]
            self._add_row(table, headers)
            self._add_row(
                table,
                [
                    "08/23", "request-a", "page-a", "source draft", "translated text",
                    "https://drive.google.com/file/d/ref123/view", "approach", "high",
                    "operator-b", "short-video", "", "new", "", "", "", "",
                    "yes",
                    "类别A/大",
                ],
                link_column=5,
            )
            self._add_row(
                table,
                [
                    "08/23", "request-c", "page-b", "fallback draft", "", "", "", "",
                    "operator-d", "short-video", "", "", "", "", "", "",
                    "no",
                    "",
                ],
            )
            document.save(str(path))

            tasks, report = ReadTaskOds2(
                path,
                schema=TEST_TASK_TABLE_SCHEMA,
                return_report=True,
            )
            self.assertEqual(len(tasks), 2)
            self.assertEqual(tasks[0].task_id, "2")
            self.assertEqual(tasks[0].admin, "request-a")
            self.assertEqual(tasks[0].creator, "operator-b")
            self.assertEqual(tasks[0].task_name, "page-a")
            self.assertEqual(tasks[0].task_audio_text, "translated text")
            self.assertTrue(tasks[0].review_required)
            self.assertTrue(tasks[0].task_type_from_table)
            self.assertEqual(tasks[0].subcategory, "类别A/大")
            self.assertIn("https://drive.google.com/file/d/ref123/view", tasks[0].task_reference_link)
            self.assertEqual(tasks[1].task_id, "3")
            self.assertEqual(tasks[1].task_audio_text, "fallback draft")
            self.assertFalse(tasks[1].review_required)
            self.assertEqual(report["header_row"], 1)
            self.assertEqual(report["matched_headers"]["task_name"], ["title"])

    def test_old_table_headers_remain_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old-table.ods"
            document = OpenDocumentSpreadsheet()
            table = OdfTable(name="legacy-sample")
            document.spreadsheet.addElement(table)
            self._add_row(
                table,
                [
                    "legacy_owner", "legacy_operator", "legacy_title", "legacy_id",
                    "legacy_category", "legacy_date", "legacy_reference", "unused",
                    "legacy_content", "legacy_voice",
                ],
            )
            self._add_row(
                table,
                ["owner-a", "operator-b", "legacy task", "100", "short-video", "0823", "", "", "audio", "voice-a"],
            )
            document.save(str(path))
            tasks = ReadTaskOds2(path, schema=TEST_TASK_TABLE_SCHEMA)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].task_id, "100")
            self.assertEqual(tasks[0].task_name, "legacy task")
            self.assertEqual(tasks[0].task_audio_type, "voice-a")
            self.assertIsNone(tasks[0].review_required)
            self.assertEqual(tasks[0].subcategory, "")

    def test_default_task_type_is_not_marked_as_local_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "default-type.ods"
            document = OpenDocumentSpreadsheet()
            table = OdfTable(name="sample")
            document.spreadsheet.addElement(table)
            self._add_row(table, ["owner", "title", "record_id"])
            self._add_row(table, ["Alice", "task", "10"])
            document.save(str(path))

            tasks = ReadTaskOds2(path, schema=TEST_TASK_TABLE_SCHEMA)

            self.assertEqual(tasks[0].task_type, "short-video")
            self.assertFalse(tasks[0].task_type_from_table)

    def test_google_submission_headers_use_new_positions(self):
        headers = [
            "date", "requester", "title", "source_text", "content",
            "reference", "approach", "priority", "operator", "category",
            "completed_at", "progress", "result_url", "status",
        ]
        header_row, columns = resolve_task_submission_layout(
            [headers], TEST_TASK_TABLE_SCHEMA
        )
        self.assertEqual(header_row, 1)
        self.assertEqual(
            columns,
            {
                "requester": 2,
                "chinese": 4,
                "creator": 9,
                "video_type": 10,
                "completed_at": 11,
                "review_status": 14,
                "product_link": 13,
            },
        )
        self.assertEqual(column_letter(columns["product_link"]), "M")

    def test_google_submission_reader_uses_unbounded_sheet_request(self):
        headers = [
            "date", "requester", "title", "source_text", "content",
            "reference", "approach", "priority", "operator", "category",
            "completed_at", "progress", "result_url", "status",
        ]
        data_row = [
            "08/24", "Alice", "page", "hello world example", "translated",
            "", "", "", "", "short-video", "", "new", "", "",
        ]
        request = MagicMock()
        request.execute.return_value = {"values": [headers, data_row]}
        values_api = MagicMock()
        values_api.get.return_value = request
        service = MagicMock()
        service.spreadsheets.return_value.values.return_value = values_api

        header_row, rows, columns = read_task_sheet_rows(
            service,
            "spreadsheet-id",
            "01 video",
            schema=TEST_TASK_TABLE_SCHEMA,
        )

        self.assertEqual(header_row, 1)
        self.assertEqual(columns["requester"], 2)
        self.assertEqual(columns["video_type"], 10)
        self.assertEqual(columns["product_link"], 13)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["requester"], "Alice")
        values_api.get.assert_called_once()
        self.assertEqual(values_api.get.call_args.kwargs["range"], "'01 video'")
        values_api.batchGet.assert_not_called()

    def test_google_submission_old_positions_remain_compatible(self):
        headers = [""] * 21
        for index, value in {
            3: "legacy_requester",
            5: "legacy_source",
            17: "legacy_operator",
            19: "legacy_completed",
            20: "legacy_status",
            21: "legacy_result",
        }.items():
            headers[index - 1] = value
        _header_row, columns = resolve_task_submission_layout(
            [headers], TEST_TASK_TABLE_SCHEMA
        )
        self.assertEqual(columns["requester"], 3)
        self.assertEqual(columns["creator"], 17)
        self.assertEqual(columns["product_link"], 21)

    def test_google_submission_updates_use_detected_columns(self):
        column_map = {
            "requester": 2,
            "chinese": 4,
            "creator": 9,
            "video_type": 10,
            "completed_at": 11,
            "review_status": 14,
            "product_link": 13,
        }
        rows = [
            {
                "row": 2,
                "requester": "Alice",
                "chinese": "hello world example",
                "creator": "",
                "video_type": "",
                "completed_at": "",
                "review_status": "",
                "product_link": "",
                "requester_key": "alice",
                "task_text_key": "helloworldexample",
                "task_filename_key": "helloworldexample",
            }
        ]
        records = [
            {
                "name": "AliceMARKER-0823-2-hello world example.mp4",
                "webViewLink": "https://drive.google.com/file/d/product123/view",
                "relative_path": "manual-review/video.mp4",
                "local_task_type": "short-video",
            }
        ]
        updates, matched = build_task_sheet_updates(
            {
                "task_submission_creator": "operator",
                "task_submission_creator_marker": "MARKER",
                "review_folder_name": "manual-review",
            },
            records,
            rows,
            "任务",
            column_map,
        )
        self.assertEqual(len(matched), 1)
        ranges = [item["range"] for item in updates]
        self.assertTrue(any(value.endswith("!I2") for value in ranges))
        self.assertTrue(any(value.endswith("!J2") for value in ranges))
        self.assertTrue(any(value.endswith("!K2") for value in ranges))
        self.assertTrue(any(value.endswith("!N2") for value in ranges))
        self.assertTrue(any(value.endswith("!M2") for value in ranges))

    def test_blank_local_task_type_leaves_google_video_type_unchanged(self):
        column_map = {
            "requester": 2,
            "chinese": 4,
            "creator": 9,
            "video_type": 10,
            "completed_at": 11,
            "review_status": 14,
            "product_link": 13,
        }
        rows = [{
            "row": 2,
            "requester": "Alice",
            "chinese": "hello world example",
            "creator": "",
            "video_type": "existing-google-value",
            "completed_at": "",
            "review_status": "",
            "product_link": "",
            "requester_key": "alice",
            "task_text_key": "helloworldexample",
            "task_filename_key": "helloworldexample",
        }]
        records = [{
            "name": "AliceMARKER-0823-2-hello world example.mp4",
            "webViewLink": "https://drive.google.com/file/d/product123/view",
            "relative_path": "Alice/video.mp4",
        }]

        updates, matched = build_task_sheet_updates(
            {
                "task_submission_creator": "operator",
                "task_submission_creator_marker": "MARKER",
                "review_folder_name": "manual-review",
            },
            records,
            rows,
            "任务",
            column_map,
        )

        ranges = [item["range"] for item in updates]
        self.assertFalse(any(value.endswith("!J2") for value in ranges))
        self.assertEqual(
            matched[0]["planned_after"]["video_type"],
            "existing-google-value",
        )


class TaskResultDetectionChoiceTests(unittest.TestCase):
    def test_subcategory_path_normalization(self):
        self.assertEqual(
            normalize_subcategory_path(r" 类别A\\大/../特殊:类别 "),
            "类别A/大/特殊_类别",
        )

    def test_review_value_normalization(self):
        for value in ("是", "需要审核", "YES", "1", True):
            self.assertIs(normalize_review_required(value), True)
        for value in ("否", "无需审核", "NO", "0", False):
            self.assertIs(normalize_review_required(value), False)
        self.assertIsNone(normalize_review_required(""))
        self.assertIsNone(normalize_review_required("稍后决定"))

    def test_video_detection_rules_come_from_runtime_config(self):
        set_runtime_config({
            "video_detection_target_name": "Widget",
            "video_detection_positive_keywords": ["Widget"],
            "video_detection_negative_keywords": ["Decoration"],
            "video_detection_report_name": "../WidgetReport.json",
        })
        try:
            result = tighten_detection_result({
                "found": True,
                "confidence": 0.9,
                "summary": "Only a Decoration is visible",
                "matched_frames": [],
            })
            self.assertEqual(get_target_name(), "Widget")
            self.assertEqual(get_report_name(), "WidgetReport.json")
            self.assertFalse(result["found"])
            self.assertLessEqual(result["confidence"], 0.3)
        finally:
            set_runtime_config({})

    def test_updated_files_are_deduplicated_in_export_order(self):
        first = Path("result/first.mp4")
        second = Path("result/notes.txt")
        batches = [
            (Path("result"), [first, second]),
            (Path("result"), [first]),
        ]
        self.assertEqual(updated_files_from_batches(batches), [first, second])

    def test_ask_mode_receives_actual_updated_files_and_uses_choice(self):
        config = {
            "video_detection_mode": "ask",
            "enable_video_review_detection": True,
        }
        received = []
        batches = [
            (Path("result"), [Path("result/video.mp4"), Path("result/readme.txt")])
        ]

        selected = resolve_ask_detection_mode(
            config,
            batches,
            resolver=lambda files: received.extend(files) or "manual",
        )

        self.assertEqual(selected, "manual")
        self.assertEqual(config["video_detection_mode"], "manual")
        self.assertEqual(received, [Path("result/video.mp4"), Path("result/readme.txt")])

    def test_closing_detection_choice_cancels_without_changing_mode(self):
        config = {
            "video_detection_mode": "ask",
            "enable_video_review_detection": True,
        }
        video = Path("result/video.mp4")

        selected = resolve_ask_detection_mode(
            config,
            [(Path("result"), [video])],
            resolver=lambda _files: "cancel",
        )

        self.assertEqual(selected, "cancel")
        self.assertEqual(config["video_detection_mode"], "ask")

    def test_cancelled_run_restores_pending_files_on_next_click(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            output_dir = base_dir / "0828" / "result"
            output_dir.mkdir(parents=True)
            video = output_dir / "pending.mp4"
            video.write_bytes(b"video")
            state_path = base_dir / "pending-state.json"
            config = {
                "run_export": True,
                "run_upload": True,
                "upload_only_changed_files": True,
                "enable_video_review_detection": True,
                "video_detection_mode": "ask",
                "task_result_pending_file": str(state_path),
            }
            export_results = [
                SimpleNamespace(output_dir=output_dir, updated_files=[video]),
                SimpleNamespace(output_dir=output_dir, updated_files=[]),
            ]
            received_first = []
            received_second = []

            with patch(
                "model.TaskResultOrganizer.export_one_date",
                side_effect=export_results,
            ), patch("model.TaskResultOrganizer.load_drive_service") as drive_service:
                first = run_task_result_organizer(
                    [date(2026, 8, 28)],
                    base_dir,
                    config,
                    detection_mode_resolver=(
                        lambda files: received_first.extend(files) or "cancel"
                    ),
                )
                second = run_task_result_organizer(
                    [date(2026, 8, 28)],
                    base_dir,
                    config,
                    detection_mode_resolver=(
                        lambda files: received_second.extend(files) or "cancel"
                    ),
                )

            self.assertTrue(first["cancelled"])
            self.assertTrue(second["cancelled"])
            self.assertEqual(received_first, [video])
            self.assertEqual(received_second, [video])
            self.assertTrue(state_path.is_file())
            drive_service.assert_not_called()

    def test_pending_file_state_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            root_dir = base_dir / "0828" / "result"
            root_dir.mkdir(parents=True)
            first = root_dir / "first.mp4"
            second = root_dir / "second.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            config = {
                "task_result_pending_file": str(base_dir / "pending.json"),
            }
            task_dates = [date(2026, 8, 28)]

            saved = save_pending_changed_file_batches(
                config,
                base_dir,
                task_dates,
                [(root_dir, [first, first, second])],
            )
            loaded = load_pending_changed_file_batches(
                config,
                base_dir,
                task_dates,
            )

            self.assertEqual(saved, 2)
            self.assertEqual(loaded, [(root_dir, [first, second])])
            self.assertEqual(
                merge_changed_file_batches(loaded, [(root_dir, [second])]),
                loaded,
            )
            clear_pending_changed_file_batches(config, base_dir, task_dates)
            self.assertFalse(Path(config["task_result_pending_file"]).exists())

    def test_export_callback_keeps_task_metadata_for_unchanged_pending_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root_dir = Path(directory)
            task_dir = root_dir / "task" / "1"
            task_dir.mkdir(parents=True)
            source = task_dir / "final.mp4"
            source.write_bytes(b"video")
            output_dir = root_dir / "result"
            output_wsp = root_dir / "result-wsp"
            task = SimpleNamespace(
                task_id="1",
                admin="Alice",
                creator="Operator",
                task_date="0828",
                task_name="Title",
                task_type="video",
            )
            profile = {
                "mode": "single",
                "task_dir": "task",
                "candidate_names": ["final.mp4"],
                "output_name_template": "{task_id}.mp4",
            }
            config = {"task_output_name_template": "{task_id}.mp4"}

            export_task(
                task,
                date(2026, 8, 28),
                root_dir,
                output_dir,
                output_wsp,
                profile,
                config,
                False,
            )
            remembered = []
            updated = export_task(
                task,
                date(2026, 8, 28),
                root_dir,
                output_dir,
                output_wsp,
                profile,
                config,
                False,
                exported_file_callback=lambda path, value: remembered.append((path, value)),
            )

            self.assertEqual(updated, [])
            self.assertEqual(remembered, [(output_dir / "1.mp4", task)])

    def test_ask_mode_does_not_prompt_without_detectable_video(self):
        config = {
            "video_detection_mode": "ask",
            "enable_video_review_detection": True,
        }

        selected = resolve_ask_detection_mode(
            config,
            [(Path("result"), [Path("result/readme.txt")])],
            resolver=lambda _files: self.fail("resolver should not be called"),
        )

        self.assertEqual(selected, "ask")
        self.assertEqual(config["video_detection_mode"], "ask")

    def test_all_local_no_overrides_skip_global_review_choice(self):
        video = Path("result/video.mp4")
        task_by_file = {
            file_identity(video): SimpleNamespace(review_required=False),
        }
        config = {
            "video_detection_mode": "ask",
            "enable_video_review_detection": True,
        }

        selected = resolve_ask_detection_mode(
            config,
            [(Path("result"), [video])],
            resolver=lambda _files: self.fail("resolver should not be called"),
            task_by_file=task_by_file,
        )

        self.assertEqual(selected, "ask")

    def test_local_yes_turns_global_skip_into_manual_review(self):
        root = Path("result")
        video = root / "AliceMARKER-0825-2-title.mp4"
        task_by_file = {
            file_identity(video): SimpleNamespace(review_required=True),
        }
        config = {
            "video_detection_mode": "skip",
            "enable_video_review_detection": True,
            "video_filename_creator_marker": "MARKER",
            "review_folder_name": "manual-review",
        }

        with patch(
            "model.TaskResultOrganizer.detect_video_element",
            return_value={"found": False},
        ) as detector, patch(
            "model.TaskResultOrganizer.summarize_detection",
            return_value="manual result",
        ), patch(
            "model.TaskResultOrganizer.is_positive_detection",
            return_value=False,
        ):
            routed = build_routed_batches(
                [(root, [video])],
                config,
                task_by_file=task_by_file,
            )

        self.assertEqual(detector.call_args.kwargs["detection_mode"], "manual")
        self.assertNotIn("force_refresh", detector.call_args.kwargs)
        self.assertEqual(routed, [(root, [video], Path("Alice"))])

    def test_forced_manual_review_reuses_cached_result(self):
        expected = {"found": True, "mode": "manual"}
        with patch(
            "model.VideoElementDetector.read_cached_detection",
            return_value=expected,
        ) as cached, patch(
            "model.VideoElementDetector.manual_review_video",
        ) as manual_review, patch(
            "model.VideoElementDetector.save_detection_report",
        ) as save_report:
            result = detect_video_element(
                Path("video.mp4"),
                detection_mode="manual",
            )

        cached.assert_called_once()
        manual_review.assert_not_called()
        save_report.assert_not_called()
        self.assertEqual(result, expected)

    def test_manual_review_can_mark_video_as_do_not_upload(self):
        with patch(
            "model.VideoElementDetector.extract_changed_frames",
            return_value=([], {"selected_frame_count": 0}),
        ), patch(
            "model.VideoElementDetector.build_contact_sheet",
            return_value=b"contact-sheet",
        ), patch(
            "model.VideoElementDetector.ask_manual_review_result",
            return_value=MANUAL_ACTION_SKIP_UPLOAD,
        ):
            result = manual_review_video(Path("video.mp4"))

        self.assertFalse(result["found"])
        self.assertTrue(result["skip_upload"])
        self.assertEqual(result["manual_action"], MANUAL_ACTION_SKIP_UPLOAD)
        self.assertIn("不上传", result["summary"])

    def test_do_not_upload_result_is_saved_and_reused_from_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"video")
            save_detection_report(
                video,
                {
                    "found": False,
                    "skip_upload": True,
                    "mode": "manual",
                },
            )

            cached = read_cached_detection(video, "manual")

        self.assertIsNotNone(cached)
        self.assertTrue(cached["skip_upload"])
        self.assertTrue(cached["from_cache"])

    def test_do_not_upload_result_is_removed_from_upload_batches(self):
        root = Path("result")
        video = root / "AliceMARKER-0825-2-title.mp4"
        task_by_file = {
            file_identity(video): SimpleNamespace(review_required=True),
        }
        config = {
            "video_detection_mode": "manual",
            "enable_video_review_detection": True,
            "video_filename_creator_marker": "MARKER",
        }

        with patch(
            "model.TaskResultOrganizer.detect_video_element",
            return_value={
                "found": False,
                "skip_upload": True,
                "summary": "人工审核：视频有问题，不上传",
            },
        ), patch(
            "model.TaskResultOrganizer.summarize_detection",
            return_value="视频有问题，不上传",
        ), patch(
            "model.TaskResultOrganizer.is_positive_detection",
        ) as positive_detection:
            routed = build_routed_batches(
                [(root, [video])],
                config,
                task_by_file=task_by_file,
            )

        positive_detection.assert_not_called()
        self.assertEqual(routed, [])

    def test_local_no_skips_even_manual_review_mode(self):
        root = Path("result")
        video = root / "AliceMARKER-0825-2-title.mp4"
        task_by_file = {
            file_identity(video): SimpleNamespace(review_required=False),
        }
        config = {
            "video_detection_mode": "manual",
            "enable_video_review_detection": True,
            "video_filename_creator_marker": "MARKER",
        }

        with patch("model.TaskResultOrganizer.detect_video_element") as detector:
            routed = build_routed_batches(
                [(root, [video])],
                config,
                task_by_file=task_by_file,
            )

        detector.assert_not_called()
        self.assertEqual(routed, [(root, [video], Path("Alice"))])

    def test_subcategory_routes_video_below_person_folder(self):
        root = Path("result")
        video = root / "AliceMARKER-0825-2-title.mp4"
        task_by_file = {
            file_identity(video): SimpleNamespace(
                review_required=False,
                subcategory="类别A/大",
            ),
        }
        config = {
            "video_detection_mode": "manual",
            "enable_video_review_detection": True,
            "video_filename_creator_marker": "MARKER",
        }

        routed = build_routed_batches(
            [(root, [video])],
            config,
            task_by_file=task_by_file,
        )

        self.assertEqual(
            routed,
            [(root, [video], Path("Alice") / "类别A" / "大")],
        )

    def test_multilevel_drive_path_keeps_person_root_link(self):
        service = MagicMock()
        with patch(
            "model.GoogleDriveHelper.get_or_create_remote_folder",
            side_effect=["person-id", "angel-id", "large-id"],
        ) as create_folder:
            deepest_id, root_id = get_or_create_remote_folder_path_with_root(
                service,
                "slot-id",
                Path("Alice") / "类别A" / "大",
            )

        self.assertEqual(deepest_id, "large-id")
        self.assertEqual(root_id, "person-id")
        self.assertEqual(
            [call.args[2] for call in create_folder.call_args_list],
            ["Alice", "类别A", "大"],
        )
        links = collect_person_folder_links(
            [{
                "remote_prefix": str(Path("Alice") / "类别A" / "大"),
                "relative_path": str(Path("Alice") / "类别A" / "大" / "video.mp4"),
                "remote_prefix_folder_id": "large-id",
                "remote_prefix_root_folder_id": "person-id",
            }],
            "manual-review",
        )
        self.assertEqual(
            links,
            {"Alice": "https://drive.google.com/drive/folders/person-id"},
        )
        self.assertFalse(
            is_review_upload_record(
                {
                    "remote_prefix": str(Path("Alice") / "manual-review"),
                    "relative_path": str(
                        Path("Alice") / "manual-review" / "video.mp4"
                    ),
                },
                "manual-review",
            )
        )
        self.assertTrue(
            is_review_upload_record(
                {
                    "remote_prefix": "manual-review",
                    "relative_path": str(Path("manual-review") / "video.mp4"),
                },
                "manual-review",
            )
        )
        self.assertFalse(
            record_needs_review(
                {
                    "remote_prefix": str(Path("Alice") / "manual-review"),
                    "relative_path": str(
                        Path("Alice") / "manual-review" / "video.mp4"
                    ),
                },
                "manual-review",
            )
        )

    def test_task_metadata_survives_compression_stage_for_sheet_writeback(self):
        root = Path("result")
        video = root / "AliceMARKER-0825-2-title.mp4"
        task = SimpleNamespace(
            task_type="short-video",
            task_type_from_table=True,
            review_required=False,
            source_row=7,
        )
        source_tasks = {file_identity(video): task}
        upload_tasks = {}

        batches = compress_routed_batches(
            [(root, [video], Path("Alice"))],
            {"compress_enabled": False},
            task_by_file=source_tasks,
            upload_task_by_file=upload_tasks,
        )
        records = [{"local_file": str(batches[0][1][0])}]
        attach_local_task_metadata(records, upload_tasks)

        self.assertEqual(records[0]["local_task_type"], "short-video")
        self.assertIs(records[0]["review_required_override"], False)
        self.assertEqual(records[0]["local_task_row"], 7)

    def test_default_task_type_is_not_attached_for_google_writeback(self):
        video = Path("result/default-type.mp4")
        task = SimpleNamespace(
            task_type="reels",
            task_type_from_table=False,
            review_required=None,
            source_row=8,
        )
        records = [{"local_file": str(video)}]

        attach_local_task_metadata(records, {file_identity(video): task})

        self.assertNotIn("local_task_type", records[0])
        self.assertEqual(records[0]["local_task_row"], 8)


if __name__ == "__main__":
    unittest.main()
