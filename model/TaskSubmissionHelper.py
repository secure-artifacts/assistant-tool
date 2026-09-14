import hashlib
import json
import os
import re
import unicodedata
import uuid
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app_paths import APP_ROOT
from model.GoogleDriveDownloader import DownloadError, parse_drive_link
from model.GoogleDriveHelper import load_drive_service
from model.GoogleSheetsHelper import (
    config_bool,
    config_str,
    drive_link_from_record,
    extract_link_text,
    extract_sheet_gid,
    extract_spreadsheet_id,
    get_sheet_info,
    load_sheets_service,
    make_hyperlink_formula,
    sheet_range,
)
from model.TaskTableSchema import (
    SUBMISSION_FIELD_ORDER,
    SUBMISSION_REQUIRED_FIELDS,
    load_task_table_schema,
    normalize_header as normalize_schema_header,
    normalize_task_table_schema,
    submission_field_label,
)


# Real spreadsheet targets belong in the ignored local configuration.
DEFAULT_TASK_SHEET_URL = ""
DEFAULT_AUDIT_LOG_FILE = APP_ROOT / "TaskSubmissionLog.jsonl"
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
TASK_TITLE_MAX_LENGTH = 100
ORAL_SHORT_VIDEO_TYPE = "口播视频-Flow【1分钟以内】"
ORAL_LONG_VIDEO_TYPE = "口播视频-Flow【2-3分钟】"
ORAL_SHORT_MAX_DURATION_MILLIS = 60_000
TASK_SHEET_ROW_GROWTH = 100


def column_letter(index: int) -> str:
    if index < 1:
        raise ValueError("Google 表格列号必须大于 0")
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _submission_columns_for_headers(headers: List[str], schema: Dict) -> Dict[str, int]:
    settings = schema["submission_sheet"]
    normalized_headers = [normalize_schema_header(value) for value in headers]
    result = {}
    for field_name in SUBMISSION_FIELD_ORDER:
        for alias in settings["fields"][field_name]["aliases"]:
            alias_key = normalize_schema_header(alias)
            for index, header_key in enumerate(normalized_headers, start=1):
                if alias_key and alias_key == header_key:
                    result[field_name] = index
                    break
            if field_name in result:
                break
    return result


def resolve_task_submission_layout(values: List[List[str]], schema=None) -> Tuple[int, Dict[str, int]]:
    effective_schema = (
        load_task_table_schema()
        if schema is None
        else normalize_task_table_schema(schema)
    )
    settings = effective_schema["submission_sheet"]
    configured_row = int(settings.get("header_row", 0))
    if configured_row:
        if configured_row > len(values):
            raise RuntimeError(
                "Google 任务表指定的表头行 {} 超出读取范围".format(configured_row)
            )
        header_row = configured_row
        columns = _submission_columns_for_headers(values[header_row - 1], effective_schema)
    else:
        header_row = 0
        columns = {}
        scan_rows = min(len(values), int(settings.get("header_scan_rows", 20)))
        for row_number in range(1, scan_rows + 1):
            candidate = _submission_columns_for_headers(values[row_number - 1], effective_schema)
            if len(candidate) > len(columns):
                header_row = row_number
                columns = candidate

    missing = [
        field_name
        for field_name in SUBMISSION_REQUIRED_FIELDS
        if field_name not in columns
    ]
    if not header_row or missing:
        missing_text = "、".join(
            submission_field_label(effective_schema, name) for name in missing
        )
        raise RuntimeError(
            "Google 任务表格结构无法识别{}。请在“程序设置 → 任务表格”中修改“回写 Google 任务表格”的字段别名。".format(
                "，缺少：" + missing_text if missing_text else ""
            )
        )
    return header_row, columns


def task_text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def get_audit_log_path(config: Dict) -> Path:
    path = Path(config_str(config, "task_submission_log_file", str(DEFAULT_AUDIT_LOG_FILE)))
    if not path.is_absolute():
        path = APP_ROOT / path
    return path


def append_audit_events(config: Dict, events: List[Dict]) -> Path:
    log_path = get_audit_log_path(config)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return log_path


def normalize_match_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def task_filename_match_key(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r'[\\/:*?"<>|]', "_", text.replace("\n", "_")).strip()
    return normalize_match_text(text[:TASK_TITLE_MAX_LENGTH])


def record_file_name(record: Dict) -> str:
    name = str(record.get("name") or "").strip()
    if name:
        return name
    return Path(str(record.get("local_file") or "")).name


def task_sheet_failure_entry(record: Dict, reason: str) -> Dict:
    return {
        "file_name": record_file_name(record),
        "reason": str(reason or "").strip(),
    }


def task_sheet_failures_for_records(records: List[Dict], reason: str) -> List[Dict]:
    failures = []
    for record in records:
        file_name = record_file_name(record)
        if Path(file_name).suffix.lower() not in VIDEO_SUFFIXES:
            continue
        failures.append(task_sheet_failure_entry(record, reason))
    return failures


def record_needs_review(record: Dict, review_folder_name: str) -> bool:
    folder_key = normalize_match_text(Path(str(review_folder_name or "review")).name)
    for field in ("relative_path", "remote_prefix"):
        parts = re.split(r"[\\/]+", str(record.get(field) or ""))
        first_part = next((part for part in parts if part), "")
        if first_part and normalize_match_text(first_part) == folder_key:
            return True
    return False


def extract_product_file_name(product_link: str) -> str:
    text = str(product_link or "").strip()
    match = re.fullmatch(
        r'=HYPERLINK\("(?:[^"]|"")*"\s*[,;]\s*"((?:[^"]|"")*)"\s*\)',
        text,
        flags=re.IGNORECASE,
    )
    if match:
        display_name = match.group(1).replace('""', '"').strip()
        if re.match(r"^https?://", display_name, flags=re.IGNORECASE):
            return ""
        return display_name
    if re.match(r"^https?://", text, flags=re.IGNORECASE):
        return ""
    return text


def populate_link_only_product_file_names(
    rows: List[Dict],
    drive_service=None,
) -> int:
    """Resolve Drive filenames for rows whose result cell contains only a URL."""

    candidates = []
    for row in rows:
        product_link = str(row.get("product_link") or "").strip()
        if not product_link or extract_product_file_name(product_link):
            continue
        link = extract_link_text(product_link)
        try:
            drive_link = parse_drive_link(link)
        except (DownloadError, TypeError, ValueError):
            continue
        candidates.append((row, drive_link.file_id))

    if not candidates:
        return 0

    service = drive_service or load_drive_service()
    names_by_id = {}
    resolved_count = 0
    for row, file_id in candidates:
        if file_id not in names_by_id:
            try:
                metadata = service.files().get(
                    fileId=file_id,
                    fields="id,name,mimeType",
                    supportsAllDrives=True,
                ).execute()
                names_by_id[file_id] = str(metadata.get("name") or "").strip()
            except Exception as error:
                names_by_id[file_id] = ""
                print(
                    f"口播查重：第 {row['row']} 行无法读取网盘视频名称 -> "
                    f"{type(error).__name__}: {error}"
                )
        file_name = names_by_id[file_id]
        if not file_name:
            continue
        row["product_file_name"] = file_name
        resolved_count += 1
        print(f"口播查重：第 {row['row']} 行网盘视频名称 -> {file_name}")
    return resolved_count


def extract_video_match_info(file_name: str, creator_marker: str) -> Optional[Dict]:
    original_name = Path(str(file_name or "")).name
    name = original_name
    if name.upper().startswith("[SHANA]"):
        name = name[len("[SHANA]") :]

    suffix = Path(name).suffix.lower()
    if suffix not in VIDEO_SUFFIXES:
        return None
    stem = name[: -len(suffix)]

    match = re.match(
        rf"^(?P<requester>.+?){re.escape(creator_marker)}-"
        r"(?P<task_date>\d{4})-(?P<task_id>\d+)-(?P<title>.+)$",
        stem,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    title = match.group("title").strip(" _-.")
    title_keys = [normalize_match_text(title)]
    without_export_index = re.sub(r"-\d+$", "", title).strip(" _-.")
    without_index_key = normalize_match_text(without_export_index)
    if without_index_key and without_index_key not in title_keys:
        title_keys.append(without_index_key)

    return {
        "file_name": original_name,
        "requester": match.group("requester").strip(),
        "requester_key": normalize_match_text(match.group("requester")),
        "task_date": match.group("task_date"),
        "task_id": match.group("task_id"),
        "title": title,
        "title_keys": [item for item in title_keys if item],
    }


def submission_file_key(file_name: str) -> str:
    """Use the displayed result filename as the stable submission identity."""

    name = unicodedata.normalize("NFKC", Path(str(file_name or "")).name).strip()
    if name.upper().startswith("[SHANA]"):
        name = name[len("[SHANA]") :]
    return name.strip().casefold()


def oral_video_type_for_record(record: Dict) -> Optional[str]:
    raw_duration = record.get("local_video_duration_millis")
    try:
        duration_millis = int(raw_duration)
    except (TypeError, ValueError):
        return None
    if duration_millis < 0:
        return None
    if duration_millis <= ORAL_SHORT_MAX_DURATION_MILLIS:
        return ORAL_SHORT_VIDEO_TYPE
    return ORAL_LONG_VIDEO_TYPE


def completion_date_for_record(record: Dict, fallback: Optional[str] = None) -> str:
    """Return an explicit historical upload date, otherwise today's date.

    Normal uploads do not set the override and keep the existing behavior.
    History-based repairs set it to the original upload batch date so a late
    repair does not make many older videos look as if they were all completed
    today.
    """

    raw_value = str(record.get("task_submission_completed_at") or "").strip()
    if raw_value:
        try:
            return date.fromisoformat(raw_value[:10]).strftime("%Y-%m-%d")
        except ValueError:
            print(f"任务表忽略无效的历史完成日期：{raw_value}")
    return str(fallback or date.today().strftime("%Y-%m-%d"))


def text_match_score(video_title_key: str, task_text_key: str) -> float:
    if not video_title_key or not task_text_key:
        return 0.0
    if video_title_key == task_text_key:
        return 1.0

    shorter = min(len(video_title_key), len(task_text_key))
    if shorter >= 6 and (
        video_title_key.startswith(task_text_key)
        or task_text_key.startswith(video_title_key)
    ):
        return 0.99

    if shorter < 8:
        return 0.0

    full_score = SequenceMatcher(
        None,
        video_title_key,
        task_text_key,
        autojunk=False,
    ).ratio()
    aligned_score = SequenceMatcher(
        None,
        video_title_key[:shorter],
        task_text_key[:shorter],
        autojunk=False,
    ).ratio()
    return max(full_score, aligned_score)


def best_title_score(title_keys: List[str], task_text_key: str) -> float:
    return max((text_match_score(item, task_text_key) for item in title_keys), default=0.0)


def task_row_match_score(video_info: Dict, row: Dict) -> float:
    task_keys = [
        row.get("task_filename_key", ""),
        row.get("task_text_key", ""),
    ]
    return max(
        (
            best_title_score(video_info["title_keys"], task_key)
            for task_key in task_keys
            if task_key
        ),
        default=0.0,
    )


def read_task_sheet_rows(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    schema=None,
    include_incomplete: bool = False,
) -> Tuple[int, List[Dict], Dict[str, int]]:
    effective_schema = (
        load_task_table_schema()
        if schema is None
        else normalize_task_table_schema(schema)
    )
    # Read one consistent snapshot of the worksheet's used range.  Do not put a
    # fixed right edge such as ZZ in the A1 range: Google rejects that request
    # when the worksheet grid has fewer columns.  A quoted sheet name by itself
    # lets the API return the used range and remains valid when columns move,
    # are added, or are removed.
    safe_sheet_name = sheet_name.replace("'", "''")
    table_response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range="'{}'".format(safe_sheet_name),
        valueRenderOption="FORMULA",
        majorDimension="ROWS",
    ).execute()
    table_values = table_response.get("values", [])
    header_row, column_map = resolve_task_submission_layout(
        table_values,
        effective_schema,
    )

    field_names = list(SUBMISSION_FIELD_ORDER)
    rows = []
    for row_number in range(header_row + 1, len(table_values) + 1):
        row_values = table_values[row_number - 1]
        item = {"row": row_number}
        for field in field_names:
            if field not in column_map:
                item[field] = ""
                continue
            column_index = column_map[field] - 1
            item[field] = (
                str(row_values[column_index])
                if column_index < len(row_values)
                else ""
            )
        item["requester_key"] = normalize_match_text(item["requester"])
        item["task_text_key"] = normalize_match_text(item["chinese"])
        item["task_filename_key"] = task_filename_match_key(item["chinese"])
        if include_incomplete or (
            item["requester_key"] and item["task_text_key"]
        ):
            rows.append(item)
    return header_row, rows, column_map


def ensure_sheet_row_capacity(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    required_row: int,
    growth: int = TASK_SHEET_ROW_GROWTH,
) -> int:
    """Append enough grid rows before writing beyond a worksheet's current limit."""

    required_row = max(0, int(required_row or 0))
    if required_row <= 0:
        return 0

    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,gridProperties(rowCount)))",
    ).execute()
    current_rows = None
    for item in metadata.get("sheets", []):
        properties = item.get("properties", {})
        if int(properties.get("sheetId", -1)) != int(sheet_id):
            continue
        current_rows = int(
            (properties.get("gridProperties") or {}).get("rowCount") or 0
        )
        break
    if current_rows is None:
        raise RuntimeError(f"找不到任务提交工作表：sheetId={sheet_id}")
    if current_rows >= required_row:
        return 0

    added_rows = max(required_row - current_rows, max(1, int(growth or 1)))
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "appendDimension": {
                        "sheetId": int(sheet_id),
                        "dimension": "ROWS",
                        "length": added_rows,
                    }
                }
            ]
        },
    ).execute()
    print(
        f"任务提交表格行数不足：已从 {current_rows} 行扩展到 "
        f"{current_rows + added_rows} 行"
    )
    return added_rows


def find_matching_row(
    video_info: Dict,
    link: str,
    rows_by_requester: Dict[str, List[Dict]],
    creator: str,
    threshold: float,
    margin: float,
    used_rows: set,
) -> Tuple[Optional[Dict], str]:
    requester_rows = rows_by_requester.get(video_info["requester_key"], [])
    if not requester_rows:
        return None, f"找不到任务归属“{video_info['requester']}”"

    exact_name_rows = [
        row
        for row in requester_rows
        if row["row"] not in used_rows
        and extract_product_file_name(row["product_link"]) == video_info["file_name"]
    ]
    if len(exact_name_rows) == 1:
        row = exact_name_rows[0]
        old_link = extract_link_text(row["product_link"])
        if old_link == link:
            return row, f"第 {row['row']} 行已有同名成品，刷新为本次上传链接"
        return row, f"第 {row['row']} 行已有同名成品，覆盖旧链接"
    if len(exact_name_rows) > 1:
        rows_text = "、".join(str(row["row"]) for row in exact_name_rows[:5])
        return None, f"同一任务归属下有多个同名成品（第 {rows_text} 行），拒绝自动覆盖"

    for row in requester_rows:
        old_link = extract_link_text(row["product_link"])
        if old_link and old_link == link:
            return None, f"第 {row['row']} 行已经是这个成品链接"

    creator_key = normalize_match_text(creator)
    candidates = []
    for row in requester_rows:
        if row["row"] in used_rows or str(row["product_link"]).strip():
            continue
        old_creator_key = normalize_match_text(row["creator"])
        if old_creator_key and old_creator_key != creator_key:
            continue
        score = task_row_match_score(video_info, row)
        if score >= threshold:
            candidates.append((score, row))

    if not candidates:
        return None, "同名任务归属下没有找到足够相似的待提交任务"

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_row = candidates[0]
    if len(candidates) > 1 and best_score - candidates[1][0] < margin:
        return None, (
            f"匹配不唯一：第 {best_row['row']} 行与第 {candidates[1][1]['row']} 行"
            f"得分过于接近（{best_score:.3f}/{candidates[1][0]:.3f}）"
        )
    return best_row, f"匹配得分 {best_score:.3f}"


def build_task_sheet_updates(
    config: Dict,
    records: List[Dict],
    rows: List[Dict],
    sheet_name: str,
    column_map: Dict[str, int],
    failed_files: Optional[List[Dict]] = None,
    already_submitted_files: Optional[List[str]] = None,
    first_data_row: int = 1,
) -> Tuple[List[Dict], List[Dict]]:
    creator = config_str(
        config,
        "task_submission_creator",
        config_str(config, "review_sheet_submitter"),
    )
    if not creator:
        raise ValueError("未配置 task_submission_creator，无法填写任务表制作人")

    creator_marker = config_str(
        config,
        "task_submission_creator_marker",
        config_str(config, "video_filename_creator_marker"),
    )
    if not creator_marker:
        raise ValueError("未配置 task_submission_creator_marker，无法解析成品文件名")
    threshold = float(config.get("task_submission_match_threshold", 0.86) or 0.86)
    margin = float(config.get("task_submission_match_margin", 0.05) or 0.05)
    review_status = config_str(config, "task_submission_status_text", "pending") or "pending"
    review_folder_name = config_str(config, "review_folder_name", "review") or "review"

    rows_by_requester = {}
    rows_by_number = {}
    for row in rows:
        rows_by_requester.setdefault(row["requester_key"], []).append(row)
        rows_by_number[int(row["row"])] = row

    submitted_links = {}
    submitted_file_keys = {}
    for row in rows:
        product_link = str(row.get("product_link") or "").strip()
        if not product_link:
            continue
        row_number = int(row["row"])
        existing_link = extract_link_text(product_link)
        if existing_link:
            submitted_links.setdefault(existing_link, row_number)
        existing_file_name = str(row.get("product_file_name") or "").strip()
        existing_file_name = existing_file_name or extract_product_file_name(product_link)
        existing_file_key = submission_file_key(existing_file_name)
        if existing_file_key:
            submitted_file_keys.setdefault(existing_file_key, row_number)

    updates = []
    matched = []
    used_rows = set()
    today_text = date.today().strftime("%Y-%m-%d")
    next_append_row = max(
        [int(row["row"]) for row in rows] + [max(0, int(first_data_row) - 1)]
    ) + 1

    for record in records:
        completion_date_text = completion_date_for_record(record, today_text)
        file_name = record_file_name(record)
        video_info = extract_video_match_info(file_name, creator_marker)
        if not video_info:
            if Path(file_name).suffix.lower() in VIDEO_SUFFIXES:
                reason = f"文件名不符合人员+{creator_marker}-日期-序号格式"
                print(f"任务表跳过：{reason}：{file_name}")
                if failed_files is not None:
                    failed_files.append(task_sheet_failure_entry(record, reason))
            continue

        link = drive_link_from_record(record)
        if not link:
            reason = "上传结果没有访问链接"
            print(f"任务表跳过：{reason}：{file_name}")
            if failed_files is not None:
                failed_files.append(task_sheet_failure_entry(record, reason))
            continue

        if bool(record.get("local_is_oral")):
            # The result-link display name already contains the task date and
            # number.  Treat that visible filename as the primary identity;
            # use the URL only when an older row has no usable display name.
            file_key = submission_file_key(file_name)
            duplicate_row = submitted_file_keys.get(file_key)
            if duplicate_row is None:
                duplicate_row = submitted_links.get(link)
            if duplicate_row is not None:
                existing_row = rows_by_number.get(int(duplicate_row), {})
                existing_link = extract_link_text(existing_row.get("product_link"))
                if existing_link == link:
                    print(
                        f"口播任务表跳过重复：第 {duplicate_row} 行已有记录 <- {file_name}"
                    )
                    if already_submitted_files is not None:
                        already_submitted_files.append(file_name)
                    continue

            oral_video_type = oral_video_type_for_record(record)
            if not oral_video_type:
                reason = "无法读取口播视频时长，不能判断短口播或长口播"
                print(f"任务表未填写：{file_name} -> {reason}")
                if failed_files is not None:
                    failed_files.append(task_sheet_failure_entry(record, reason))
                continue
            if "video_type" not in column_map:
                reason = "任务提交表格没有视频类型列，无法追加口播任务"
                print(f"任务表未填写：{file_name} -> {reason}")
                if failed_files is not None:
                    failed_files.append(task_sheet_failure_entry(record, reason))
                continue

            if duplicate_row is not None:
                row_number = int(duplicate_row)
                product_formula = make_hyperlink_formula(link, file_name)
                needs_review = record_needs_review(record, review_folder_name)
                planned_creator = str(existing_row.get("creator") or "").strip() or creator
                planned_completed_at = (
                    str(existing_row.get("completed_at") or "").strip()
                    or completion_date_text
                )
                planned_review_status = (
                    review_status
                    if needs_review
                    else str(existing_row.get("review_status") or "").strip()
                )
                replacement_values = {
                    "creator": planned_creator,
                    "completed_at": planned_completed_at,
                    "review_status": planned_review_status,
                    "video_type": oral_video_type,
                    "product_link": product_formula,
                }
                for field_name, value in replacement_values.items():
                    if field_name not in column_map:
                        continue
                    updates.append({
                        "range": sheet_range(
                            sheet_name,
                            "{}{}".format(
                                column_letter(column_map[field_name]),
                                row_number,
                            ),
                        ),
                        "values": [[value]],
                    })
                requester = str(existing_row.get("requester") or "").strip()
                requester = requester or str(record.get("local_admin") or "").strip()
                requester = requester or video_info["requester"]
                existing_chinese = str(existing_row.get("chinese") or "").strip()
                matched.append({
                    "operation": "replace_oral",
                    "row": row_number,
                    "file_name": file_name,
                    "requester": requester,
                    "reason": "口播修改版覆盖任务表中的旧网盘链接",
                    "match_score": 1.0,
                    "video_info": {
                        "task_date": video_info["task_date"],
                        "task_id": video_info["task_id"],
                        "title": video_info["title"],
                    },
                    "drive": {
                        "file_id": record.get("id"),
                        "link": link,
                        "previous_link": existing_link,
                        "action": record.get("action"),
                        "local_file": record.get("local_file"),
                        "relative_path": record.get("relative_path"),
                        "remote_prefix": record.get("remote_prefix"),
                        "needs_review": needs_review,
                        "review_required_override": record.get("review_required_override"),
                        "task_type_override": oral_video_type,
                        "duration_millis": record.get("local_video_duration_millis"),
                    },
                    "before": {
                        "task_date": str(existing_row.get("task_date") or ""),
                        "requester": str(existing_row.get("requester") or ""),
                        "chinese": existing_chinese,
                        "chinese_sha256": task_text_hash(existing_chinese),
                        "creator": str(existing_row.get("creator") or ""),
                        "completed_at": str(existing_row.get("completed_at") or ""),
                        "review_status": str(existing_row.get("review_status") or ""),
                        "video_type": str(existing_row.get("video_type") or ""),
                        "product_link": str(existing_row.get("product_link") or ""),
                    },
                    "planned_after": {
                        "task_date": str(existing_row.get("task_date") or ""),
                        "requester": str(existing_row.get("requester") or ""),
                        "chinese": existing_chinese,
                        "creator": planned_creator,
                        "completed_at": planned_completed_at,
                        "review_status": planned_review_status,
                        "video_type": oral_video_type,
                        "product_link": product_formula,
                        "drive_link": link,
                    },
                })
                used_rows.add(row_number)
                submitted_links[link] = row_number
                submitted_file_keys[file_key] = row_number
                print(
                    f"口播任务表替换：第 {row_number} 行 <- {file_name}"
                )
                continue

            row_number = next_append_row
            next_append_row += 1
            product_formula = make_hyperlink_formula(link, file_name)
            needs_review = record_needs_review(record, review_folder_name)
            requester = str(record.get("local_admin") or "").strip()
            requester = requester or video_info["requester"]
            task_text = str(record.get("local_task_name") or "").strip()
            task_text = task_text or video_info["title"]
            task_date_text = str(record.get("local_task_date") or "").strip()
            task_date_text = task_date_text or video_info["task_date"]
            planned_review_status = review_status if needs_review else ""
            append_values = {
                "task_date": task_date_text,
                "requester": requester,
                "chinese": task_text,
                "video_type": oral_video_type,
                "creator": creator,
                "completed_at": completion_date_text,
                "review_status": planned_review_status,
                "product_link": product_formula,
            }
            for field_name, value in append_values.items():
                if field_name not in column_map or value == "":
                    continue
                updates.append({
                    "range": sheet_range(
                        sheet_name,
                        "{}{}".format(
                            column_letter(column_map[field_name]),
                            row_number,
                        ),
                    ),
                    "values": [[value]],
                })

            matched.append({
                "operation": "append_oral",
                "row": row_number,
                "file_name": file_name,
                "requester": requester,
                "reason": "口播任务追加到表格末行",
                "match_score": 1.0,
                "video_info": {
                    "task_date": video_info["task_date"],
                    "task_id": video_info["task_id"],
                    "title": video_info["title"],
                },
                "drive": {
                    "file_id": record.get("id"),
                    "link": link,
                    "action": record.get("action"),
                    "local_file": record.get("local_file"),
                    "relative_path": record.get("relative_path"),
                    "remote_prefix": record.get("remote_prefix"),
                    "needs_review": needs_review,
                    "review_required_override": record.get("review_required_override"),
                    "task_type_override": oral_video_type,
                    "duration_millis": record.get("local_video_duration_millis"),
                },
                "before": {
                    "task_date": "",
                    "requester": "",
                    "chinese": "",
                    "chinese_sha256": task_text_hash(""),
                    "creator": "",
                    "completed_at": "",
                    "review_status": "",
                    "video_type": "",
                    "product_link": "",
                },
                "planned_after": {
                    "task_date": task_date_text,
                    "requester": requester,
                    "chinese": task_text,
                    "creator": creator,
                    "completed_at": completion_date_text,
                    "review_status": planned_review_status,
                    "video_type": oral_video_type,
                    "product_link": product_formula,
                    "drive_link": link,
                },
            })
            used_rows.add(row_number)
            submitted_links[link] = row_number
            submitted_file_keys[file_key] = row_number
            print(
                f"口播任务表追加：第 {row_number} 行 <- {file_name}（{oral_video_type}）"
            )
            continue

        row, reason = find_matching_row(
            video_info,
            link,
            rows_by_requester,
            creator,
            threshold,
            margin,
            used_rows,
        )
        if row is None:
            print(f"任务表未填写：{file_name} -> {reason}")
            if failed_files is not None:
                failed_files.append(task_sheet_failure_entry(record, reason))
            continue

        row_number = row["row"]
        product_formula = make_hyperlink_formula(link, file_name)
        needs_review = record_needs_review(record, review_folder_name)
        planned_creator = str(row["creator"]).strip() or creator
        planned_completed_at = (
            str(row["completed_at"]).strip() or completion_date_text
        )
        planned_review_status = review_status if needs_review else str(row.get("review_status", ""))
        # The Google video-type cell mirrors only the explicit task type from
        # the local registration sheet.  Empty local cells leave Google as-is;
        # defaults, export profiles and upload folders are never used here.
        local_task_type = str(record.get("local_task_type") or "").strip()
        planned_video_type = local_task_type or str(row.get("video_type", "")).strip()
        if not str(row["creator"]).strip():
            updates.append({
                "range": sheet_range(
                    sheet_name,
                    "{}{}".format(column_letter(column_map["creator"]), row_number),
                ),
                "values": [[creator]],
            })
        if not str(row["completed_at"]).strip():
            updates.append({
                "range": sheet_range(
                    sheet_name,
                    "{}{}".format(column_letter(column_map["completed_at"]), row_number),
                ),
                "values": [[completion_date_text]],
            })
        if needs_review:
            updates.append({
                "range": sheet_range(
                    sheet_name,
                    "{}{}".format(column_letter(column_map["review_status"]), row_number),
                ),
                "values": [[review_status]],
            })
        if local_task_type and "video_type" in column_map:
            if str(row.get("video_type", "")).strip() != local_task_type:
                updates.append({
                    "range": sheet_range(
                        sheet_name,
                        "{}{}".format(
                            column_letter(column_map["video_type"]),
                            row_number,
                        ),
                    ),
                    "values": [[local_task_type]],
                })
        elif local_task_type:
            print("任务提交表格没有视频类型列，已跳过本地任务类型回写。")
        updates.append({
            "range": sheet_range(
                sheet_name,
                "{}{}".format(column_letter(column_map["product_link"]), row_number),
            ),
            "values": [[product_formula]],
        })

        used_rows.add(row_number)
        match_score = task_row_match_score(video_info, row)
        matched.append({
            "operation": "update_existing",
            "row": row_number,
            "file_name": file_name,
            "requester": video_info["requester"],
            "reason": reason,
            "match_score": round(match_score, 6),
            "video_info": {
                "task_date": video_info["task_date"],
                "task_id": video_info["task_id"],
                "title": video_info["title"],
            },
            "drive": {
                "file_id": record.get("id"),
                "link": link,
                "action": record.get("action"),
                "local_file": record.get("local_file"),
                "relative_path": record.get("relative_path"),
                "remote_prefix": record.get("remote_prefix"),
                "needs_review": needs_review,
                "review_required_override": record.get("review_required_override"),
                "task_type_override": local_task_type,
            },
            "before": {
                "requester": row["requester"],
                "chinese": row["chinese"],
                "chinese_sha256": task_text_hash(row["chinese"]),
                "creator": row["creator"],
                "completed_at": row["completed_at"],
                "review_status": row.get("review_status", ""),
                "video_type": row.get("video_type", ""),
                "product_link": row["product_link"],
            },
            "planned_after": {
                "task_date": row.get("task_date", ""),
                "requester": row["requester"],
                "chinese": row["chinese"],
                "creator": planned_creator,
                "completed_at": planned_completed_at,
                "review_status": planned_review_status,
                "video_type": planned_video_type,
                "product_link": product_formula,
                "drive_link": link,
            },
        })
        print(f"任务表匹配：第 {row_number} 行 <- {file_name}（{reason}）")

    return updates, matched


def read_matched_rows_after_write(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    matched: List[Dict],
    column_map: Dict[str, int],
) -> Dict[int, Dict]:
    fields = [field for field in SUBMISSION_FIELD_ORDER if field in column_map]
    ranges = []
    range_fields = []
    for item in matched:
        for field in fields:
            column = column_letter(column_map[field])
            ranges.append(sheet_range(sheet_name, f"{column}{item['row']}"))
            range_fields.append((item["row"], field))

    response = service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=ranges,
        valueRenderOption="FORMULA",
        dateTimeRenderOption="FORMATTED_STRING",
        majorDimension="ROWS",
    ).execute()
    value_ranges = response.get("valueRanges", [])
    actual_rows = {item["row"]: {} for item in matched}

    for index, (row_number, field) in enumerate(range_fields):
        value_range = value_ranges[index] if index < len(value_ranges) else {}
        values = value_range.get("values", [])
        actual_rows[row_number][field] = str(values[0][0]) if values and values[0] else ""
    return actual_rows


def make_audit_event(
    run_id: str,
    status: str,
    spreadsheet_id: str,
    sheet_url: str,
    sheet_name: str,
    item: Dict,
    **extra,
) -> Dict:
    event = {
        "logged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_id": run_id,
        "status": status,
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_url": sheet_url,
        "sheet_name": sheet_name,
        "row": item["row"],
        "requester": item["requester"],
        "file_name": item["file_name"],
        "match_reason": item["reason"],
        "match_score": item["match_score"],
        "video_info": item["video_info"],
        "drive": item["drive"],
        "before": item["before"],
        "planned_after": item["planned_after"],
    }
    event.update(extra)
    return event


def write_task_submission_links(
    config: Dict,
    records: List[Dict],
    result_report: Optional[Dict] = None,
    drive_service=None,
) -> int:
    if isinstance(result_report, dict):
        result_report.clear()
        result_report.update({
            "failed_files": [],
            "successful_files": [],
            "attempted": False,
        })
    if not records:
        return 0
    if not config_bool(config, "task_submission_sheet_enabled", True):
        print("配置中已关闭任务提交表格写入。")
        return 0

    sheet_url = config_str(config, "task_submission_sheet_url", DEFAULT_TASK_SHEET_URL)
    spreadsheet_id = extract_spreadsheet_id(sheet_url)
    if not spreadsheet_id:
        print("未配置 task_submission_sheet_url，跳过任务提交表格写入。")
        if isinstance(result_report, dict):
            result_report["attempted"] = True
            result_report["failed_files"] = task_sheet_failures_for_records(
                records,
                "未配置有效的任务提交表格链接",
            )
        return 0

    if isinstance(result_report, dict):
        result_report["attempted"] = True

    service = load_sheets_service(config, "task_submission_sheet")
    gid = extract_sheet_gid(sheet_url)
    sheet_name, sheet_id = get_sheet_info(service, spreadsheet_id, gid)
    task_schema = load_task_table_schema()
    header_row, rows, column_map = read_task_sheet_rows(
        service,
        spreadsheet_id,
        sheet_name,
        schema=task_schema,
        include_incomplete=True,
    )
    if any(bool(record.get("local_is_oral")) for record in records):
        populate_link_only_product_file_names(rows, drive_service=drive_service)
    failed_files = []
    already_submitted_files = []
    updates, matched = build_task_sheet_updates(
        config,
        records,
        rows,
        sheet_name,
        column_map,
        failed_files=failed_files,
        already_submitted_files=already_submitted_files,
        first_data_row=header_row + 1,
    )
    if isinstance(result_report, dict):
        result_report["failed_files"] = failed_files
        result_report["successful_files"] = list(already_submitted_files)

    if not matched:
        if already_submitted_files:
            print(
                f"任务提交表格已有 {len(already_submitted_files)} 条相同任务，"
                "已全部跳过重复写入。"
            )
        else:
            print("任务提交表格没有找到可以安全填写的任务。")
        return 0

    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    prepared_events = [
        make_audit_event(
            run_id,
            "prepared",
            spreadsheet_id,
            sheet_url,
            sheet_name,
            item,
            message=(
                "口播任务准备追加到 Google 表格末行"
                if item.get("operation") == "append_oral"
                else (
                    "口播修改版准备替换 Google 表格中的旧链接"
                    if item.get("operation") == "replace_oral"
                    else "已完成唯一匹配，准备写入 Google 表格"
                )
            ),
        )
        for item in matched
    ]
    log_path = append_audit_events(config, prepared_events)

    try:
        appended_rows = [
            int(item["row"])
            for item in matched
            if item.get("operation") == "append_oral"
        ]
        if appended_rows:
            ensure_sheet_row_capacity(
                service,
                spreadsheet_id,
                sheet_id,
                max(appended_rows),
            )
        write_response = service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": updates,
            },
        ).execute()
    except Exception as exc:
        write_error = f"Google 表格写入失败：{type(exc).__name__}: {exc}"
        if isinstance(result_report, dict):
            result_report["failed_files"].extend(
                {
                    "file_name": item["file_name"],
                    "reason": write_error,
                }
                for item in matched
            )
        failure_events = [
            make_audit_event(
                run_id,
                "write_failed",
                spreadsheet_id,
                sheet_url,
                sheet_name,
                item,
                error=f"{type(exc).__name__}: {exc}",
                message="Google 表格写入失败",
            )
            for item in matched
        ]
        append_audit_events(config, failure_events)
        raise

    response_summary = {
        key: write_response.get(key)
        for key in ("totalUpdatedRows", "totalUpdatedColumns", "totalUpdatedCells", "totalUpdatedSheets")
        if key in write_response
    }

    try:
        actual_rows = read_matched_rows_after_write(
            service,
            spreadsheet_id,
            sheet_name,
            matched,
            column_map,
        )
    except Exception as exc:
        verification_error = f"写入后无法校验：{type(exc).__name__}: {exc}"
        if isinstance(result_report, dict):
            result_report["failed_files"].extend(
                {
                    "file_name": item["file_name"],
                    "reason": verification_error,
                }
                for item in matched
            )
        unverified_events = [
            make_audit_event(
                run_id,
                "write_succeeded_verification_failed",
                spreadsheet_id,
                sheet_url,
                sheet_name,
                item,
                write_response=response_summary,
                verification_error=f"{type(exc).__name__}: {exc}",
                message="Google 表格已写入，但写入后读回校验失败",
            )
            for item in matched
        ]
        append_audit_events(config, unverified_events)
        print(f"任务表已经写入，但读回校验失败：{type(exc).__name__}: {exc}")
    else:
        completed_events = []
        successful_files = list(already_submitted_files)
        verification_failures = []
        for item in matched:
            actual = actual_rows.get(item["row"], {})
            is_append = item.get("operation") == "append_oral"
            expected_requester = (
                item["planned_after"]["requester"]
                if is_append
                else item["before"]["requester"]
            )
            expected_chinese = (
                item["planned_after"]["chinese"]
                if is_append
                else item["before"]["chinese"]
            )
            checks = {
                "requester_correct": (
                    normalize_match_text(actual.get("requester"))
                    == normalize_match_text(expected_requester)
                ),
                "task_text_correct": (
                    task_text_hash(actual.get("chinese"))
                    == task_text_hash(expected_chinese)
                ),
                "task_date_correct": (
                    "task_date" not in column_map
                    or not is_append
                    or normalize_match_text(actual.get("task_date"))
                    == normalize_match_text(item["planned_after"]["task_date"])
                ),
                "creator_correct": (
                    normalize_match_text(actual.get("creator"))
                    == normalize_match_text(item["planned_after"]["creator"])
                ),
                "completed_at_present": bool(str(actual.get("completed_at") or "").strip()),
                "review_status_correct": (
                    str(actual.get("review_status") or "").strip()
                    == item["planned_after"]["review_status"]
                ),
                "video_type_correct": (
                    "video_type" not in column_map
                    or normalize_match_text(actual.get("video_type"))
                    == normalize_match_text(item["planned_after"]["video_type"])
                ),
                "drive_link_correct": (
                    extract_link_text(actual.get("product_link"))
                    == item["planned_after"]["drive_link"]
                ),
            }
            verified = all(checks.values())
            if verified:
                successful_files.append(item["file_name"])
            else:
                failed_checks = [name for name, passed in checks.items() if not passed]
                verification_failures.append({
                    "file_name": item["file_name"],
                    "reason": "写入后校验不一致：" + "、".join(failed_checks),
                })
            completed_events.append(
                make_audit_event(
                    run_id,
                    "success" if verified else "written_but_verification_mismatch",
                    spreadsheet_id,
                    sheet_url,
                    sheet_name,
                    item,
                    actual_after=actual,
                    verification_checks=checks,
                    write_response=response_summary,
                    message="写入成功并校验一致" if verified else "已经写入，但读回内容与预期不完全一致",
                )
            )
        append_audit_events(config, completed_events)
        if isinstance(result_report, dict):
            result_report["successful_files"] = successful_files
            result_report["failed_files"].extend(verification_failures)

    print(f"任务提交表格日志：{log_path}")
    print(f"已填写任务提交表格：{len(matched)} 条")
    return len(matched)
