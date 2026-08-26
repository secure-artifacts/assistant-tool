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
        return match.group(1).replace('""', '"').strip()
    if re.match(r"^https?://", text, flags=re.IGNORECASE):
        return ""
    return text


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
) -> Tuple[int, List[Dict], Dict[str, int]]:
    effective_schema = (
        load_task_table_schema()
        if schema is None
        else normalize_task_table_schema(schema)
    )
    # Read a single, consistent table snapshot starting at column A.  Google
    # Sheets safely clips the right edge to the sheet grid.  Requesting each
    # previously detected column separately can fail after columns are removed
    # (for example, asking for Q:Q after a sheet is reduced to A:P).
    table_response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=sheet_range(sheet_name, "A:ZZ"),
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
        if item["requester_key"] and item["task_text_key"]:
            rows.append(item)
    return header_row, rows, column_map


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
    for row in rows:
        rows_by_requester.setdefault(row["requester_key"], []).append(row)

    updates = []
    matched = []
    used_rows = set()
    today_text = date.today().strftime("%Y-%m-%d")

    for record in records:
        file_name = record_file_name(record)
        video_info = extract_video_match_info(file_name, creator_marker)
        if not video_info:
            if Path(file_name).suffix.lower() in VIDEO_SUFFIXES:
                print(f"任务表跳过：文件名不符合人员+{creator_marker}-日期-序号格式：{file_name}")
            continue

        link = drive_link_from_record(record)
        if not link:
            print(f"任务表跳过：上传结果没有访问链接：{file_name}")
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
            continue

        row_number = row["row"]
        product_formula = make_hyperlink_formula(link, file_name)
        needs_review = record_needs_review(record, review_folder_name)
        planned_creator = str(row["creator"]).strip() or creator
        planned_completed_at = str(row["completed_at"]).strip() or today_text
        planned_review_status = review_status if needs_review else str(row.get("review_status", ""))
        local_video_type = str(record.get("task_type_override") or "").strip()
        planned_video_type = local_video_type or str(row.get("video_type", "")).strip()
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
                "values": [[today_text]],
            })
        if needs_review:
            updates.append({
                "range": sheet_range(
                    sheet_name,
                    "{}{}".format(column_letter(column_map["review_status"]), row_number),
                ),
                "values": [[review_status]],
            })
        if local_video_type and "video_type" in column_map:
            if str(row.get("video_type", "")).strip() != local_video_type:
                updates.append({
                    "range": sheet_range(
                        sheet_name,
                        "{}{}".format(
                            column_letter(column_map["video_type"]),
                            row_number,
                        ),
                    ),
                    "values": [[local_video_type]],
                })
        elif local_video_type:
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
                "task_type_override": local_video_type,
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


def write_task_submission_links(config: Dict, records: List[Dict]) -> int:
    if not records:
        return 0
    if not config_bool(config, "task_submission_sheet_enabled", True):
        print("配置中已关闭任务提交表格写入。")
        return 0

    sheet_url = config_str(config, "task_submission_sheet_url", DEFAULT_TASK_SHEET_URL)
    spreadsheet_id = extract_spreadsheet_id(sheet_url)
    if not spreadsheet_id:
        print("未配置 task_submission_sheet_url，跳过任务提交表格写入。")
        return 0

    service = load_sheets_service(config, "task_submission_sheet")
    gid = extract_sheet_gid(sheet_url)
    sheet_name, _ = get_sheet_info(service, spreadsheet_id, gid)
    task_schema = load_task_table_schema()
    _, rows, column_map = read_task_sheet_rows(
        service,
        spreadsheet_id,
        sheet_name,
        schema=task_schema,
    )
    updates, matched = build_task_sheet_updates(
        config,
        records,
        rows,
        sheet_name,
        column_map,
    )

    if not matched:
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
            message="已完成唯一匹配，准备写入 Google 表格",
        )
        for item in matched
    ]
    log_path = append_audit_events(config, prepared_events)

    try:
        write_response = service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": updates,
            },
        ).execute()
    except Exception as exc:
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
        for item in matched:
            actual = actual_rows.get(item["row"], {})
            checks = {
                "requester_unchanged": (
                    normalize_match_text(actual.get("requester"))
                    == normalize_match_text(item["before"]["requester"])
                ),
                "task_text_unchanged": (
                    task_text_hash(actual.get("chinese"))
                    == item["before"]["chinese_sha256"]
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

    print(f"任务提交表格日志：{log_path}")
    print(f"已填写任务提交表格：{len(matched)} 条")
    return len(matched)
