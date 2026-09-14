"""Cross-check uploaded videos against the task submission spreadsheet."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional

from model.GoogleDriveHelper import load_drive_service
from model.GoogleSheetsHelper import (
    config_str,
    extract_link_text,
    extract_sheet_gid,
    extract_spreadsheet_id,
    get_sheet_info,
    load_sheets_service,
    sheet_range,
)
from model.OralVideoDurationChecker import (
    read_rich_product_links_by_row,
    video_duration_millis,
)
from model.TaskSubmissionHelper import (
    ORAL_LONG_VIDEO_TYPE,
    ORAL_SHORT_MAX_DURATION_MILLIS,
    ORAL_SHORT_VIDEO_TYPE,
    DEFAULT_AUDIT_LOG_FILE,
    column_letter,
    extract_product_file_name,
    populate_link_only_product_file_names,
    read_task_sheet_rows,
    normalize_match_text,
    submission_file_key,
    write_task_submission_links,
)
from model.TaskTableSchema import load_task_table_schema
from model.VideoUploadHistory import all_video_upload_records


UNSET_TYPE_LABEL = "（未设置任务类型）"


def _report(callback: Optional[Callable[[str], None]], message: str) -> None:
    if callback is not None:
        callback(str(message))


def _record_moment(record: Mapping) -> datetime:
    text = str(record.get("recorded_at") or "").strip()
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min
    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def _valid_duration(value) -> Optional[int]:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _is_oral_record(record: Mapping) -> bool:
    task = record.get("task") if isinstance(record.get("task"), Mapping) else {}
    task_type = str(task.get("type") or "").strip()
    return bool(task.get("is_oral")) or task_type in {
        ORAL_SHORT_VIDEO_TYPE,
        ORAL_LONG_VIDEO_TYPE,
    }


def _expected_type(record: Mapping) -> str:
    task = record.get("task") if isinstance(record.get("task"), Mapping) else {}
    duration = _valid_duration(record.get("duration_millis"))
    if _is_oral_record(record) and duration is not None:
        return (
            ORAL_SHORT_VIDEO_TYPE
            if duration <= ORAL_SHORT_MAX_DURATION_MILLIS
            else ORAL_LONG_VIDEO_TYPE
        )
    return str(task.get("type") or "").strip()


def _historical_upload_date(record: Mapping) -> str:
    for raw_value in (record.get("batch_date"), record.get("recorded_at")):
        text = str(raw_value or "").strip()
        if not text:
            continue
        try:
            return datetime.fromisoformat(text[:10]).date().isoformat()
        except ValueError:
            continue
    return ""


def _normalized_date(value) -> str:
    if isinstance(value, (int, float)):
        try:
            return (datetime(1899, 12, 30) + timedelta(days=float(value))).date().isoformat()
        except (OverflowError, ValueError):
            return str(value)
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except ValueError:
        try:
            serial = float(text)
        except ValueError:
            return text
        if 1 <= serial <= 2_958_465:
            try:
                return (
                    datetime(1899, 12, 30) + timedelta(days=serial)
                ).date().isoformat()
            except (OverflowError, ValueError):
                return text
        return text


def _populate_completed_dates(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    rows: List[Dict],
    completed_at_column: int,
) -> None:
    if not rows:
        return
    first_row = min(int(row["row"]) for row in rows)
    last_row = max(int(row["row"]) for row in rows)
    column = column_letter(completed_at_column)
    response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=sheet_range(sheet_name, f"{column}{first_row}:{column}{last_row}"),
        valueRenderOption="UNFORMATTED_VALUE",
        dateTimeRenderOption="FORMATTED_STRING",
        majorDimension="ROWS",
    ).execute()
    values = response.get("values", [])
    by_number = {int(row["row"]): row for row in rows}
    for offset, row_values in enumerate(values):
        row_number = first_row + offset
        row = by_number.get(row_number)
        if row is None:
            continue
        raw_value = row_values[0] if row_values else ""
        row["completed_at"] = _normalized_date(raw_value)


def _self_check_repair_events(config: Mapping) -> Dict[str, Dict]:
    configured_path = str(config.get("task_submission_log_file") or "").strip()
    path = Path(configured_path).expanduser() if configured_path else DEFAULT_AUDIT_LOG_FILE
    latest = {}
    try:
        handle = path.open("r", encoding="utf-8")
    except (FileNotFoundError, OSError):
        return latest
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, Mapping) or str(event.get("status") or "") != "success":
                continue
            drive = event.get("drive") if isinstance(event.get("drive"), Mapping) else {}
            if str(drive.get("action") or "") != "history_self_check_repair":
                continue
            key = submission_file_key(event.get("file_name", ""))
            if not key:
                continue
            latest[key] = {
                "row": event.get("row"),
                "written_date": _normalized_date(
                    (event.get("planned_after") or {}).get("completed_at")
                ),
                "logged_at": str(event.get("logged_at") or ""),
            }
    return latest


def latest_upload_records(records: Iterable[Mapping]) -> List[Dict]:
    """Choose the newest current Drive version for each displayed filename."""

    grouped: Dict[str, List[Dict]] = {}
    for raw in records or ():
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        key = str(item.get("logical_key") or "").strip()
        key = key or submission_file_key(item.get("file_name", ""))
        if not key or not str(item.get("drive_link") or "").strip():
            continue
        grouped.setdefault(key, []).append(item)

    latest = []
    for key, candidates in grouped.items():
        candidates.sort(key=_record_moment, reverse=True)
        current = [
            item
            for item in candidates
            if str((item.get("replacement") or {}).get("state") or "") != "replaced"
        ]
        chosen = dict((current or candidates)[0])
        chosen["logical_key"] = key

        # Older audit events can contain duration/type metadata that predates
        # the main upload ledger.  Carry it forward without changing the link
        # selected from the latest version.
        if _valid_duration(chosen.get("duration_millis")) is None:
            for candidate in candidates:
                duration = _valid_duration(candidate.get("duration_millis"))
                if duration is not None:
                    chosen["duration_millis"] = duration
                    break
        if not _is_oral_record(chosen):
            for candidate in candidates:
                if _is_oral_record(candidate):
                    task = dict(chosen.get("task") or {})
                    task["is_oral"] = True
                    if not str(task.get("type") or "").strip():
                        task["type"] = str(
                            (candidate.get("task") or {}).get("type") or ""
                        ).strip()
                    chosen["task"] = task
                    break
        latest.append(chosen)
    return sorted(latest, key=_record_moment, reverse=True)


def _row_link(row: Mapping) -> str:
    return str(row.get("resolved_product_link") or "").strip() or extract_link_text(
        row.get("product_link")
    )


def _item_from_record(record: Mapping, status: str, row: Optional[Mapping] = None) -> Dict:
    task = record.get("task") if isinstance(record.get("task"), Mapping) else {}
    sheet_row = row if isinstance(row, Mapping) else {}
    expected_type = _expected_type(record)
    actual_type = str(sheet_row.get("video_type") or "").strip()
    return {
        "status": status,
        "file_name": str(record.get("file_name") or "").strip(),
        "drive_link": str(record.get("drive_link") or "").strip(),
        "sheet_link": _row_link(sheet_row),
        "sheet_row": sheet_row.get("row"),
        "task_date": str(task.get("date") or "").strip(),
        "task_id": str(task.get("id") or "").strip(),
        "task_name": str(task.get("name") or "").strip(),
        "admin": str(task.get("admin") or "").strip(),
        "expected_type": expected_type,
        "actual_type": actual_type,
        "expected_completed_at": _historical_upload_date(record),
        "actual_completed_at": _normalized_date(sheet_row.get("completed_at")),
        "filter_type": actual_type or expected_type or UNSET_TYPE_LABEL,
        "is_oral": _is_oral_record(record),
        "history_record": dict(record),
    }


def _blank_type_item_from_sheet(row: Mapping) -> Dict:
    product = str(row.get("product_link") or "").strip()
    file_name = str(row.get("product_file_name") or "").strip()
    file_name = file_name or extract_product_file_name(product)
    return {
        "status": "视频类型为空",
        "file_name": file_name or "（只有链接，未能读取文件名）",
        "drive_link": "",
        "sheet_link": _row_link(row),
        "sheet_row": row.get("row"),
        "task_date": str(row.get("task_date") or "").strip(),
        "task_id": "",
        "task_name": str(row.get("chinese") or "").strip(),
        "admin": str(row.get("requester") or "").strip(),
        "expected_type": "",
        "actual_type": "",
        "filter_type": UNSET_TYPE_LABEL,
        "is_oral": False,
        "history_record": {},
    }


def scan_task_submission_sheet(
    config: Dict,
    progress_callback: Optional[Callable[[str], None]] = None,
    *,
    records: Optional[Iterable[Mapping]] = None,
    sheets_service_factory=None,
    drive_service_factory=None,
    schema=None,
) -> Dict:
    """Find uploaded videos missing from the sheet or missing a video type."""

    sheet_url = config_str(config, "task_submission_sheet_url")
    spreadsheet_id = extract_spreadsheet_id(sheet_url)
    if not spreadsheet_id:
        raise ValueError("未配置有效的任务提交表格链接")

    sheets_factory = sheets_service_factory or load_sheets_service
    drive_factory = drive_service_factory or load_drive_service
    service = sheets_factory(config, "task_submission_sheet")
    sheet_name, sheet_id = get_sheet_info(
        service,
        spreadsheet_id,
        extract_sheet_gid(sheet_url),
    )
    effective_schema = schema or load_task_table_schema()
    _header_row, rows, column_map = read_task_sheet_rows(
        service,
        spreadsheet_id,
        sheet_name,
        schema=effective_schema,
        include_incomplete=True,
    )
    if "product_link" not in column_map:
        raise RuntimeError("任务提交表格没有识别到“成品链接”列")
    if "video_type" not in column_map:
        raise RuntimeError("任务提交表格没有识别到“视频类型”列")
    if "completed_at" in column_map:
        _populate_completed_dates(
            service,
            spreadsheet_id,
            sheet_name,
            rows,
            column_map["completed_at"],
        )

    direct_link_rows = [
        row
        for row in rows
        if str(row.get("product_link") or "").strip()
        and not extract_product_file_name(row.get("product_link"))
    ]
    drive_service = None
    if direct_link_rows:
        drive_service = drive_factory()
        populate_link_only_product_file_names(rows, drive_service=drive_service)

    # Google Sheets may store a displayed filename with a rich-text link rather
    # than a HYPERLINK formula.  The filename is enough for de-duplication, but
    # retaining the hidden URL makes the review window useful too.
    linked_text_rows = [
        row
        for row in rows
        if extract_product_file_name(row.get("product_link"))
        and not extract_link_text(row.get("product_link"))
    ]
    if linked_text_rows:
        try:
            rich_links = read_rich_product_links_by_row(
                service,
                spreadsheet_id,
                sheet_name,
                column_map["product_link"],
                (row["row"] for row in linked_text_rows),
            )
        except Exception as error:
            _report(
                progress_callback,
                f"读取隐藏超链接失败，仍按视频名称检查：{type(error).__name__}: {error}",
            )
        else:
            for row in rows:
                if row["row"] in rich_links:
                    row["resolved_product_link"] = rich_links[row["row"]]

    all_records = list(records) if records is not None else all_video_upload_records(config)
    latest_records = latest_upload_records(all_records)
    rows_by_file = {}
    rows_by_link = {}
    for row in rows:
        product = str(row.get("product_link") or "").strip()
        if not product:
            continue
        file_name = str(row.get("product_file_name") or "").strip()
        file_name = file_name or extract_product_file_name(product)
        file_key = submission_file_key(file_name)
        if file_key:
            rows_by_file.setdefault(file_key, []).append(row)
        link = _row_link(row)
        if link:
            rows_by_link.setdefault(link, []).append(row)

    missing = []
    blank_type = []
    blank_type_rows = set()
    duplicates = []
    wrong_date = []
    present_count = 0
    self_check_events = _self_check_repair_events(config)
    for record in latest_records:
        file_key = submission_file_key(record.get("file_name", ""))
        matching_rows = list(rows_by_file.get(file_key, []))
        if not matching_rows:
            matching_rows = list(rows_by_link.get(str(record.get("drive_link") or ""), []))
        if not matching_rows:
            missing.append(_item_from_record(record, "未填写"))
            continue

        present_count += 1
        if len(matching_rows) > 1:
            duplicates.append(_item_from_record(record, "重复填写", matching_rows[0]))
        blank_rows = [
            row for row in matching_rows if not str(row.get("video_type") or "").strip()
        ]
        if blank_rows:
            blank_type.append(_item_from_record(record, "视频类型为空", blank_rows[0]))
            blank_type_rows.add(int(blank_rows[0]["row"]))
        expected_date = _historical_upload_date(record)
        repair_event = self_check_events.get(file_key, {})
        for matching_row in matching_rows:
            actual_date = _normalized_date(matching_row.get("completed_at"))
            buggy_written_date = str(repair_event.get("written_date") or "")
            if (
                expected_date
                and actual_date
                and actual_date != expected_date
                and buggy_written_date
                and actual_date == buggy_written_date
            ):
                wrong_date.append(
                    _item_from_record(record, "完成日期有误", matching_row)
                )
                break

    # The ledger may not cover very old manual submissions.  Include every
    # filled row belonging to the configured creator whose video type is blank,
    # while avoiding other people's rows in a shared spreadsheet.
    creator_key = normalize_match_text(config_str(config, "task_submission_creator"))
    if creator_key:
        for row in rows:
            if (
                int(row["row"]) in blank_type_rows
                or not str(row.get("product_link") or "").strip()
                or str(row.get("video_type") or "").strip()
                or normalize_match_text(row.get("creator")) != creator_key
            ):
                continue
            blank_type.append(_blank_type_item_from_sheet(row))
            blank_type_rows.add(int(row["row"]))

    type_values = sorted(
        {
            str(item.get("filter_type") or UNSET_TYPE_LABEL)
            for item in missing + blank_type + duplicates + wrong_date
        },
        key=str.casefold,
    )
    _report(
        progress_callback,
        "检查完成：上传历史 {} 个，未填写 {} 个，视频类型为空 {} 个，"
        "历史补填日期有误 {} 个。".format(
            len(latest_records),
            len(missing),
            len(blank_type),
            len(wrong_date),
        ),
    )
    return {
        "sheet_url": sheet_url,
        "sheet_name": sheet_name,
        "sheet_id": sheet_id,
        "history_count": len(latest_records),
        "present_count": present_count,
        "missing": missing,
        "blank_type": blank_type,
        "duplicates": duplicates,
        "wrong_date": wrong_date,
        "type_values": type_values,
    }


def _duration_for_repair(record: Mapping, drive_service) -> Optional[int]:
    duration = _valid_duration(record.get("duration_millis"))
    if duration is not None:
        return duration
    task_type = str((record.get("task") or {}).get("type") or "").strip()
    if task_type == ORAL_SHORT_VIDEO_TYPE:
        return ORAL_SHORT_MAX_DURATION_MILLIS
    if task_type == ORAL_LONG_VIDEO_TYPE:
        return ORAL_SHORT_MAX_DURATION_MILLIS + 1
    file_id = str(record.get("drive_file_id") or "").strip()
    if not file_id:
        return None
    metadata = drive_service.files().get(
        fileId=file_id,
        fields="id,name,videoMediaMetadata(durationMillis)",
        supportsAllDrives=True,
    ).execute()
    return video_duration_millis(metadata)


def submission_record_from_history(record: Mapping, drive_service=None) -> Dict:
    task = record.get("task") if isinstance(record.get("task"), Mapping) else {}
    is_oral = _is_oral_record(record)
    duration = None
    if is_oral:
        duration = _valid_duration(record.get("duration_millis"))
        task_type = str(task.get("type") or "").strip()
        if duration is None and task_type == ORAL_SHORT_VIDEO_TYPE:
            duration = ORAL_SHORT_MAX_DURATION_MILLIS
        elif duration is None and task_type == ORAL_LONG_VIDEO_TYPE:
            duration = ORAL_SHORT_MAX_DURATION_MILLIS + 1
        if duration is None and drive_service is None:
            drive_service = load_drive_service()
        if duration is None:
            duration = _duration_for_repair(record, drive_service)
        if duration is None:
            raise ValueError("无法读取视频时长，不能判断短口播或长口播")
    return {
        "id": str(record.get("drive_file_id") or "").strip(),
        "name": str(record.get("file_name") or "").strip(),
        "mimeType": str(record.get("mime_type") or "video/unknown").strip(),
        "webViewLink": str(record.get("drive_link") or "").strip(),
        "action": "history_self_check_repair",
        "local_file": str(record.get("local_file") or "").strip(),
        "relative_path": str(record.get("relative_path") or "").strip(),
        "remote_prefix": str(record.get("remote_prefix") or "").strip(),
        "local_task_date": str(task.get("date") or "").strip(),
        "local_task_id": str(task.get("id") or "").strip(),
        "local_task_name": str(task.get("name") or "").strip(),
        "local_admin": str(task.get("admin") or "").strip(),
        "local_task_row": task.get("row"),
        "local_task_type": str(task.get("type") or "").strip(),
        "local_is_oral": is_oral,
        "local_video_duration_millis": duration,
        "task_submission_completed_at": _historical_upload_date(record),
    }


def correct_self_check_dates(
    config: Dict,
    items: Iterable[Mapping],
    progress_callback: Optional[Callable[[str], None]] = None,
    *,
    sheets_service_factory=None,
    schema=None,
) -> Dict:
    """Repair only dates previously written by the history self-check bug."""

    selected = [
        dict(item) for item in items or () if item.get("status") == "完成日期有误"
    ]
    if not selected:
        return {"requested_count": 0, "updated_count": 0, "failed_files": []}
    sheet_url = config_str(config, "task_submission_sheet_url")
    spreadsheet_id = extract_spreadsheet_id(sheet_url)
    if not spreadsheet_id:
        raise ValueError("未配置有效的任务提交表格链接")
    service = (sheets_service_factory or load_sheets_service)(
        config,
        "task_submission_sheet",
    )
    sheet_name, _sheet_id = get_sheet_info(
        service,
        spreadsheet_id,
        extract_sheet_gid(sheet_url),
    )
    _header, rows, column_map = read_task_sheet_rows(
        service,
        spreadsheet_id,
        sheet_name,
        schema=schema or load_task_table_schema(),
        include_incomplete=True,
    )
    if "completed_at" not in column_map:
        raise RuntimeError("任务提交表格没有识别到“完成时间”列")
    _populate_completed_dates(
        service,
        spreadsheet_id,
        sheet_name,
        rows,
        column_map["completed_at"],
    )
    rows_by_number = {int(row["row"]): row for row in rows}
    updates = []
    failures = []
    for item in selected:
        file_name = str(item.get("file_name") or "").strip()
        try:
            row_number = int(item.get("sheet_row"))
        except (TypeError, ValueError):
            failures.append({"file_name": file_name, "reason": "缺少表格行号"})
            continue
        row = rows_by_number.get(row_number)
        if row is None:
            failures.append({"file_name": file_name, "reason": "表格行已经不存在"})
            continue
        current_product = str(row.get("product_link") or "").strip()
        current_name = str(row.get("product_file_name") or "").strip()
        current_name = current_name or extract_product_file_name(current_product)
        same_name = submission_file_key(current_name) == submission_file_key(file_name)
        current_link = extract_link_text(current_product)
        expected_links = {
            str(item.get("sheet_link") or "").strip(),
            str(item.get("drive_link") or "").strip(),
        }
        if not same_name and (not current_link or current_link not in expected_links):
            failures.append({"file_name": file_name, "reason": "表格行内容已变化，拒绝覆盖"})
            continue
        if _normalized_date(row.get("completed_at")) != _normalized_date(
            item.get("actual_completed_at")
        ):
            failures.append({"file_name": file_name, "reason": "完成日期已被修改，拒绝覆盖"})
            continue
        expected_date = _normalized_date(item.get("expected_completed_at"))
        if not expected_date:
            failures.append({"file_name": file_name, "reason": "上传历史缺少日期"})
            continue
        updates.append({
            "range": sheet_range(
                sheet_name,
                f'{column_letter(column_map["completed_at"])}{row_number}',
            ),
            "values": [[expected_date]],
        })
    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()
    _report(
        progress_callback,
        f"已修正 {len(updates)} 个历史补填日期，跳过 {len(failures)} 个。",
    )
    return {
        "requested_count": len(selected),
        "updated_count": len(updates),
        "failed_files": failures,
    }


def repair_missing_submissions(
    config: Dict,
    items: Iterable[Mapping],
    progress_callback: Optional[Callable[[str], None]] = None,
    *,
    drive_service_factory=None,
    writer=None,
) -> Dict:
    """Write only the missing rows selected in the self-check window."""

    selected = [dict(item) for item in items or () if item.get("status") == "未填写"]
    drive_service = None
    records = []
    preparation_failures = []
    for item in selected:
        history_record = item.get("history_record") or {}
        try:
            needs_drive_duration = (
                _is_oral_record(history_record)
                and _valid_duration(history_record.get("duration_millis")) is None
                and str((history_record.get("task") or {}).get("type") or "").strip()
                not in {ORAL_SHORT_VIDEO_TYPE, ORAL_LONG_VIDEO_TYPE}
            )
            if needs_drive_duration and drive_service is None:
                drive_service = (drive_service_factory or load_drive_service)()
            records.append(submission_record_from_history(history_record, drive_service))
        except Exception as error:
            preparation_failures.append({
                "file_name": str(item.get("file_name") or ""),
                "reason": f"{type(error).__name__}: {error}",
            })

    report = {}
    write_count = 0
    if records:
        _report(progress_callback, f"正在补填 {len(records)} 个缺失视频……")
        write_count = (writer or write_task_submission_links)(
            config,
            records,
            result_report=report,
            drive_service=drive_service,
        )
    else:
        report = {"attempted": False, "successful_files": [], "failed_files": []}
    report.setdefault("failed_files", []).extend(preparation_failures)
    _report(
        progress_callback,
        "补填完成：写入 {} 个，失败 {} 个。".format(
            write_count,
            len(report.get("failed_files", [])),
        ),
    )
    return {
        "requested_count": len(selected),
        "prepared_count": len(records),
        "write_count": write_count,
        "report": report,
    }


def repair_submission_items(
    config: Dict,
    items: Iterable[Mapping],
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict:
    selected = list(items or ())
    missing_result = repair_missing_submissions(
        config,
        selected,
        progress_callback=progress_callback,
    )
    date_result = correct_self_check_dates(
        config,
        selected,
        progress_callback=progress_callback,
    )
    return {
        "requested_count": len(selected),
        "write_count": missing_result.get("write_count", 0),
        "date_updated_count": date_result.get("updated_count", 0),
        "report": missing_result.get("report", {}),
        "date_failures": date_result.get("failed_files", []),
    }
