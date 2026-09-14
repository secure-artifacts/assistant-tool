import hashlib
import json
import os
import re
import threading
import unicodedata
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from app_paths import APP_ROOT


VIDEO_UPLOAD_HISTORY_VERSION = 1
VIDEO_UPLOAD_HISTORY_RETENTION_DAYS = 31
DEFAULT_VIDEO_UPLOAD_HISTORY_FILE = APP_ROOT / "VideoUploadHistory.json"
DEFAULT_VIDEO_UPLOAD_ARCHIVE_DIR = APP_ROOT / "VideoUploadHistoryArchives"
DEFAULT_TASK_SUBMISSION_AUDIT_FILE = APP_ROOT / "TaskSubmissionLog.jsonl"
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
_COMPRESSED_PREFIX = re.compile(r"^\s*\[shana\]\s*", re.IGNORECASE)
_LOCK = threading.RLock()


def _config_path(config: Optional[Mapping], key: str, default: Path) -> Path:
    value = str((config or {}).get(key, "") or "").strip()
    return Path(value).expanduser() if value else Path(default)


def video_upload_history_path(config: Optional[Mapping] = None) -> Path:
    return _config_path(
        config,
        "video_upload_history_file",
        DEFAULT_VIDEO_UPLOAD_HISTORY_FILE,
    )


def video_upload_archive_dir(config: Optional[Mapping] = None) -> Path:
    return _config_path(
        config,
        "video_upload_history_archive_dir",
        DEFAULT_VIDEO_UPLOAD_ARCHIVE_DIR,
    )


def _config_bool(config: Optional[Mapping], key: str, default: bool) -> bool:
    value = (config or {}).get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().casefold() not in {"", "0", "false", "no", "off", "否"}


def _moment(value, fallback: Optional[datetime] = None) -> Optional[datetime]:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time())
    else:
        text = str(value or "").strip()
        if not text:
            return fallback
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return fallback
    if result.tzinfo is None:
        timezone = (fallback or datetime.now().astimezone()).astimezone().tzinfo
        result = result.replace(tzinfo=timezone)
    return result


def _iso(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def normalize_video_identity(file_name: str) -> str:
    """Return the conservative identity used for automatic version replacement."""

    name = Path(str(file_name or "").strip()).name
    while _COMPRESSED_PREFIX.match(name):
        name = _COMPRESSED_PREFIX.sub("", name, count=1)
    name = unicodedata.normalize("NFKC", name).casefold()
    return "".join(name.split())


def _drive_link(record: Mapping) -> str:
    return str(record.get("webViewLink") or record.get("webContentLink") or "").strip()


def _is_video_record(record: Mapping) -> bool:
    file_name = str(record.get("name") or "").strip()
    mime_type = str(record.get("mimeType") or "").strip().casefold()
    return Path(file_name).suffix.casefold() in VIDEO_SUFFIXES or mime_type.startswith("video/")


def _task_sheet_result(file_name: str, result_report: Optional[Mapping]) -> Dict:
    report = result_report if isinstance(result_report, Mapping) else {}
    identity = normalize_video_identity(file_name)
    successful = {
        normalize_video_identity(
            item.get("file_name") if isinstance(item, Mapping) else item
        )
        for item in report.get("successful_files", ()) or ()
    }
    failures = {}
    for item in report.get("failed_files", ()) or ():
        if not isinstance(item, Mapping):
            continue
        key = normalize_video_identity(item.get("file_name", ""))
        if key:
            failures[key] = str(item.get("reason") or "").strip()
    if identity in failures:
        return {"status": "failed", "reason": failures[identity]}
    if identity in successful:
        return {"status": "confirmed", "reason": ""}
    if report.get("attempted"):
        return {"status": "not_matched", "reason": "未确认写入任务提交表"}
    return {"status": "not_attempted", "reason": ""}


def _event_id(record: Mapping) -> str:
    values = [
        str(record.get("drive_file_id") or ""),
        str(record.get("md5") or ""),
        str(record.get("drive_modified_at") or ""),
        str(record.get("batch_date") or ""),
        str(record.get("batch_slot") or ""),
        str(record.get("logical_key") or ""),
    ]
    digest = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()
    return f"upload:{digest}"


def _record_from_upload(
    record: Mapping,
    batch_date,
    batch_slot,
    now: datetime,
    task_sheet_report: Optional[Mapping],
) -> Optional[Dict]:
    if not _is_video_record(record):
        return None
    file_name = str(record.get("name") or "").strip()
    file_id = str(record.get("id") or "").strip()
    link = _drive_link(record)
    logical_key = normalize_video_identity(file_name)
    if not file_name or not file_id or not link or not logical_key:
        return None
    item = {
        "recorded_at": _iso(now),
        "source": "upload",
        "logical_key": logical_key,
        "file_name": file_name,
        "drive_file_id": file_id,
        "drive_link": link,
        "drive_action": str(record.get("action") or "").strip(),
        "md5": str(record.get("md5Checksum") or "").strip(),
        "size": str(record.get("size") or "").strip(),
        "mime_type": str(record.get("mimeType") or "").strip(),
        "duration_millis": record.get("local_video_duration_millis"),
        "drive_modified_at": str(record.get("modifiedTime") or "").strip(),
        "batch_date": (
            batch_date.isoformat()
            if isinstance(batch_date, date)
            else str(batch_date or "").strip()
        ),
        "batch_slot": str(batch_slot or "").strip(),
        "local_file": str(record.get("local_file") or "").strip(),
        "relative_path": str(record.get("relative_path") or "").strip(),
        "remote_prefix": str(record.get("remote_prefix") or "").strip(),
        "remote_parent_id": str(record.get("remote_prefix_folder_id") or "").strip(),
        "task": {
            "date": str(record.get("local_task_date") or "").strip(),
            "id": str(record.get("local_task_id") or "").strip(),
            "name": str(record.get("local_task_name") or "").strip(),
            "admin": str(record.get("local_admin") or "").strip(),
            "row": record.get("local_task_row"),
            "type": str(record.get("local_task_type") or "").strip(),
            "is_oral": bool(record.get("local_is_oral")),
        },
        "task_submission": _task_sheet_result(file_name, task_sheet_report),
        "replacement": {
            "state": "current",
            "replaces_file_ids": [],
            "cleanup_errors": [],
        },
    }
    item["event_id"] = _event_id(item)
    return item


def _empty_state() -> Dict:
    return {
        "version": VIDEO_UPLOAD_HISTORY_VERSION,
        "updated_at": "",
        "audit_migration_complete": False,
        "records": [],
    }


def load_video_upload_history(config: Optional[Mapping] = None) -> Dict:
    path = video_upload_history_path(config)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return _empty_state()
    if not isinstance(raw, Mapping):
        return _empty_state()
    state = _empty_state()
    state["updated_at"] = str(raw.get("updated_at") or "").strip()
    state["audit_migration_complete"] = bool(raw.get("audit_migration_complete"))
    records = raw.get("records", [])
    if isinstance(records, list):
        state["records"] = [dict(item) for item in records if isinstance(item, Mapping)]
    return state


def _save_state(path: Path, state: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def _archive_path(archive_dir: Path, month: str) -> Path:
    return archive_dir / f"VideoUploadHistory-{month}.zip"


def _archive_member(month: str) -> str:
    return f"VideoUploadHistory-{month}.json"


def _read_archive(path: Path, month: str) -> List[Dict]:
    if not path.exists():
        return []
    try:
        with zipfile.ZipFile(path, "r") as archive:
            raw = json.loads(archive.read(_archive_member(month)).decode("utf-8"))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"视频上传历史归档损坏，已停止覆盖：{path}") from exc
    records = raw.get("records", []) if isinstance(raw, Mapping) else []
    return [dict(item) for item in records if isinstance(item, Mapping)]


def _merge_records(existing: Iterable[Mapping], incoming: Iterable[Mapping]) -> List[Dict]:
    merged = {}
    order = []
    for raw in list(existing) + list(incoming):
        item = dict(raw)
        key = str(item.get("event_id") or "").strip()
        if not key:
            continue
        if key not in merged:
            order.append(key)
            merged[key] = item
        else:
            merged[key].update(item)
    return [merged[key] for key in order]


def _write_archive(archive_dir: Path, month: str, records: Iterable[Mapping]) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = _archive_path(archive_dir, month)
    merged = _merge_records(_read_archive(path, month), records)
    payload = {
        "version": VIDEO_UPLOAD_HISTORY_VERSION,
        "month": month,
        "records": merged,
    }
    temp_path = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(
        temp_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        archive.writestr(
            _archive_member(month),
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
    os.replace(temp_path, path)
    return path


def _archive_expired_records(
    state: Dict,
    archive_dir: Path,
    now: datetime,
    retention_days: int,
) -> List[Path]:
    cutoff = now - timedelta(days=max(1, retention_days))
    active = []
    by_month = {}
    for record in state.get("records", []):
        recorded_at = _moment(record.get("recorded_at"), fallback=now)
        if recorded_at is None or recorded_at >= cutoff:
            active.append(record)
            continue
        month = recorded_at.strftime("%Y-%m")
        by_month.setdefault(month, []).append(record)
    archived_paths = [
        _write_archive(archive_dir, month, records)
        for month, records in sorted(by_month.items())
    ]
    state["records"] = active
    return archived_paths


def _audit_status(status: str) -> str:
    status = str(status or "").strip().casefold()
    if status == "success":
        return "confirmed"
    if status in {"write_failed", "written_but_verification_mismatch"}:
        return "failed"
    if status == "write_succeeded_verification_failed":
        return "unverified"
    return "pending"


def _migrate_task_submission_audit(
    state: Dict,
    audit_path: Path,
    now: datetime,
) -> int:
    if state.get("audit_migration_complete"):
        return 0
    migrated = {}
    try:
        handle = audit_path.open("r", encoding="utf-8")
    except (FileNotFoundError, OSError):
        state["audit_migration_complete"] = True
        return 0
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, Mapping):
                continue
            drive = event.get("drive")
            if not isinstance(drive, Mapping):
                continue
            file_name = str(event.get("file_name") or "").strip()
            file_id = str(drive.get("file_id") or "").strip()
            link = str(drive.get("link") or "").strip()
            logical_key = normalize_video_identity(file_name)
            if not file_name or not file_id or not link or not logical_key:
                continue
            run_id = str(event.get("run_id") or "legacy").strip()
            key = f"audit:{run_id}:{file_id}"
            recorded_at = _moment(event.get("logged_at"), fallback=now) or now
            item = migrated.get(key, {
                "event_id": key,
                "recorded_at": _iso(recorded_at),
                "source": "task_submission_audit_migration",
                "logical_key": logical_key,
                "file_name": file_name,
                "drive_file_id": file_id,
                "drive_link": link,
                "drive_action": str(drive.get("action") or "").strip(),
                "md5": "",
                "size": "",
                "mime_type": "video/unknown",
                "duration_millis": drive.get("duration_millis"),
                "drive_modified_at": "",
                "batch_date": recorded_at.date().isoformat(),
                "batch_slot": "",
                "local_file": str(drive.get("local_file") or "").strip(),
                "relative_path": str(drive.get("relative_path") or "").strip(),
                "remote_prefix": str(drive.get("remote_prefix") or "").strip(),
                "remote_parent_id": "",
                "task": {
                    "date": str((event.get("video_info") or {}).get("task_date") or "").strip(),
                    "id": str((event.get("video_info") or {}).get("task_id") or "").strip(),
                    "name": str((event.get("video_info") or {}).get("title") or "").strip(),
                    "admin": str(event.get("requester") or "").strip(),
                    "row": event.get("row"),
                    "type": str((event.get("planned_after") or {}).get("video_type") or "").strip(),
                    "is_oral": str(event.get("match_reason") or "").startswith("口播"),
                },
                "task_submission": {"status": "pending", "reason": ""},
                "replacement": {
                    "state": "current",
                    "replaces_file_ids": [],
                    "cleanup_errors": [],
                },
            })
            item["task_submission"] = {
                "status": _audit_status(event.get("status", "")),
                "reason": str(event.get("error") or "").strip(),
                "sheet_name": str(event.get("sheet_name") or "").strip(),
                "row": event.get("row"),
            }
            migrated[key] = item
    state["records"] = _merge_records(state.get("records", []), migrated.values())
    state["audit_migration_complete"] = True
    return len(migrated)


def _all_archive_records(archive_dir: Path) -> List[Dict]:
    records = []
    if not archive_dir.is_dir():
        return records
    for path in sorted(archive_dir.glob("VideoUploadHistory-????-??.zip")):
        month = path.stem.rsplit("-", 2)[-2] + "-" + path.stem.rsplit("-", 1)[-1]
        records.extend(_read_archive(path, month))
    return records


def all_video_upload_records(
    config: Optional[Mapping] = None,
    now: Optional[datetime] = None,
) -> List[Dict]:
    """Return active and archived uploads, migrating the legacy audit if needed.

    The task-submission self-check needs the complete ledger rather than only
    the most recent month.  Keeping this access here also prevents UI code
    from depending on the ZIP archive layout.
    """

    moment = _moment(now) or datetime.now().astimezone()
    history_path = video_upload_history_path(config)
    archive_dir = video_upload_archive_dir(config)
    retention_days = int(
        (config or {}).get(
            "video_upload_history_retention_days",
            VIDEO_UPLOAD_HISTORY_RETENTION_DAYS,
        )
        or VIDEO_UPLOAD_HISTORY_RETENTION_DAYS
    )
    with _LOCK:
        state = load_video_upload_history(config)
        audit_path = _config_path(
            config,
            "task_submission_log_file",
            DEFAULT_TASK_SUBMISSION_AUDIT_FILE,
        )
        migrated = _migrate_task_submission_audit(state, audit_path, moment)
        archived_paths = _archive_expired_records(
            state,
            archive_dir,
            moment,
            retention_days,
        )
        if migrated or archived_paths:
            state["updated_at"] = _iso(moment)
            _save_state(history_path, state)
        return list(state.get("records", [])) + _all_archive_records(archive_dir)


def _trash_replaced_files(
    state: Dict,
    archive_dir: Path,
    new_records: List[Dict],
    drive_service,
    now: datetime,
) -> Dict:
    result = {"trashed": [], "errors": []}
    if drive_service is None or not new_records:
        return result
    prior_records = list(state.get("records", [])) + _all_archive_records(archive_dir)
    current_ids = {
        str(item.get("drive_file_id") or "").strip() for item in new_records
    }
    already_replaced = {
        str(file_id)
        for item in state.get("records", [])
        for file_id in (item.get("replacement") or {}).get("replaces_file_ids", [])
    }
    for new_record in new_records:
        new_id = str(new_record.get("drive_file_id") or "").strip()
        logical_key = str(new_record.get("logical_key") or "").strip()
        if (new_record.get("task_submission") or {}).get("status") != "confirmed":
            new_record["replacement"]["state"] = "cleanup_deferred"
            continue
        candidates = {}
        for old in prior_records:
            old_id = str(old.get("drive_file_id") or "").strip()
            if (
                not old_id
                or old_id == new_id
                or old_id in current_ids
                or old_id in already_replaced
                or str(old.get("logical_key") or "") != logical_key
            ):
                continue
            candidates[old_id] = old
        for old_id, old_record in candidates.items():
            try:
                drive_service.files().update(
                    fileId=old_id,
                    body={"trashed": True},
                    fields="id,trashed",
                    supportsAllDrives=True,
                ).execute()
            except Exception as exc:
                error = {
                    "file_id": old_id,
                    "file_name": str(old_record.get("file_name") or ""),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                new_record["replacement"]["cleanup_errors"].append(error)
                result["errors"].append(error)
                continue
            new_record["replacement"]["replaces_file_ids"].append(old_id)
            result["trashed"].append(old_id)
            already_replaced.add(old_id)
            for active_record in state.get("records", []):
                if str(active_record.get("drive_file_id") or "") == old_id:
                    active_record.setdefault("replacement", {})["state"] = "replaced"
                    active_record["replacement"]["replaced_at"] = _iso(now)
                    active_record["replacement"]["replaced_by_file_id"] = new_id
    return result


def record_video_uploads(
    config: Optional[Mapping],
    uploaded_records: Iterable[Mapping],
    batch_date,
    batch_slot,
    task_sheet_report: Optional[Mapping] = None,
    drive_service=None,
    now: Optional[datetime] = None,
) -> Dict:
    """Persist uploaded video links and safely retire exact-name older versions."""

    if not _config_bool(config, "video_upload_history_enabled", True):
        return {"saved": 0, "migrated": 0, "archived": [], "trashed": [], "errors": []}
    moment = _moment(now) or datetime.now().astimezone()
    history_path = video_upload_history_path(config)
    archive_dir = video_upload_archive_dir(config)
    retention_days = int(
        (config or {}).get(
            "video_upload_history_retention_days",
            VIDEO_UPLOAD_HISTORY_RETENTION_DAYS,
        )
        or VIDEO_UPLOAD_HISTORY_RETENTION_DAYS
    )
    with _LOCK:
        state = load_video_upload_history(config)
        audit_path = _config_path(
            config,
            "task_submission_log_file",
            DEFAULT_TASK_SUBMISSION_AUDIT_FILE,
        )
        migrated = _migrate_task_submission_audit(state, audit_path, moment)
        archived_paths = _archive_expired_records(
            state,
            archive_dir,
            moment,
            retention_days,
        )
        new_records = []
        for raw_record in uploaded_records or ():
            if not isinstance(raw_record, Mapping):
                continue
            item = _record_from_upload(
                raw_record,
                batch_date,
                batch_slot,
                moment,
                task_sheet_report,
            )
            if item is not None:
                new_records.append(item)
        before_ids = {
            str(item.get("event_id") or "") for item in state.get("records", [])
        }
        state["records"] = _merge_records(state.get("records", []), new_records)
        new_event_ids = {
            str(item.get("event_id") or "") for item in new_records
        }
        stored_new_records = [
            item
            for item in state.get("records", [])
            if str(item.get("event_id") or "") in new_event_ids
        ]
        state["updated_at"] = _iso(moment)
        # Save the newly uploaded replacement before touching an older Drive file.
        _save_state(history_path, state)
        replacement_result = {"trashed": [], "errors": []}
        if _config_bool(config, "video_upload_replace_old_enabled", True):
            replacement_result = _trash_replaced_files(
                state,
                archive_dir,
                stored_new_records,
                drive_service,
                moment,
            )
            state["updated_at"] = _iso(moment)
            _save_state(history_path, state)
        saved = sum(
            1 for item in new_records if str(item.get("event_id") or "") not in before_ids
        )
        return {
            "saved": saved,
            "migrated": migrated,
            "archived": [str(path) for path in archived_paths],
            "trashed": replacement_result["trashed"],
            "errors": replacement_result["errors"],
            "history_file": str(history_path),
        }
