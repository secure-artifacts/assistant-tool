import json
import os
import re
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from PyQt5 import QtCore

from app_paths import APP_ROOT
from model.GoogleDriveHelper import (
    load_drive_service,
    print_person_folder_links,
    read_drive_parent_folder_id,
    upload_local_dirs_to_drive_batch,
    upload_routed_changed_files_to_drive_batch,
)
from model.GoogleSheetsHelper import write_review_video_links
from model.OdsHelper import normalize_subcategory_path
from model.TaskResultExporter import export_one_date
from model.TaskSubmissionHelper import write_task_submission_links
from model.VideoCompressor import compress_video
from model import VideoElementDetector as video_element_detector
from model.VideoElementDetector import (
    detect_video_element,
    is_positive_detection,
    summarize_detection,
)


VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
LEGACY_TASK_RESULT_CONFIG = APP_ROOT / "model" / "task_result_organizer" / "defaults.json"
DEFAULT_PENDING_FILE_STATE = APP_ROOT / "TaskResultPendingFiles.json"
PENDING_FILE_STATE_VERSION = 1

TASK_RESULT_DEFAULTS = {
    "run_export": True,
    "run_upload": True,
    "open_result_dir": True,
    "wsp_export": False,
    "upload_only_changed_files": True,
    "enable_video_review_detection": True,
    "video_detection_mode": "ask",
    "review_min_confidence": 0.4,
    "detection_failure_goes_to_review": True,
    "review_folder_name": "review",
    "compress_enabled": True,
    "compress_name_keywords": [],
    "gemini_api_keys": [],
    "gemini_model": "gemini-2.5-flash-lite",
    "drive_parent_folder_id": "",
    "upload_date_override": "",
    "upload_slot_override": "",
    "review_sheet_enabled": True,
    "task_submission_sheet_enabled": True,
}

MIGRATED_FILE_NAMES = {
    "review_sheet_credentials_file": "GoogleSheetsCredentials.json",
    "review_sheet_token_file": "GoogleSheetsToken.json",
    "task_submission_sheet_credentials_file": "GoogleSheetsCredentials.json",
    "task_submission_sheet_token_file": "GoogleSheetsToken.json",
    "task_submission_log_file": "TaskSubmissionLog.jsonl",
}


def config_bool(config: Dict[str, Any], name: str, default: bool) -> bool:
    value = config.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "是"}
    return bool(value)


def config_str(config: Dict[str, Any], name: str, default: str = "") -> str:
    value = config.get(name, default)
    return str(value).strip() if value is not None else default


def config_list(config: Dict[str, Any], name: str) -> List[str]:
    value = config.get(name) or []
    if isinstance(value, str):
        return [part for part in re.split(r"[\s,，;；]+", value) if part]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def load_effective_config(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = dict(TASK_RESULT_DEFAULTS)
    if isinstance(overrides, dict):
        config.update(overrides)
    return config


def migrate_legacy_task_result_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """One-time migration from the old copied subproject into the main config."""
    target = Path(config_path or APP_ROOT / "config.json")
    if not target.is_absolute():
        target = APP_ROOT / target

    config = {}
    if target.exists():
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError(f"主配置必须是 JSON 对象：{target}")

    if not LEGACY_TASK_RESULT_CONFIG.exists():
        return config

    legacy = json.loads(LEGACY_TASK_RESULT_CONFIG.read_text(encoding="utf-8-sig"))
    if not isinstance(legacy, dict):
        raise ValueError(f"旧整理配置必须是 JSON 对象：{LEGACY_TASK_RESULT_CONFIG}")

    changed = False
    for key, value in legacy.items():
        value = MIGRATED_FILE_NAMES.get(key, value)
        if key not in config:
            config[key] = value
            changed = True

    for key, value in MIGRATED_FILE_NAMES.items():
        if key in config and config[key] != value:
            config[key] = value
            changed = True

    if changed:
        temporary = target.with_name(f"{target.name}.task-result-migration.tmp")
        temporary.write_text(
            json.dumps(config, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        os.replace(temporary, target)
    return config


def parse_task_date(text: str) -> date:
    raw_text = str(text or "").strip()
    if not raw_text:
        raise ValueError("日期不能为空")

    lowered = raw_text.lower()
    if lowered in {"today", "今天"}:
        return date.today()
    if lowered in {"yesterday", "昨天"}:
        return date.today() - timedelta(days=1)

    numbers = [int(part) for part in re.findall(r"\d+", raw_text)]
    if len(numbers) == 1 and len(raw_text) == 4:
        return date(date.today().year, int(raw_text[:2]), int(raw_text[2:]))
    if len(numbers) == 2:
        return date(date.today().year, numbers[0], numbers[1])
    if len(numbers) == 3:
        return date(numbers[0], numbers[1], numbers[2])
    raise ValueError(f"无法识别日期：{text}")


def get_upload_batch(config: Dict, now: Optional[datetime] = None) -> Tuple[date, str]:
    override_date = config_str(config, "upload_date_override")
    override_slot = config_str(config, "upload_slot_override")
    if override_date or override_slot:
        if not override_date or not override_slot:
            raise ValueError("指定上传批次时，日期和批次必须同时填写")
        if override_slot not in {"01", "02", "03"}:
            raise ValueError("上传批次只能是 01、02 或 03")
        return parse_task_date(override_date), override_slot

    current = now or datetime.now()
    if current.hour < 7:
        return current.date() - timedelta(days=1), "03"
    if current.hour < 12:
        return current.date(), "01"
    if current.hour < 18:
        return current.date(), "02"
    return current.date(), "03"


def is_video_file(file_path: Path) -> bool:
    return Path(file_path).suffix.lower() in VIDEO_SUFFIXES


def is_compressed_video(file_path: Path) -> bool:
    return Path(file_path).name.startswith("[SHANA]")


def file_identity(file_path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(Path(file_path))))


def task_for_file(task_by_file: Optional[Dict[str, Any]], file_path: Path):
    if not task_by_file:
        return None
    return task_by_file.get(file_identity(file_path))


def review_override_for_file(
    task_by_file: Optional[Dict[str, Any]],
    file_path: Path,
) -> Optional[bool]:
    task = task_for_file(task_by_file, file_path)
    value = getattr(task, "review_required", None) if task is not None else None
    return value if isinstance(value, bool) else None


def subcategory_path_for_file(
    task_by_file: Optional[Dict[str, Any]],
    file_path: Path,
) -> Path:
    task = task_for_file(task_by_file, file_path)
    if task is None:
        return Path(".")
    normalized = normalize_subcategory_path(getattr(task, "subcategory", ""))
    if not normalized:
        return Path(".")
    return Path(*normalized.split("/"))


def updated_files_from_batches(
    changed_file_batches: Iterable[Tuple[Path, Iterable[Path]]],
) -> List[Path]:
    """Return each updated file once while preserving the export order."""
    updated_files = []
    seen = set()
    for _root_dir, files in changed_file_batches:
        for file_path in files:
            path = Path(file_path)
            key = os.path.normcase(os.path.abspath(str(path)))
            if key in seen:
                continue
            seen.add(key)
            updated_files.append(path)
    return updated_files


def merge_changed_file_batches(
    *batch_groups: Iterable[Tuple[Path, Iterable[Path]]],
) -> List[Tuple[Path, List[Path]]]:
    """Merge batches without losing their original order or duplicating files."""
    merged = []
    batch_indexes = {}
    seen_files = set()
    for batches in batch_groups:
        for root_dir, files in batches:
            root_path = Path(root_dir)
            root_key = file_identity(root_path)
            if root_key not in batch_indexes:
                batch_indexes[root_key] = len(merged)
                merged.append((root_path, []))
            target_files = merged[batch_indexes[root_key]][1]
            for file_path in files:
                path = Path(file_path)
                file_key = file_identity(path)
                if file_key in seen_files:
                    continue
                seen_files.add(file_key)
                target_files.append(path)
    return [(root, files) for root, files in merged if files]


def pending_file_state_path(config: Dict) -> Path:
    configured = config_str(config, "task_result_pending_file")
    path = Path(configured) if configured else DEFAULT_PENDING_FILE_STATE
    if not path.is_absolute():
        path = APP_ROOT / path
    return path


def _pending_scope_key(base_dir: Path, task_dates: Iterable[date]) -> str:
    dates = sorted(
        item.isoformat() if hasattr(item, "isoformat") else str(item)
        for item in task_dates
    )
    return "{}|{}".format(file_identity(base_dir), ",".join(dates))


def _read_pending_file_state(state_path: Path) -> Dict:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError, TypeError) as error:
        print(f"读取待上传文件列表失败，已忽略损坏记录：{error}")
        state = {}
    scopes = state.get("scopes") if isinstance(state, dict) else None
    return {
        "version": PENDING_FILE_STATE_VERSION,
        "scopes": scopes if isinstance(scopes, dict) else {},
    }


def _write_pending_file_state(state_path: Path, state: Dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_name(state_path.name + ".tmp")
    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, state_path)


def load_pending_changed_file_batches(
    config: Dict,
    base_dir: Path,
    task_dates: Iterable[date],
) -> List[Tuple[Path, List[Path]]]:
    state = _read_pending_file_state(pending_file_state_path(config))
    scope = state["scopes"].get(_pending_scope_key(base_dir, task_dates), {})
    batches = []
    for item in scope.get("batches", []) if isinstance(scope, dict) else []:
        if not isinstance(item, dict):
            continue
        root_dir = Path(str(item.get("root_dir") or ""))
        files = [
            Path(str(file_path))
            for file_path in item.get("files", [])
            if str(file_path).strip() and Path(str(file_path)).is_file()
        ]
        if files:
            batches.append((root_dir, files))
    return merge_changed_file_batches(batches)


def save_pending_changed_file_batches(
    config: Dict,
    base_dir: Path,
    task_dates: Iterable[date],
    changed_file_batches: Iterable[Tuple[Path, Iterable[Path]]],
) -> int:
    valid_batches = []
    for root_dir, files in merge_changed_file_batches(changed_file_batches):
        valid_files = [Path(file_path) for file_path in files if Path(file_path).is_file()]
        if valid_files:
            valid_batches.append((Path(root_dir), valid_files))

    state_path = pending_file_state_path(config)
    state = _read_pending_file_state(state_path)
    scope_key = _pending_scope_key(base_dir, task_dates)
    if not valid_batches:
        state["scopes"].pop(scope_key, None)
    else:
        state["scopes"][scope_key] = {
            "base_dir": str(Path(base_dir).resolve()),
            "task_dates": [
                item.isoformat() if hasattr(item, "isoformat") else str(item)
                for item in task_dates
            ],
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "batches": [
                {
                    "root_dir": str(root_dir.resolve()),
                    "files": [str(file_path.resolve()) for file_path in files],
                }
                for root_dir, files in valid_batches
            ],
        }
    _write_pending_file_state(state_path, state)
    return len(updated_files_from_batches(valid_batches))


def clear_pending_changed_file_batches(
    config: Dict,
    base_dir: Path,
    task_dates: Iterable[date],
) -> None:
    state_path = pending_file_state_path(config)
    state = _read_pending_file_state(state_path)
    state["scopes"].pop(_pending_scope_key(base_dir, task_dates), None)
    if state["scopes"]:
        _write_pending_file_state(state_path, state)
    else:
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass


def resolve_ask_detection_mode(
    config: Dict,
    changed_file_batches: Iterable[Tuple[Path, Iterable[Path]]],
    resolver: Optional[Callable[[List[Path]], str]] = None,
    task_by_file: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve the interactive video-review choice after export has finished."""
    detection_mode = config_str(config, "video_detection_mode", "ai").lower()
    if (
        detection_mode != "ask"
        or not config_bool(config, "enable_video_review_detection", True)
    ):
        return detection_mode

    updated_files = updated_files_from_batches(changed_file_batches)
    has_detectable_video = any(
        is_video_file(file_path)
        and not is_compressed_video(file_path)
        and review_override_for_file(task_by_file, file_path) is not False
        for file_path in updated_files
    )
    if not has_detectable_video:
        return detection_mode

    selected_mode = "ai" if resolver is None else str(resolver(updated_files) or "")
    selected_mode = selected_mode.strip().lower()
    if selected_mode == "cancel":
        return selected_mode
    if selected_mode not in {"ai", "skip", "manual"}:
        raise ValueError(f"不支持的视频检测方式：{selected_mode or '空'}")
    config["video_detection_mode"] = selected_mode
    return selected_mode


def get_upload_person_name(file_path: Path, root_dir: Path, creator_marker: str) -> Optional[str]:
    file_path = Path(file_path)
    root_dir = Path(root_dir)
    try:
        relative_path = file_path.relative_to(root_dir)
    except ValueError:
        relative_path = Path(file_path.name)

    name = relative_path.parts[0] if len(relative_path.parts) > 1 else relative_path.name
    if name.startswith("[SHANA]"):
        name = name[len("[SHANA]") :]

    match = re.match(
        rf"^(?P<person>.+?){re.escape(creator_marker)}-\d{{4}}-",
        name,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group("person").strip().strip(". ") or None


def build_routed_batches(
    changed_file_batches: Iterable[Tuple[Path, Iterable[Path]]],
    config: Dict,
    task_by_file: Optional[Dict[str, Any]] = None,
) -> List[Tuple[Path, List[Path], Path]]:
    routed = []
    api_keys = None
    detection_enabled = config_bool(config, "enable_video_review_detection", True)
    detection_mode = config_str(config, "video_detection_mode", "ai").lower()
    if detection_mode == "ask":
        # The main window resolves "ask" before starting the worker. This fallback
        # keeps programmatic callers deterministic.
        detection_mode = "ai"
    review_folder_name = config_str(config, "review_folder_name", "review") or "review"
    min_confidence = float(config.get("review_min_confidence", 0.4) or 0.4)
    failure_goes_to_review = config_bool(config, "detection_failure_goes_to_review", True)
    creator_marker = config_str(config, "video_filename_creator_marker")
    if not creator_marker:
        raise ValueError("未配置 video_filename_creator_marker，无法按文件名分流")

    for root_dir, changed_files in changed_file_batches:
        root_dir = Path(root_dir)
        normal_files = []
        review_files = []

        for file_path in changed_files:
            file_path = Path(file_path)
            review_override = review_override_for_file(task_by_file, file_path)
            is_detectable_video = (
                is_video_file(file_path) and not is_compressed_video(file_path)
            )
            if is_detectable_video and review_override is False:
                normal_files.append(file_path)
                print(f"本地任务设置为无需审核，已跳过检测：{file_path.name}")
                continue

            effective_mode = detection_mode
            if is_detectable_video and review_override is True:
                if not detection_enabled or detection_mode == "skip":
                    effective_mode = "manual"
                    print(f"本地任务强制审核，已改为人工审核：{file_path.name}")
                else:
                    print(f"本地任务要求审核，按本轮方式处理：{file_path.name}")

            should_detect = is_detectable_video and (
                review_override is True
                or (detection_enabled and effective_mode != "skip")
            )
            if should_detect:
                print(f"\n开始检测视频元素：{file_path.name}")
                try:
                    if effective_mode == "ai" and api_keys is None:
                        configured_keys = config_list(config, "gemini_api_keys")
                        if not configured_keys:
                            raise RuntimeError("没有可用的 Gemini API Key，请在程序设置中添加")
                        api_keys = video_element_detector.read_gemini_api_keys(configured_keys)
                    result = detect_video_element(
                        file_path,
                        api_keys=api_keys,
                        detection_mode=effective_mode,
                    )
                    print(f"检测结果：{summarize_detection(result)}")
                    if result.get("skip_upload"):
                        print(f"人工审核标记为视频有问题，本轮不上传：{file_path.name}")
                        continue
                    if is_positive_detection(result, min_confidence):
                        review_files.append(file_path)
                        print(f"进入人工检查队列：{file_path.name}")
                    else:
                        normal_files.append(file_path)
                except BaseException as error:
                    print(f"视频检测失败：{file_path.name} -> {type(error).__name__}: {error}")
                    if failure_goes_to_review:
                        review_files.append(file_path)
                        print(f"检测失败，按保守策略进入人工检查队列：{file_path.name}")
                    else:
                        normal_files.append(file_path)
            else:
                normal_files.append(file_path)

        normal_file_groups = {}
        for normal_file in normal_files:
            person_name = get_upload_person_name(normal_file, root_dir, creator_marker)
            remote_prefix = Path(person_name) if person_name else Path(".")
            subcategory_path = subcategory_path_for_file(task_by_file, normal_file)
            if person_name and str(subcategory_path) not in {"", "."}:
                remote_prefix = remote_prefix / subcategory_path
            normal_file_groups.setdefault(remote_prefix, []).append(normal_file)

        for remote_prefix, grouped_files in normal_file_groups.items():
            routed.append((root_dir, grouped_files, remote_prefix))
        if review_files:
            routed.append((root_dir, review_files, Path(review_folder_name)))
    return routed


def compress_routed_batches(
    routed_batches: List[Tuple[Path, List[Path], Path]],
    config: Dict,
    task_by_file: Optional[Dict[str, Any]] = None,
    upload_task_by_file: Optional[Dict[str, Any]] = None,
) -> List[Tuple[Path, List[Path], Path]]:
    compressed_batches = []
    for root_dir, file_list, remote_prefix in routed_batches:
        upload_files = []
        for file_path in file_list:
            source_path = Path(file_path)
            upload_path = (
                compress_video(source_path, config)
                if is_video_file(source_path)
                else source_path
            )
            upload_files.append(upload_path)
            task = task_for_file(task_by_file, source_path)
            if task is not None and upload_task_by_file is not None:
                upload_task_by_file[file_identity(upload_path)] = task
        if upload_files:
            compressed_batches.append((root_dir, upload_files, remote_prefix))
    return compressed_batches


def attach_local_task_metadata(
    uploaded_records: Iterable[Dict],
    upload_task_by_file: Optional[Dict[str, Any]],
) -> None:
    for record in uploaded_records:
        local_file = record.get("local_file")
        if not local_file:
            continue
        task = task_for_file(upload_task_by_file, Path(str(local_file)))
        if task is None:
            continue
        # Only an explicit value from the local registration sheet may be sent
        # to the Google sheet's video-type column.  The export profile/default
        # task type is routing information and must never leak into writeback.
        if bool(getattr(task, "task_type_from_table", False)):
            local_task_type = str(getattr(task, "task_type", "") or "").strip()
            if local_task_type:
                record["local_task_type"] = local_task_type
        review_required = getattr(task, "review_required", None)
        if isinstance(review_required, bool):
            record["review_required_override"] = review_required
        source_row = getattr(task, "source_row", None)
        if source_row is not None:
            record["local_task_row"] = source_row


def is_review_upload_record(record: Dict, review_folder_name: str) -> bool:
    review_key = str(review_folder_name or "review").strip().casefold()
    for field_name in ("remote_prefix", "relative_path"):
        parts = [
            part
            for part in re.split(r"[\\/]+", str(record.get(field_name) or ""))
            if part
        ]
        if parts:
            return parts[0].casefold() == review_key
    return False


def run_task_result_organizer(
    task_dates: Iterable[date],
    base_dir: Path,
    config: Optional[Dict] = None,
    detection_mode_resolver: Optional[Callable[[List[Path]], str]] = None,
) -> Dict:
    config = load_effective_config(config)
    video_element_detector.set_runtime_config(config)
    base_dir = Path(base_dir)
    task_dates = list(task_dates)
    if not task_dates:
        raise ValueError("没有要整理的任务日期")
    if not base_dir.is_dir():
        raise ValueError(f"任务路径不是一个目录：{base_dir}")

    run_export = config_bool(config, "run_export", True)
    run_upload = config_bool(config, "run_upload", True)
    only_changed = config_bool(config, "upload_only_changed_files", True)
    wsp_export = config_bool(config, "wsp_export", False)
    review_folder_name = config_str(config, "review_folder_name", "review") or "review"
    result_dirs = []
    changed_file_batches = []
    task_by_file = {}

    def remember_exported_task(file_path, task):
        task_by_file[file_identity(file_path)] = task

    if run_export:
        for task_date in task_dates:
            result = export_one_date(
                task_date,
                base_dir=base_dir,
                wsp_export=wsp_export,
                config=config,
                exported_file_callback=remember_exported_task,
            )
            if result:
                result_dirs.append(result.output_dir)
                if result.updated_files:
                    changed_file_batches.append((result.output_dir, result.updated_files))
    else:
        result_dirs = [base_dir / f"{item.month:02d}{item.day:02d}" / "result" for item in task_dates]

    summary = {
        "result_dirs": [str(path) for path in result_dirs],
        "changed_file_count": sum(len(files) for _, files in changed_file_batches),
        "uploaded_file_count": 0,
        "message": "整理完成",
    }
    if not run_upload:
        summary["message"] = "整理完成，设置中已关闭上传。"
        return summary

    if only_changed:
        pending_batches = load_pending_changed_file_batches(
            config,
            base_dir,
            task_dates,
        )
        pending_count = len(updated_files_from_batches(pending_batches))
        changed_file_batches = merge_changed_file_batches(
            pending_batches,
            changed_file_batches,
        )
        summary["changed_file_count"] = len(
            updated_files_from_batches(changed_file_batches)
        )
        if not changed_file_batches:
            summary["message"] = "没有本次新增或更新的文件，已跳过 Google Drive 上传。"
            return summary
        if pending_count:
            print(f"已恢复上次保留的待处理文件：{pending_count} 个")
        saved_count = save_pending_changed_file_batches(
            config,
            base_dir,
            task_dates,
            changed_file_batches,
        )
        print(f"已保存本轮待处理文件列表：{saved_count} 个")

        selected_mode = resolve_ask_detection_mode(
            config,
            changed_file_batches,
            resolver=detection_mode_resolver,
            task_by_file=task_by_file,
        )
        if selected_mode == "cancel":
            summary["cancelled"] = True
            summary["message"] = (
                f"已取消本次操作，{saved_count} 个待处理文件已保留，"
                "下次点击“整理任务结果”会继续显示。"
            )
            print(summary["message"])
            return summary

    configured_parent = config_str(config, "drive_parent_folder_id")
    if not configured_parent:
        raise ValueError("Google Drive 父目录不能为空，请在程序设置中填写")
    parent_folder_id = read_drive_parent_folder_id(configured_parent)
    service = load_drive_service()
    batch_date, batch_slot = get_upload_batch(config)
    summary["upload_batch"] = f"{batch_date:%m%d}/{batch_slot}"

    if only_changed:
        routed_batches = build_routed_batches(
            changed_file_batches,
            config,
            task_by_file=task_by_file,
        )
        if not routed_batches:
            clear_pending_changed_file_batches(config, base_dir, task_dates)
            summary["message"] = "没有需要上传的文件。"
            return summary
        upload_task_by_file = {}
        routed_batches = compress_routed_batches(
            routed_batches,
            config,
            task_by_file=task_by_file,
            upload_task_by_file=upload_task_by_file,
        )
        uploaded_records = upload_routed_changed_files_to_drive_batch(
            service,
            routed_batches,
            parent_folder_id,
            batch_date=batch_date,
            batch_slot=batch_slot,
        )
        attach_local_task_metadata(uploaded_records, upload_task_by_file)
        summary["uploaded_file_count"] = len(uploaded_records)

        try:
            task_sheet_count = write_task_submission_links(config, uploaded_records)
            print(f"任务提交表格：已填写 {task_sheet_count} 条")
            summary["task_sheet_count"] = task_sheet_count
        except Exception as error:
            print(f"写入任务提交表格失败：{type(error).__name__}: {error}")
            summary["task_sheet_error"] = str(error)

        review_records = [
            record for record in uploaded_records
            if is_review_upload_record(record, review_folder_name)
        ]
        if review_records:
            try:
                summary["review_sheet_count"] = write_review_video_links(config, review_records)
            except Exception as error:
                print(f"写入人工检查表格失败：{type(error).__name__}: {error}")
                summary["review_sheet_error"] = str(error)
        summary["person_folder_links"] = print_person_folder_links(
            uploaded_records,
            review_folder_name,
        )
        clear_pending_changed_file_batches(config, base_dir, task_dates)
    else:
        print("整目录上传模式不会做视频检测分流。")
        upload_local_dirs_to_drive_batch(
            service,
            result_dirs,
            parent_folder_id,
            batch_date=batch_date,
            batch_slot=batch_slot,
        )

    summary["message"] = f"Google Drive 上传完成：{batch_date:%m%d}/{batch_slot}"
    return summary


class _SignalWriter:
    def __init__(self, signal):
        self.signal = signal
        self.buffer = ""

    def write(self, text):
        if not text:
            return 0
        self.buffer += str(text).replace("\r", "\n")
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                self.signal.emit(line)
        return len(text)

    def flush(self):
        if self.buffer.strip():
            self.signal.emit(self.buffer.rstrip())
        self.buffer = ""


class TaskResultOrganizerThread(QtCore.QThread):
    log = QtCore.pyqtSignal(str)
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)
    detection_choice_requested = QtCore.pyqtSignal(object)

    def __init__(
        self,
        task_dates,
        base_dir,
        config,
        parent=None,
        interactive_detection_choice=False,
    ):
        super().__init__(parent)
        self.task_dates = list(task_dates)
        self.base_dir = Path(base_dir)
        self.config = dict(config or {})
        self.interactive_detection_choice = bool(interactive_detection_choice)
        self._detection_choice = "cancel"
        self._detection_choice_event = threading.Event()

    def set_detection_choice(self, detection_mode):
        selected_mode = str(detection_mode or "cancel").strip().lower()
        if selected_mode not in {"ai", "skip", "manual", "cancel"}:
            selected_mode = "cancel"
        self._detection_choice = selected_mode
        self._detection_choice_event.set()

    def _request_detection_choice(self, updated_files):
        self._detection_choice = "cancel"
        self._detection_choice_event.clear()
        self.detection_choice_requested.emit(
            [str(Path(file_path)) for file_path in updated_files]
        )
        while not self._detection_choice_event.wait(0.2):
            if self.isInterruptionRequested():
                return "cancel"
        return self._detection_choice

    def run(self):
        writer = _SignalWriter(self.log)
        try:
            resolver = (
                self._request_detection_choice
                if self.interactive_detection_choice
                else None
            )
            with redirect_stdout(writer), redirect_stderr(writer):
                result = run_task_result_organizer(
                    self.task_dates,
                    self.base_dir,
                    self.config,
                    detection_mode_resolver=resolver,
                )
            writer.flush()
            self.completed.emit(result)
        except BaseException as error:
            writer.flush()
            self.log.emit(traceback.format_exc())
            self.failed.emit(f"{type(error).__name__}: {error}")
