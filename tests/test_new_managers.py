import json
import logging
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
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
    material_directory_stats,
)
from model.MaterialSourceDownloader import (
    DownloadError,
    download_google_drive_source,
    parse_material_drive_link,
    resolve_google_drive_folder_name,
)
from model.AudioSettings import (
    AudioSettingsError,
    build_audio_profile,
    extra_settings_json,
    normalize_audio_settings,
    voice_lines_from_profile,
)
from model.AboutInfo import MAINTENANCE_LESSONS, maintenance_lessons_text
from model.AppLogger import _create_file_handler
from model.AudioHelper import _save_audio_with_api_keys
from model.DailyLinkHistory import (
    daily_link_counts,
    daily_task_sheet_failure_count,
    daily_task_sheet_failures,
    format_daily_links,
    normalize_daily_link_history,
    record_daily_person_links,
    update_daily_task_sheet_results,
)
from model.OdsHelper import (
    ReadTaskOds2,
    normalize_review_required,
    normalize_subcategory_path,
)
from model.TaskTableSchema import normalize_task_table_schema
from model.TaskSubmissionHelper import (
    ORAL_LONG_VIDEO_TYPE,
    ORAL_SHORT_VIDEO_TYPE,
    build_task_sheet_updates,
    column_letter,
    ensure_sheet_row_capacity,
    extract_product_file_name,
    populate_link_only_product_file_names,
    read_task_sheet_rows,
    record_needs_review,
    oral_video_type_for_record,
    resolve_task_submission_layout,
    write_task_submission_links,
)
from model.TaskReferenceDownloader import (
    TaskReferenceDownloadCache,
    TaskReferenceJob,
    partition_cached_reference_jobs,
)
from model.TaskResultExporter import (
    export_task,
    legacy_output_name,
    limit_output_filename,
    output_name,
)
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
    task_uses_oral_source_dir,
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
from model.VideoCompressor import make_compressed_file_name


TEST_TASK_TABLE_SCHEMA = normalize_task_table_schema(
    {
        "labels": {
            "task_id": "Record ID",
            "admin": "Owner",
            "creator": "Operator",
            "task_name": "Title",
            "task_type": "Category",
            "submission_task_type": "Submission category",
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
            "submission_task_type": {"aliases": ["submission_category"]},
            "task_date": {"aliases": ["date", "legacy_date"]},
            "task_reference_link": {"aliases": ["reference", "legacy_reference"]},
            "task_audio_text": {"aliases": ["content", "source_text", "legacy_content"]},
            "task_audio_type": {"aliases": ["voice_profile", "legacy_voice"]},
            "review_required": {"aliases": ["needs_review"]},
            "subcategory": {"aliases": ["subcategory"]},
        },
        "submission_sheet": {
            "labels": {
                "task_date": "Date",
                "requester": "Requester",
                "chinese": "Source text",
                "video_type": "Video type",
                "creator": "Operator",
                "completed_at": "Completed at",
                "review_status": "Status",
                "product_link": "Result URL",
            },
            "fields": {
                "task_date": {"aliases": ["date"]},
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


class AudioSettingsTests(unittest.TestCase):
    def test_profiles_are_copied_and_unknown_fields_are_preserved(self):
        source = {
            "voice-a": {
                "model": "edge",
                "sex": "female",
                "custom_future_option": {"enabled": True},
            }
        }
        profiles = normalize_audio_settings(source)
        profiles["voice-a"]["custom_future_option"]["enabled"] = False

        self.assertTrue(source["voice-a"]["custom_future_option"]["enabled"])
        self.assertIn("custom_future_option", extra_settings_json(source["voice-a"]))

    def test_build_elevenlabs_profile_supports_per_voice_speed(self):
        profile = build_audio_profile(
            "elevenlabs",
            sex="female",
            speed=1.0,
            voice_lines="voice-one\nvoice-two | 0.85",
            extra_json='{"future_option": 3}',
            original_profile={
                "voices": [
                    {"id": "voice-one", "future_voice_option": "preserved"}
                ]
            },
        )

        self.assertEqual(
            profile["voices"][0],
            {"id": "voice-one", "future_voice_option": "preserved"},
        )
        self.assertEqual(
            profile["voices"][1],
            {"id": "voice-two", "speed": 0.85},
        )
        self.assertEqual(profile["future_option"], 3)
        self.assertEqual(
            voice_lines_from_profile(profile),
            "voice-one\nvoice-two | 0.85",
        )

    def test_elevenlabs_profile_requires_a_voice(self):
        with self.assertRaises(AudioSettingsError):
            build_audio_profile("elevenlabs", voice_lines="")


class AboutInfoTests(unittest.TestCase):
    def test_maintenance_lessons_are_structured_and_copyable(self):
        self.assertGreaterEqual(len(MAINTENANCE_LESSONS), 10)
        for lesson in MAINTENANCE_LESSONS:
            self.assertTrue(lesson["title"].strip())
            self.assertTrue(lesson["mistake"].strip())
            self.assertTrue(lesson["rule"].strip())

        text = maintenance_lessons_text()
        self.assertIn("破坏原生库导入顺序", text)
        self.assertIn("把故障延后误当成修复", text)
        self.assertIn("以后必须遵守", text)


class ApplicationLoggingTests(unittest.TestCase):
    def test_file_handler_rotates_and_keeps_bounded_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, log_path = _create_file_handler(Path(directory), 256, 2)
            test_logger = logging.getLogger("assistant_tool_test_rotation")
            test_logger.propagate = False
            test_logger.setLevel(logging.INFO)
            test_logger.addHandler(handler)
            try:
                for index in range(80):
                    test_logger.info("log-line-%03d-%s", index, "x" * 40)
            finally:
                test_logger.removeHandler(handler)
                handler.close()

            log_files = list(log_path.parent.glob("assistant-tool.log*"))
            self.assertGreater(len(log_files), 1)
            self.assertLessEqual(len(log_files), 3)

    def test_file_handler_keeps_python_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, log_path = _create_file_handler(
                Path(directory),
                16 * 1024,
                1,
            )
            test_logger = logging.getLogger("assistant_tool_test_traceback")
            test_logger.propagate = False
            test_logger.setLevel(logging.INFO)
            test_logger.addHandler(handler)
            try:
                try:
                    raise RuntimeError("traceback-marker")
                except RuntimeError:
                    test_logger.exception("音频处理异常")
                handler.flush()
            finally:
                test_logger.removeHandler(handler)
                handler.close()

            content = log_path.read_text(encoding="utf-8")
            self.assertIn("Traceback", content)
            self.assertIn("RuntimeError: traceback-marker", content)

    @patch("model.AudioHelper.ElevenLabs")
    def test_audio_api_progress_uses_callback_and_masks_key(self, client_class):
        client_class.return_value = object()
        messages = []
        api_key = "abcdefghijklmno"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "task_audio.mp3"
            created = _save_audio_with_api_keys(
                [api_key],
                str(target),
                lambda _client: [b"audio-data"],
                api_key_status_config=None,
                progress_callback=messages.append,
            )

            self.assertTrue(created)
            self.assertEqual(target.read_bytes(), b"audio-data")

        progress_text = "\n".join(messages)
        self.assertIn("正在尝试 ElevenLabs API Key", progress_text)
        self.assertIn("音频已保存", progress_text)
        self.assertNotIn(api_key, progress_text)


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

    def test_material_store_copies_multiple_sources_and_check_removes_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_file = root / "logo.png"
            source_file.write_bytes(b"image")
            source_folder = root / "opening"
            source_folder.mkdir()
            (source_folder / "clip.mp4").write_bytes(b"video")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )

            material = store.add_material(
                "常用素材",
                [source_file, source_folder],
            )

            saved_dir = Path(material["path"])
            self.assertTrue((saved_dir / "logo.png").is_file())
            self.assertTrue((saved_dir / "opening" / "clip.mp4").is_file())
            self.assertEqual(store.list_materials()[0]["name"], "常用素材")
            self.assertEqual(material_directory_stats(saved_dir)["file_count"], 2)

            for saved_file in saved_dir.rglob("*"):
                if saved_file.is_file():
                    saved_file.unlink()
            result = store.check_materials()

            self.assertEqual(result["kept"], [])
            self.assertEqual(result["removed"][0]["check_reason"], "素材目录已空")
            self.assertEqual(store.list_materials(), [])

    def test_material_can_use_first_google_folder_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )

            def fake_copy(_sources, staging_dir, progress_callback=None):
                (staging_dir / "clip.mp4").write_bytes(b"video")

            with patch(
                "model.InventoryManager.resolve_google_drive_folder_name",
                return_value="Cloud Pack",
            ) as resolver, patch.object(
                store, "_copy_material_sources", side_effect=fake_copy
            ):
                material = store.add_material(
                    "",
                    ["https://drive.google.com/drive/folders/folder-456"],
                    use_drive_folder_name=True,
                )

            self.assertEqual(material["name"], "Cloud Pack")
            self.assertTrue(Path(material["path"]).name.startswith("Cloud Pack-"))
            resolver.assert_called_once()

    def test_drive_folder_auto_name_requires_a_folder_link(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InventoryStore(Path(directory) / "inventory.json")
            with self.assertRaisesRegex(ValueError, "Google Drive 文件夹"):
                store.add_material(
                    "",
                    ["https://drive.google.com/file/d/file-123/view"],
                    use_drive_folder_name=True,
                )

    def test_removing_material_record_keeps_copied_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_file = root / "source.txt"
            source_file.write_text("material", encoding="utf-8")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            material = store.add_material("文案", [source_file])
            saved_dir = Path(material["path"])

            store.remove_material_record(material["id"])

            self.assertEqual(store.list_materials(), [])
            self.assertTrue((saved_dir / "source.txt").is_file())

    def test_material_store_accepts_mixed_local_and_drive_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_file = root / "local.txt"
            local_file.write_text("local", encoding="utf-8")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            drive_url = "https://drive.google.com/file/d/drive-file-123/view"
            progress = []

            def fake_download(url, output_dir, progress_callback=None):
                self.assertEqual(url, drive_url)
                target = Path(output_dir) / "cloud.mp4"
                target.write_bytes(b"cloud")
                if progress_callback:
                    progress_callback("网盘文件下载完成：cloud.mp4")
                return SimpleNamespace(downloaded_files=1)

            with patch(
                "model.InventoryManager.download_google_drive_source",
                side_effect=fake_download,
            ) as downloader:
                material = store.add_material(
                    "混合素材",
                    [
                        local_file,
                        drive_url,
                        drive_url + "?duplicate=1",
                    ],
                    progress_callback=progress.append,
                )

            saved_dir = Path(material["path"])
            self.assertTrue((saved_dir / "local.txt").is_file())
            self.assertTrue((saved_dir / "cloud.mp4").is_file())
            self.assertEqual(downloader.call_count, 1)
            self.assertEqual(material["source_summary"], "本地 1 / 网盘 1")
            self.assertEqual(
                [source["type"] for source in material["sources"]],
                ["local", "google_drive"],
            )
            self.assertIn("网盘文件下载完成", "\n".join(progress))

    def test_failed_drive_material_leaves_no_record_or_partial_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            with patch(
                "model.InventoryManager.download_google_drive_source",
                side_effect=DownloadError("没有访问权限"),
            ):
                with self.assertRaises(DownloadError):
                    store.add_material(
                        "失败素材",
                        ["https://drive.google.com/file/d/private-file/view"],
                    )

            self.assertEqual(store.list_materials(), [])
            self.assertEqual(list((root / "library").iterdir()), [])

    def test_append_material_adds_files_to_existing_directory_and_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_source = root / "first" / "clip.mp4"
            first_source.parent.mkdir()
            first_source.write_bytes(b"first")
            second_source = root / "second" / "clip.mp4"
            second_source.parent.mkdir()
            second_source.write_bytes(b"second")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            original = store.add_material("视频片段", [first_source])
            original_path = original["path"]
            original_created_at = original["created_at"]

            updated = store.append_material(original["id"], [second_source])

            saved_dir = Path(updated["path"])
            self.assertEqual(updated["path"], original_path)
            self.assertEqual(updated["created_at"], original_created_at)
            self.assertEqual((saved_dir / "clip.mp4").read_bytes(), b"first")
            self.assertEqual((saved_dir / "clip (2).mp4").read_bytes(), b"second")
            self.assertEqual(updated["source_count"], 2)
            self.assertEqual(updated["source_summary"], "本地 2")
            self.assertEqual(len(store.list_materials()), 1)

    def test_failed_append_keeps_existing_material_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "existing.txt"
            source.write_text("keep", encoding="utf-8")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            original = store.add_material("已有素材", [source])
            saved_dir = Path(original["path"])
            before_record = store.list_materials()[0]

            with patch(
                "model.InventoryManager.download_google_drive_source",
                side_effect=DownloadError("没有访问权限"),
            ):
                with self.assertRaises(DownloadError):
                    store.append_material(
                        original["id"],
                        ["https://drive.google.com/file/d/private-file/view"],
                    )

            self.assertEqual(store.list_materials()[0], before_record)
            self.assertEqual((saved_dir / "existing.txt").read_text(encoding="utf-8"), "keep")
            self.assertEqual(list(saved_dir.iterdir()), [saved_dir / "existing.txt"])
            self.assertFalse(any("append" in path.name for path in (root / "library").iterdir()))

    def test_person_profile_stores_avatar_sheet_bindings_and_materials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            avatar = root / "portrait.png"
            avatar.write_bytes(b"png")
            first_source = root / "first" / "clip.mp4"
            first_source.parent.mkdir()
            first_source.write_bytes(b"first")
            second_source = root / "second" / "clip.mp4"
            second_source.parent.mkdir()
            second_source.write_bytes(b"second")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            first_sheet = "https://docs.google.com/spreadsheets/d/sheet-one/edit#gid=0"
            second_tab = "https://docs.google.com/spreadsheets/d/sheet-one/edit#gid=123"

            person = store.add_person(
                "Alice",
                avatar_path=avatar,
                google_sheet_links=[
                    first_sheet,
                    first_sheet + "&duplicate=1",
                    second_tab,
                ],
                source_paths=[first_source],
                metadata={"tag": "lead"},
                bindings={"future_service": {"account": "A1"}},
            )

            self.assertTrue(Path(person["avatar_path"]).is_file())
            self.assertEqual(
                person["bindings"]["google_sheets"],
                [first_sheet, second_tab],
            )
            self.assertEqual(
                person["bindings"]["future_service"],
                {"account": "A1"},
            )
            self.assertEqual(person["metadata"], {"tag": "lead"})
            material_dir = Path(person["material_path"])
            self.assertEqual((material_dir / "clip.mp4").read_bytes(), b"first")

            updated = store.append_person_material(person["id"], [second_source])

            self.assertEqual((material_dir / "clip (2).mp4").read_bytes(), b"second")
            self.assertEqual(updated["source_count"], 2)
            self.assertEqual(updated["source_summary"], "本地 2")

    def test_person_profile_edit_preserves_extension_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            avatar = root / "portrait.jpg"
            avatar.write_bytes(b"jpg")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            person = store.add_person(
                "Before",
                avatar_path=avatar,
                google_sheet_links=[],
                metadata={"existing": True},
                bindings={"future_service": ["value"]},
            )
            new_sheet = "https://docs.google.com/spreadsheets/d/sheet-two/edit"

            store.update_person_profile(
                person["id"],
                "After",
                google_sheet_links=[new_sheet],
                remove_avatar=True,
                metadata_update={"note": "updated"},
            )
            reloaded = store.list_people()[0]

            self.assertEqual(reloaded["name"], "After")
            self.assertEqual(reloaded["avatar_path"], "")
            self.assertFalse(Path(person["avatar_path"]).exists())
            self.assertEqual(reloaded["bindings"]["google_sheets"], [new_sheet])
            self.assertEqual(reloaded["bindings"]["future_service"], ["value"])
            self.assertEqual(
                reloaded["metadata"],
                {"existing": True, "note": "updated"},
            )

    def test_empty_person_material_record_is_kept_by_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            person = store.add_person("No Assets")

            result = store.check_people()

            self.assertEqual(result["removed"], [])
            self.assertEqual(result["kept"][0]["id"], person["id"])
            self.assertTrue(Path(person["material_path"]).is_dir())

    def test_failed_person_profile_write_restores_previous_avatar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_avatar = root / "old.png"
            old_avatar.write_bytes(b"old")
            new_avatar = root / "new.png"
            new_avatar.write_bytes(b"new")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            person = store.add_person("Alice", avatar_path=old_avatar)
            saved_avatar = Path(person["avatar_path"])

            with patch.object(store, "_write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.update_person_profile(
                        person["id"],
                        "Alice",
                        avatar_path=new_avatar,
                    )

            self.assertEqual(saved_avatar.read_bytes(), b"old")
            self.assertEqual(store.list_people()[0]["avatar_path"], str(saved_avatar))

    def test_failed_person_append_keeps_profile_and_materials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "existing.mp4"
            source.write_bytes(b"keep")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            person = store.add_person("Alice", source_paths=[source])
            before_record = store.list_people()[0]

            with patch(
                "model.InventoryManager.download_google_drive_source",
                side_effect=DownloadError("下载失败"),
            ):
                with self.assertRaises(DownloadError):
                    store.append_person_material(
                        person["id"],
                        ["https://drive.google.com/file/d/private-file/view"],
                    )

            self.assertEqual(store.list_people()[0], before_record)
            material_dir = Path(person["material_path"])
            self.assertEqual((material_dir / "existing.mp4").read_bytes(), b"keep")
            self.assertFalse(
                any("append" in path.name for path in store.people_root.iterdir())
            )

    def test_person_can_import_existing_material_and_keep_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "clip.mp4"
            source.write_bytes(b"video")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            material = store.add_material("天使素材", [source])
            person = store.add_person("Alice")

            result = store.import_materials_to_person(
                person["id"],
                [material["id"]],
            )

            imported_file = Path(person["material_path"]) / "天使素材" / "clip.mp4"
            self.assertEqual(imported_file.read_bytes(), b"video")
            self.assertTrue(Path(material["path"]).is_dir())
            self.assertEqual(len(store.list_materials()), 1)
            reloaded_person = store.list_people()[0]
            self.assertEqual(reloaded_person["source_summary"], "素材库 1")
            self.assertEqual(
                reloaded_person["sources"][0]["original_path"],
                str(Path(material["path"]).resolve()),
            )
            self.assertEqual(len(result["imported_materials"]), 1)
            self.assertEqual(result["removed_materials"], [])

    def test_person_import_can_remove_original_materials_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_source = root / "first.mp4"
            first_source.write_bytes(b"first")
            second_source = root / "second.mp4"
            second_source.write_bytes(b"second")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            first = store.add_material("第一组", [first_source])
            second = store.add_material("第二组", [second_source])
            person = store.add_person("Alice")

            result = store.import_materials_to_person(
                person["id"],
                [first["id"], second["id"]],
                remove_originals=True,
            )

            person_materials = Path(person["material_path"])
            self.assertEqual(
                (person_materials / "第一组" / "first.mp4").read_bytes(),
                b"first",
            )
            self.assertEqual(
                (person_materials / "第二组" / "second.mp4").read_bytes(),
                b"second",
            )
            self.assertEqual(store.list_materials(), [])
            self.assertFalse(Path(first["path"]).exists())
            self.assertFalse(Path(second["path"]).exists())
            self.assertEqual(len(result["removed_materials"]), 2)
            self.assertFalse(
                any("material-import-trash" in path.name for path in store.material_root.iterdir())
            )

    def test_failed_destructive_person_import_restores_original_and_person(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("keep", encoding="utf-8")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            material = store.add_material("已有素材", [source])
            person = store.add_person("Alice")
            original_path = Path(material["path"])
            before_person = store.list_people()[0]

            with patch.object(store, "_write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.import_materials_to_person(
                        person["id"],
                        [material["id"]],
                        remove_originals=True,
                    )

            self.assertEqual(
                (original_path / "source.txt").read_text(encoding="utf-8"),
                "keep",
            )
            self.assertEqual(store.list_materials()[0]["id"], material["id"])
            self.assertEqual(store.list_people()[0], before_person)
            self.assertEqual(list(Path(person["material_path"]).iterdir()), [])
            self.assertFalse(
                any("material-import" in path.name for path in store.material_root.iterdir())
            )


    def test_material_images_can_be_moved_to_multiple_tasks_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "incoming"
            source_dir.mkdir()
            (source_dir / "cover.png").write_bytes(b"new-cover")
            (source_dir / "portrait.jpg").write_bytes(b"portrait")
            (source_dir / "notes.txt").write_text("keep", encoding="utf-8")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            material = store.add_material("图片素材", [source_dir])
            images = store.list_material_images()
            self.assertEqual(
                {image["name"] for image in images},
                {"cover.png", "portrait.jpg"},
            )

            first_target = root / "tasks" / "1"
            second_target = root / "tasks" / "2"
            first_target.mkdir(parents=True)
            (first_target / "cover.png").write_bytes(b"existing")
            assignments = []
            for image in images:
                assignments.append({
                    "path": image["path"],
                    "target_dir": str(
                        first_target
                        if image["name"] == "cover.png"
                        else second_target
                    ),
                    "task_label": "任务1" if image["name"] == "cover.png" else "任务2",
                })

            result = store.move_material_images(assignments)

            self.assertEqual(len(result["moved"]), 2)
            self.assertEqual((first_target / "cover.png").read_bytes(), b"existing")
            self.assertEqual((first_target / "cover (2).png").read_bytes(), b"new-cover")
            self.assertEqual((second_target / "portrait.jpg").read_bytes(), b"portrait")
            self.assertEqual(len(store.list_materials()), 1)
            self.assertTrue(Path(material["path"]).joinpath("incoming", "notes.txt").is_file())

    def test_moving_last_material_image_removes_empty_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "only.png"
            source.write_bytes(b"image")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            material = store.add_material("一次性图片", [source])
            image = store.list_material_images()[0]
            target_dir = root / "task"

            result = store.move_material_images([{
                "path": image["path"],
                "target_dir": str(target_dir),
                "task_label": "任务A",
            }])

            self.assertEqual(store.list_materials(), [])
            self.assertFalse(Path(material["path"]).exists())
            self.assertEqual((target_dir / "only.png").read_bytes(), b"image")
            self.assertEqual(result["removed_materials"][0]["id"], material["id"])

    def test_material_image_move_rolls_back_when_state_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "only.jpg"
            source.write_bytes(b"image")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            material = store.add_material("回滚图片", [source])
            image = store.list_material_images()[0]
            stored_image_path = Path(image["path"])
            target_dir = root / "task"

            with patch.object(store, "_write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.move_material_images([{
                        "path": image["path"],
                        "target_dir": str(target_dir),
                    }])

            self.assertEqual(stored_image_path.read_bytes(), b"image")
            self.assertFalse((target_dir / "only.jpg").exists())
            self.assertEqual(store.list_materials()[0]["id"], material["id"])

    def test_image_groups_include_material_and_person_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material_source = root / "material.png"
            person_source = root / "person.jpg"
            material_source.write_bytes(b"material")
            person_source.write_bytes(b"person")
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            store.add_material("普通素材组", [material_source])
            person = store.add_person("Alice", source_paths=[person_source])

            groups = store.list_image_groups()

            self.assertEqual(
                [(group["source_kind"], group["name"]) for group in groups],
                [("material", "普通素材组"), ("person", "Alice")],
            )
            self.assertEqual([group["image_count"] for group in groups], [1, 1])

            person_group = groups[1]
            target_dir = root / "task"
            result = store.move_material_images([{
                "path": person_group["images"][0]["path"],
                "target_dir": str(target_dir),
                "task_label": "任务1",
            }])

            self.assertEqual(result["moved"][0]["source_kind"], "person")
            self.assertEqual((target_dir / "person.jpg").read_bytes(), b"person")
            self.assertEqual(store.list_people()[0]["id"], person["id"])
            self.assertEqual(store.list_image_groups()[1]["image_count"], 0)


class MaterialSourceDownloaderTests(unittest.TestCase):
    def test_parse_file_folder_and_google_document_links(self):
        file_link = parse_material_drive_link(
            "https://drive.google.com/file/d/file-123/view?resourcekey=key-1"
        )
        folder_link = parse_material_drive_link(
            "https://drive.google.com/drive/u/0/folders/folder-456?resourcekey=key-2"
        )
        doc_link = parse_material_drive_link(
            "https://docs.google.com/document/d/document-789/edit"
        )

        self.assertEqual(file_link.identity, "file:file-123")
        self.assertEqual(file_link.resource_key, "key-1")
        self.assertTrue(folder_link.is_folder)
        self.assertEqual(folder_link.identity, "folder:folder-456")
        self.assertEqual(doc_link.google_type, "document")

    def test_rejects_non_google_url(self):
        with self.assertRaises(DownloadError):
            parse_material_drive_link("https://example.com/file.mp4")

    def test_resolve_google_drive_folder_name_reads_real_metadata(self):
        request = MagicMock()
        request.execute.return_value = {
            "id": "folder-456",
            "name": "客户补充素材",
            "mimeType": "application/vnd.google-apps.folder",
        }
        files_api = MagicMock()
        files_api.get.return_value = request
        service = MagicMock()
        service.files.return_value = files_api

        name = resolve_google_drive_folder_name(
            "https://drive.google.com/drive/folders/folder-456",
            service_factory=lambda: service,
        )

        self.assertEqual(name, "客户补充素材")
        files_api.get.assert_called_once()

    def test_resolve_folder_name_rejects_google_file_link(self):
        with self.assertRaisesRegex(DownloadError, "文件夹链接"):
            resolve_google_drive_folder_name(
                "https://drive.google.com/file/d/file-123/view",
                service_factory=MagicMock,
            )

    def test_authenticated_folder_download_is_recursive_and_exports_docs(self):
        class FakeRequest:
            def __init__(self, payload=None, data=b""):
                self.payload = payload
                self.data = data

            def execute(self):
                return self.payload

        class FakeFiles:
            def get(self, fileId, **_kwargs):
                self.requested_id = fileId
                return FakeRequest(
                    {
                        "id": "folder-456",
                        "name": "Cloud Pack",
                        "mimeType": "application/vnd.google-apps.folder",
                    }
                )

            def list(self, q, **_kwargs):
                if "'folder-456'" in q:
                    children = [
                        {
                            "id": "video-1",
                            "name": "clip.mp4",
                            "mimeType": "video/mp4",
                        },
                        {
                            "id": "subfolder-1",
                            "name": "Documents",
                            "mimeType": "application/vnd.google-apps.folder",
                        },
                    ]
                else:
                    children = [
                        {
                            "id": "doc-1",
                            "name": "Readme",
                            "mimeType": "application/vnd.google-apps.document",
                        }
                    ]
                return FakeRequest({"files": children})

            def get_media(self, fileId, **_kwargs):
                return FakeRequest(data=f"file:{fileId}".encode())

            def export_media(self, fileId, mimeType):
                return FakeRequest(data=f"export:{fileId}:{mimeType}".encode())

        class FakeService:
            def __init__(self):
                self.files_api = FakeFiles()

            def files(self):
                return self.files_api

        class FakeStatus:
            @staticmethod
            def progress():
                return 1.0

        class FakeDownloader:
            def __init__(self, stream, request, chunksize):
                self.stream = stream
                self.request = request

            def next_chunk(self):
                self.stream.write(self.request.data)
                return FakeStatus(), True

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with patch("googleapiclient.http.MediaIoBaseDownload", FakeDownloader):
                result = download_google_drive_source(
                    "https://drive.google.com/drive/folders/folder-456",
                    output_dir,
                    service_factory=FakeService,
                )

            root = output_dir / "Cloud Pack"
            self.assertEqual(result.downloaded_files, 2)
            self.assertTrue(result.used_authenticated_api)
            self.assertEqual((root / "clip.mp4").read_bytes(), b"file:video-1")
            self.assertTrue((root / "Documents" / "Readme.docx").is_file())


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

    def test_routing_type_and_submission_type_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "separate-types.ods"
            document = OpenDocumentSpreadsheet()
            table = OdfTable(name="sample")
            document.spreadsheet.addElement(table)
            self._add_row(
                table,
                ["owner", "title", "record_id", "category", "submission_category"],
            )
            self._add_row(
                table,
                ["Alice", "task-a", "10", "routing-reels", "client-type-a"],
            )
            self._add_row(
                table,
                ["Bob", "task-b", "11", "routing-reels", ""],
            )
            document.save(str(path))

            tasks = ReadTaskOds2(path, schema=TEST_TASK_TABLE_SCHEMA)

            self.assertEqual(tasks[0].task_type, "routing-reels")
            self.assertEqual(tasks[0].submission_task_type, "client-type-a")
            self.assertTrue(tasks[0].submission_task_type_from_table)
            self.assertEqual(tasks[1].task_type, "routing-reels")
            self.assertEqual(tasks[1].submission_task_type, "")
            self.assertFalse(tasks[1].submission_task_type_from_table)

    def test_submission_type_is_legacy_routing_fallback_when_only_type_column(self):
        schema = normalize_task_table_schema(
            {
                "fields": {
                    "task_id": {"aliases": ["record_id"], "default": "{row}"},
                    "task_name": {"aliases": ["title"], "default": "{task_id}"},
                    "task_type": {"aliases": ["category"], "default": ""},
                    "submission_task_type": {
                        "aliases": ["submission_category"],
                        "default": "",
                    },
                }
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-one-type.ods"
            document = OpenDocumentSpreadsheet()
            table = OdfTable(name="sample")
            document.spreadsheet.addElement(table)
            self._add_row(table, ["title", "submission_category"])
            self._add_row(table, ["task", "legacy-routing-type"])
            document.save(str(path))

            tasks = ReadTaskOds2(path, schema=schema)

            self.assertEqual(tasks[0].task_type, "legacy-routing-type")
            self.assertEqual(
                tasks[0].submission_task_type,
                "legacy-routing-type",
            )

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
                "task_date": 1,
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

    def test_blank_local_submission_type_leaves_google_video_type_unchanged(self):
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

    def test_oral_video_is_appended_after_the_last_used_row(self):
        column_map = {
            "task_date": 1,
            "requester": 2,
            "chinese": 4,
            "creator": 9,
            "video_type": 10,
            "completed_at": 11,
            "product_link": 13,
            "review_status": 14,
        }
        rows = [{
            "row": 20,
            "task_date": "",
            "requester": "Other",
            "chinese": "existing task",
            "creator": "",
            "video_type": "",
            "completed_at": "",
            "review_status": "",
            "product_link": "",
            "requester_key": "other",
            "task_text_key": "existingtask",
            "task_filename_key": "existingtask",
        }]
        records = [{
            "name": "AliceMARKER-0911-7-oral title.mp4",
            "webViewLink": "https://drive.google.com/file/d/oral123/view",
            "local_is_oral": True,
            "local_video_duration_millis": 60_001,
            "local_admin": "Alice",
            "local_task_name": "oral title",
            "local_task_date": "2026-09-11",
            "task_submission_completed_at": "2026-08-26",
            "relative_path": "Alice/oral.mp4",
        }]

        updates, matched = build_task_sheet_updates(
            {
                "task_submission_creator": "operator",
                "task_submission_creator_marker": "MARKER",
                "review_folder_name": "manual-review",
            },
            records,
            rows,
            "Tasks",
            column_map,
            first_data_row=2,
        )

        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["operation"], "append_oral")
        self.assertEqual(matched[0]["row"], 21)
        self.assertEqual(
            matched[0]["planned_after"]["video_type"],
            ORAL_LONG_VIDEO_TYPE,
        )
        ranges = {item["range"]: item["values"][0][0] for item in updates}
        self.assertEqual(ranges["'Tasks'!A21"], "2026-09-11")
        self.assertEqual(ranges["'Tasks'!B21"], "Alice")
        self.assertEqual(ranges["'Tasks'!D21"], "oral title")
        self.assertEqual(ranges["'Tasks'!J21"], ORAL_LONG_VIDEO_TYPE)
        self.assertEqual(ranges["'Tasks'!K21"], "2026-08-26")
        self.assertEqual(
            ranges["'Tasks'!M21"],
            '=HYPERLINK("https://drive.google.com/file/d/oral123/view",'
            '"AliceMARKER-0911-7-oral title.mp4")',
        )

    def test_oral_append_expands_full_google_sheet_before_write(self):
        class FakeRequest:
            def __init__(self, result):
                self.result = result

            def execute(self):
                return self.result

        class FakeSpreadsheets:
            def __init__(self):
                self.batch_update_calls = []

            def get(self, **_kwargs):
                return FakeRequest({
                    "sheets": [{
                        "properties": {
                            "sheetId": 7,
                            "gridProperties": {"rowCount": 20},
                        }
                    }]
                })

            def batchUpdate(self, **kwargs):
                self.batch_update_calls.append(kwargs)
                return FakeRequest({})

        class FakeService:
            def __init__(self):
                self.api = FakeSpreadsheets()

            def spreadsheets(self):
                return self.api

        service = FakeService()

        added = ensure_sheet_row_capacity(
            service,
            "spreadsheet123",
            sheet_id=7,
            required_row=21,
        )

        self.assertEqual(added, 100)
        request = service.api.batch_update_calls[0]
        self.assertEqual(request["spreadsheetId"], "spreadsheet123")
        self.assertEqual(
            request["body"]["requests"][0]["appendDimension"],
            {
                "sheetId": 7,
                "dimension": "ROWS",
                "length": 100,
            },
        )

    def test_existing_oral_task_is_not_appended_again(self):
        file_name = "AliceMARKER-0911-7-oral title.mp4"
        rows = [{
            "row": 8,
            "task_date": "2026-09-11",
            "requester": "Alice",
            "chinese": "oral title",
            "creator": "operator",
            "video_type": ORAL_SHORT_VIDEO_TYPE,
            "completed_at": "2026-09-11",
            "review_status": "",
            "product_link": (
                '=HYPERLINK("https://drive.google.com/file/d/old/view";'
                f'"{file_name}")'
            ),
            "requester_key": "alice",
            "task_text_key": "oraltitle",
            "task_filename_key": "oraltitle",
        }]
        records = [{
            "name": "[SHANA]" + file_name,
            "webViewLink": "https://drive.google.com/file/d/old/view",
            "local_is_oral": True,
            "local_video_duration_millis": 30_000,
        }]
        already_submitted = []
        failures = []

        updates, matched = build_task_sheet_updates(
            {
                "task_submission_creator": "operator",
                "task_submission_creator_marker": "MARKER",
            },
            records,
            rows,
            "Tasks",
            {
                "requester": 2,
                "chinese": 4,
                "creator": 9,
                "video_type": 10,
                "completed_at": 11,
                "product_link": 13,
                "review_status": 14,
            },
            failed_files=failures,
            already_submitted_files=already_submitted,
        )

        self.assertEqual(updates, [])
        self.assertEqual(matched, [])
        self.assertEqual(already_submitted, ["[SHANA]" + file_name])
        self.assertEqual(failures, [])

    def test_existing_oral_task_with_new_link_replaces_product_link(self):
        file_name = "AliceMARKER-0911-7-oral title.mp4"
        rows = [{
            "row": 8,
            "task_date": "2026-09-11",
            "requester": "Alice",
            "chinese": "oral title",
            "creator": "operator",
            "video_type": ORAL_SHORT_VIDEO_TYPE,
            "completed_at": "2026-09-11",
            "review_status": "",
            "product_link": (
                '=HYPERLINK("https://drive.google.com/file/d/old/view",'
                f'"{file_name}")'
            ),
            "requester_key": "alice",
            "task_text_key": "oraltitle",
            "task_filename_key": "oraltitle",
        }]
        records = [{
            "name": "[SHANA]" + file_name,
            "webViewLink": "https://drive.google.com/file/d/new/view",
            "local_is_oral": True,
            "local_video_duration_millis": 60_001,
        }]

        updates, matched = build_task_sheet_updates(
            {
                "task_submission_creator": "operator",
                "task_submission_creator_marker": "MARKER",
            },
            records,
            rows,
            "Tasks",
            {
                "requester": 2,
                "chinese": 4,
                "creator": 9,
                "video_type": 10,
                "completed_at": 11,
                "product_link": 13,
                "review_status": 14,
            },
        )

        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["operation"], "replace_oral")
        self.assertEqual(matched[0]["row"], 8)
        ranges = {item["range"]: item["values"][0][0] for item in updates}
        self.assertEqual(ranges["'Tasks'!J8"], ORAL_LONG_VIDEO_TYPE)
        self.assertIn("/new/view", ranges["'Tasks'!M8"])

    def test_link_only_result_resolves_drive_name_for_oral_dedupe(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "old123",
            "name": "AliceMARKER-0911-7-oral title.mp4",
            "mimeType": "video/mp4",
        }
        rows = [{
            "row": 8,
            "product_link": "https://drive.google.com/file/d/old123/view",
        }]

        resolved = populate_link_only_product_file_names(rows, service)

        self.assertEqual(resolved, 1)
        self.assertEqual(
            rows[0]["product_file_name"],
            "AliceMARKER-0911-7-oral title.mp4",
        )
        service.files.return_value.get.assert_called_once_with(
            fileId="old123",
            fields="id,name,mimeType",
            supportsAllDrives=True,
        )

    def test_url_used_as_hyperlink_title_is_still_link_only(self):
        link = "https://drive.google.com/file/d/old123/view"
        self.assertEqual(extract_product_file_name(link), "")
        self.assertEqual(
            extract_product_file_name(f'=HYPERLINK("{link}","{link}")'),
            "",
        )

    def test_resolved_link_only_name_prevents_duplicate_append(self):
        file_name = "AliceMARKER-0911-7-oral title.mp4"
        rows = [{
            "row": 8,
            "task_date": "",
            "requester": "",
            "chinese": "",
            "creator": "",
            "video_type": "",
            "completed_at": "",
            "review_status": "",
            "product_link": "https://drive.google.com/file/d/old123/view",
            "product_file_name": file_name,
            "requester_key": "",
            "task_text_key": "",
            "task_filename_key": "",
        }]
        records = [{
            "name": file_name,
            "webViewLink": "https://drive.google.com/file/d/new456/view",
            "local_is_oral": True,
            "local_video_duration_millis": 30_000,
        }]
        already_submitted = []

        updates, matched = build_task_sheet_updates(
            {
                "task_submission_creator": "operator",
                "task_submission_creator_marker": "MARKER",
            },
            records,
            rows,
            "Tasks",
            {
                "requester": 2,
                "chinese": 4,
                "creator": 9,
                "video_type": 10,
                "completed_at": 11,
                "product_link": 13,
                "review_status": 14,
            },
            already_submitted_files=already_submitted,
        )

        self.assertTrue(updates)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["operation"], "replace_oral")
        self.assertEqual(matched[0]["row"], 8)
        self.assertEqual(already_submitted, [])

    def test_oral_dedupe_does_not_merge_different_display_names(self):
        rows = [{
            "row": 8,
            "task_date": "2026-09-11",
            "requester": "Alice",
            "chinese": "first result",
            "creator": "operator",
            "video_type": ORAL_SHORT_VIDEO_TYPE,
            "completed_at": "2026-09-11",
            "review_status": "",
            "product_link": (
                '=HYPERLINK("https://drive.google.com/file/d/old/view",'
                '"AliceMARKER-0911-7-first.mp4")'
            ),
            "requester_key": "alice",
            "task_text_key": "firstresult",
            "task_filename_key": "firstresult",
        }]
        records = [{
            "name": "AliceMARKER-0911-7-second.mp4",
            "webViewLink": "https://drive.google.com/file/d/new/view",
            "local_is_oral": True,
            "local_video_duration_millis": 30_000,
        }]

        _updates, matched = build_task_sheet_updates(
            {
                "task_submission_creator": "operator",
                "task_submission_creator_marker": "MARKER",
            },
            records,
            rows,
            "Tasks",
            {
                "requester": 2,
                "chinese": 4,
                "creator": 9,
                "video_type": 10,
                "completed_at": 11,
                "product_link": 13,
                "review_status": 14,
            },
        )

        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["row"], 9)

    def test_oral_duration_boundary_uses_short_type(self):
        self.assertEqual(
            oral_video_type_for_record({"local_video_duration_millis": 60_000}),
            ORAL_SHORT_VIDEO_TYPE,
        )
        self.assertEqual(
            oral_video_type_for_record({"local_video_duration_millis": 60_001}),
            ORAL_LONG_VIDEO_TYPE,
        )
        self.assertIsNone(oral_video_type_for_record({}))


class TaskResultDetectionChoiceTests(unittest.TestCase):
    def test_compressed_filename_limit_is_explicit_and_old_default_is_unchanged(self):
        source = Path("customer-" + "very-long-title-" * 12 + ".mp4")

        old_name = make_compressed_file_name(source, "[SHANA]")
        limited_name = make_compressed_file_name(
            source,
            "[SHANA]",
            max_length=50,
        )

        self.assertGreater(len(old_name), 50)
        self.assertLessEqual(len(limited_name), 50)
        self.assertTrue(limited_name.endswith(".mp4"))

    def test_output_filename_defaults_to_fifty_and_overwrites_same_prefix(self):
        task = SimpleNamespace(
            task_id="7",
            admin="Alice",
            creator="Operator",
            task_date="0914",
            task_name="A very long customer title " + "word " * 20,
            task_type="video",
        )
        profile = {
            "output_name_template": (
                "{admin}MARKER-{mm}{dd}-{task_id}-{task_name}.mp4"
            )
        }
        first = output_name(task, date(2026, 9, 14), {}, profile)
        task.task_name += "different"
        second = output_name(task, date(2026, 9, 14), {}, profile)

        self.assertLessEqual(len(first), 50)
        self.assertTrue(first.endswith(".mp4"))
        self.assertEqual(first, second)
        self.assertNotIn("~", first)
        self.assertGreater(
            len(legacy_output_name(task, date(2026, 9, 14), {}, profile)),
            50,
        )
        self.assertEqual(
            limit_output_filename("unchanged.mp4", 50), "unchanged.mp4"
        )

    def test_existing_legacy_long_output_is_reused_without_new_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root_dir = Path(directory)
            task_dir = root_dir / "task" / "7"
            task_dir.mkdir(parents=True)
            source = task_dir / "final.mp4"
            source.write_bytes(b"video")
            output_dir = root_dir / "result"
            output_dir.mkdir()
            task = SimpleNamespace(
                task_id="7",
                admin="Alice",
                creator="Operator",
                task_date="0914",
                task_name="long title " * 12,
                task_type="video",
            )
            profile = {
                "mode": "single",
                "task_dir": "task",
                "candidate_names": ["final.mp4"],
                "output_name_template": "{admin}MARKER-0914-{task_id}-{task_name}.mp4",
            }
            config = {"task_output_filename_max_length": 50}
            old_name = legacy_output_name(
                task, date(2026, 9, 14), config, profile
            )
            old_path = output_dir / old_name
            old_path.write_bytes(b"video")
            remembered = []

            updated = export_task(
                task,
                date(2026, 9, 14),
                root_dir,
                output_dir,
                root_dir / "result-wsp",
                profile,
                config,
                False,
                exported_file_callback=(
                    lambda path, _task: remembered.append(path)
                ),
            )

            self.assertEqual(updated, [])
            self.assertEqual(remembered, [old_path])
            self.assertFalse(
                (output_dir / output_name(
                    task, date(2026, 9, 14), config, profile
                )).exists()
            )

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
            self.assertEqual(len(received_first), 1)
            self.assertEqual(len(received_second), 1)
            self.assertTrue(received_first[0].samefile(video))
            self.assertTrue(received_second[0].samefile(video))
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
            self.assertEqual(len(loaded), 1)
            loaded_root, loaded_files = loaded[0]
            self.assertTrue(loaded_root.samefile(root_dir))
            self.assertEqual(len(loaded_files), 2)
            self.assertTrue(loaded_files[0].samefile(first))
            self.assertTrue(loaded_files[1].samefile(second))
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
            submission_task_type="client-video-type",
            submission_task_type_from_table=True,
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

        self.assertEqual(records[0]["local_task_type"], "client-video-type")
        self.assertIs(records[0]["review_required_override"], False)
        self.assertEqual(records[0]["local_task_row"], 7)

    def test_oral_source_directory_marks_record_and_keeps_duration(self):
        video = Path("result/oral.mp4")
        task = SimpleNamespace(
            task_type="custom-oral",
            submission_task_type="",
            submission_task_type_from_table=False,
            review_required=None,
            source_row=9,
            admin="Alice",
            task_name="oral task",
            task_id="7",
            task_date="2026-09-11",
        )
        records = [{"local_file": str(video)}]
        config = {
            "task_export_profiles": {
                "custom-oral": {"task_dir": "口播"},
            },
        }

        self.assertTrue(task_uses_oral_source_dir(task, config))
        with patch(
            "model.TaskResultOrganizer.local_video_duration_millis",
            return_value=61_250,
        ) as duration_reader:
            attach_local_task_metadata(
                records,
                {file_identity(video): task},
                config,
            )

        duration_reader.assert_called_once_with(video, config)
        self.assertTrue(records[0]["local_is_oral"])
        self.assertEqual(records[0]["local_video_duration_millis"], 61_250)
        self.assertEqual(records[0]["local_task_date"], "2026-09-11")

    def test_profile_name_alone_does_not_mark_non_oral_source_directory(self):
        task = SimpleNamespace(task_type="口播")
        config = {"task_export_profiles": {"口播": {"task_dir": "reels"}}}

        self.assertFalse(task_uses_oral_source_dir(task, config))

    def test_routing_type_is_not_attached_when_submission_type_is_blank(self):
        video = Path("result/default-type.mp4")
        task = SimpleNamespace(
            task_type="reels",
            task_type_from_table=True,
            submission_task_type="",
            submission_task_type_from_table=False,
            review_required=None,
            source_row=8,
        )
        records = [{"local_file": str(video)}]

        attach_local_task_metadata(records, {file_identity(video): task})

        self.assertNotIn("local_task_type", records[0])
        self.assertEqual(records[0]["local_task_row"], 8)


class DailyLinkHistoryTests(unittest.TestCase):
    def test_history_merges_batches_and_replaces_same_slot(self):
        now = datetime.now().replace(microsecond=0)
        history, count = record_daily_person_links(
            {},
            now.date(),
            "01",
            {"Alice": "https://example.test/alice-1"},
            now=now,
        )
        self.assertEqual(count, 1)

        history, count = record_daily_person_links(
            history,
            now.date(),
            "02",
            {
                "Alice": "https://example.test/alice-2",
                "Bob": "https://example.test/bob-2",
            },
            now=now,
        )
        self.assertEqual(count, 2)
        history, _ = record_daily_person_links(
            history,
            now.date(),
            "01",
            {"Alice": "https://example.test/alice-new"},
            now=now,
        )

        day_key = now.date().isoformat()
        self.assertEqual(daily_link_counts(history, day_key), (2, 3))
        self.assertEqual(
            history[day_key]["people"]["Alice"]["01"]["link"],
            "https://example.test/alice-new",
        )

    def test_history_keeps_seven_calendar_days(self):
        today = date.today()
        raw = {}
        for offset in range(9):
            day_key = (today - timedelta(days=offset)).isoformat()
            raw[day_key] = {
                "people": {
                    "Alice": {
                        "01": {"link": f"https://example.test/{offset}"},
                    }
                }
            }

        normalized = normalize_daily_link_history(raw, today=today)

        self.assertEqual(len(normalized), 7)
        self.assertIn(today.isoformat(), normalized)
        self.assertIn((today - timedelta(days=6)).isoformat(), normalized)
        self.assertNotIn((today - timedelta(days=7)).isoformat(), normalized)

    def test_daily_copy_text_is_grouped_by_person(self):
        now = datetime.now().replace(microsecond=0)
        history, _ = record_daily_person_links(
            {},
            now.date(),
            "02",
            {
                "Alice": "https://example.test/alice",
                "Bob": "https://example.test/bob",
            },
            now=now,
        )

        text = format_daily_links(history, now.date().isoformat())

        expected_heading = (
            f"{now.year:04d}年{now.month:02d}月{now.day:02d}日"
        )
        self.assertIn(expected_heading, text)
        self.assertIn("Alice：\n批次 02：https://example.test/alice", text)
        self.assertIn("Bob：\n批次 02：https://example.test/bob", text)

    def test_task_sheet_failures_are_saved_and_later_success_resolves_them(self):
        now = datetime.now().replace(microsecond=0)
        failure = {
            "file_name": "AliceMARKER-0904-1-title.mp4",
            "reason": "没有找到匹配行",
        }
        history, count = update_daily_task_sheet_results(
            {},
            now.date(),
            "01",
            [failure],
            now=now,
        )

        day_key = now.date().isoformat()
        self.assertEqual(count, 1)
        self.assertEqual(daily_task_sheet_failure_count(history, day_key), 1)
        self.assertEqual(
            daily_task_sheet_failures(history, day_key)[0]["reason"],
            "没有找到匹配行",
        )

        history, count = update_daily_task_sheet_results(
            history,
            now.date(),
            "02",
            [],
            successful_files=[failure["file_name"]],
            now=now,
        )

        self.assertEqual(count, 0)
        self.assertEqual(daily_task_sheet_failure_count(history, day_key), 0)

    def test_missing_submission_sheet_url_reports_video_name(self):
        report = {}
        file_name = "AliceMARKER-0904-1-title.mp4"

        count = write_task_submission_links(
            {
                "task_submission_sheet_enabled": True,
                "task_submission_sheet_url": "",
            },
            [{"name": file_name}],
            result_report=report,
        )

        self.assertEqual(count, 0)
        self.assertTrue(report["attempted"])
        self.assertEqual(report["failed_files"][0]["file_name"], file_name)
        self.assertIn("表格链接", report["failed_files"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
