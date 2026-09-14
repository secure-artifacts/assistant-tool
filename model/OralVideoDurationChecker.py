"""Check oral-video durations in the configured task submission sheet."""

from __future__ import annotations

import re
from typing import Callable, Dict, Optional

from model.GoogleDriveDownloader import DownloadError, parse_drive_link
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
from model.TaskSubmissionHelper import (
    ORAL_LONG_VIDEO_TYPE,
    ORAL_SHORT_MAX_DURATION_MILLIS,
    ORAL_SHORT_VIDEO_TYPE,
    column_letter,
    normalize_match_text,
    read_task_sheet_rows,
)
from model.TaskTableSchema import load_task_table_schema


SOURCE_VIDEO_TYPE = ORAL_SHORT_VIDEO_TYPE
REPLACEMENT_VIDEO_TYPE = ORAL_LONG_VIDEO_TYPE
MAX_SHORT_VIDEO_DURATION_MILLIS = ORAL_SHORT_MAX_DURATION_MILLIS
GOOGLE_DRIVE_URL_PATTERN = re.compile(
    r"https?://(?:drive\.google\.com|drive\.usercontent\.google\.com|docs\.google\.com)"
    r"/[^\s<>\"']+",
    re.IGNORECASE,
)


def format_duration(duration_millis: int) -> str:
    total_seconds = max(0, int(duration_millis)) / 1000
    whole_seconds = int(total_seconds)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    milliseconds = int(round((total_seconds - whole_seconds) * 1000))
    if milliseconds == 1000:
        seconds += 1
        milliseconds = 0
        if seconds == 60:
            minutes += 1
            seconds = 0
        if minutes == 60:
            hours += 1
            minutes = 0
    if hours:
        base = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        base = f"{minutes:02d}:{seconds:02d}"
    return f"{base}.{milliseconds:03d}" if milliseconds else base


def video_duration_millis(metadata: Dict) -> Optional[int]:
    video_metadata = metadata.get("videoMediaMetadata")
    if not isinstance(video_metadata, dict):
        return None
    raw_value = video_metadata.get("durationMillis")
    if raw_value in (None, ""):
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _report(callback: Optional[Callable[[str], None]], message: str) -> None:
    if callback is not None:
        callback(str(message))


def _skip(row: Dict, reason: str, link: str = "") -> Dict:
    return {
        "row": row["row"],
        "task_name": str(row.get("chinese") or "").strip(),
        "link": link,
        "reason": str(reason),
    }


def _cell_drive_links(cell: Dict) -> list[str]:
    raw_values = [cell.get("hyperlink")]
    for value_key in ("formattedValue", "userEnteredValue", "effectiveValue"):
        value = cell.get(value_key)
        if isinstance(value, dict):
            raw_values.extend(value.values())
        else:
            raw_values.append(value)

    formats = [cell.get("userEnteredFormat", {}).get("textFormat", {})]
    formats.extend(
        run.get("format", {}) for run in cell.get("textFormatRuns", [])
    )
    for text_format in formats:
        if isinstance(text_format, dict):
            link = text_format.get("link")
            if isinstance(link, dict):
                raw_values.append(link.get("uri"))

    links = []
    for raw_value in raw_values:
        for match in GOOGLE_DRIVE_URL_PATTERN.findall(str(raw_value or "")):
            link = match.rstrip(".,;，；)]}）】")
            if link and link not in links:
                links.append(link)
    return links


def read_rich_product_links_by_row(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    product_link_column: int,
    row_numbers,
) -> Dict[int, str]:
    """Read URLs behind linked display text in the product-link cells."""

    wanted_rows = {int(row_number) for row_number in row_numbers}
    if not wanted_rows:
        return {}
    first_row = min(wanted_rows)
    last_row = max(wanted_rows)
    column = column_letter(product_link_column)
    response = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        ranges=[sheet_range(sheet_name, f"{column}{first_row}:{column}{last_row}")],
        includeGridData=True,
    ).execute()

    result = {}
    for sheet in response.get("sheets", []):
        for grid in sheet.get("data", []):
            start_row = int(grid.get("startRow", first_row - 1) or 0)
            for offset, row_data in enumerate(grid.get("rowData", [])):
                row_number = start_row + offset + 1
                if row_number not in wanted_rows:
                    continue
                cells = row_data.get("values", [])
                if not cells:
                    continue
                links = _cell_drive_links(cells[0])
                if links:
                    result[row_number] = links[0]
    return result


def check_oral_video_durations(
    config: Dict,
    progress_callback: Optional[Callable[[str], None]] = None,
    *,
    sheets_service_factory=None,
    drive_service_factory=None,
    schema=None,
) -> Dict:
    """Check and update matching rows without downloading video contents."""

    sheet_url = config_str(config, "task_submission_sheet_url")
    spreadsheet_id = extract_spreadsheet_id(sheet_url)
    if not spreadsheet_id:
        raise ValueError("未配置有效的任务提交表格链接")

    creator = config_str(
        config,
        "task_submission_creator",
        config_str(config, "review_sheet_submitter"),
    )
    if not creator:
        raise ValueError("未配置任务制作人，请先在程序设置中填写")

    sheets_factory = sheets_service_factory or load_sheets_service
    drive_factory = drive_service_factory or load_drive_service
    sheets_service = sheets_factory(config, "task_submission_sheet")
    gid = extract_sheet_gid(sheet_url)
    sheet_name, sheet_id = get_sheet_info(sheets_service, spreadsheet_id, gid)
    effective_schema = schema or load_task_table_schema()
    _header_row, rows, column_map = read_task_sheet_rows(
        sheets_service,
        spreadsheet_id,
        sheet_name,
        schema=effective_schema,
        include_incomplete=True,
    )
    if "video_type" not in column_map:
        raise RuntimeError(
            "任务提交表格没有识别到“视频类型”列，请先在程序设置的任务表格中检查字段别名"
        )

    creator_key = normalize_match_text(creator)
    candidates = [
        row
        for row in rows
        if normalize_match_text(row.get("creator")) == creator_key
        and str(row.get("video_type") or "").strip() == SOURCE_VIDEO_TYPE
    ]
    _report(
        progress_callback,
        f"工作表“{sheet_name}”中找到 {len(candidates)} 行待检测口播视频。",
    )

    result = {
        "sheet_url": sheet_url,
        "sheet_name": sheet_name,
        "sheet_id": sheet_id,
        "creator": creator,
        "source_video_type": SOURCE_VIDEO_TYPE,
        "replacement_video_type": REPLACEMENT_VIDEO_TYPE,
        "candidate_count": len(candidates),
        "checked_count": 0,
        "updated_count": 0,
        "updated_rows": [],
        "skipped_rows": [],
    }
    if not candidates:
        return result

    rich_links_by_row = read_rich_product_links_by_row(
        sheets_service,
        spreadsheet_id,
        sheet_name,
        column_map["product_link"],
        (row["row"] for row in candidates),
    )
    drive_service = drive_factory()
    updates = []
    for index, row in enumerate(candidates, start=1):
        row_number = row["row"]
        link = rich_links_by_row.get(row_number) or extract_link_text(
            row.get("product_link")
        )
        if not link:
            skipped = _skip(row, "成品链接为空")
            result["skipped_rows"].append(skipped)
            _report(progress_callback, f"第 {row_number} 行：成品链接为空，已跳过。")
            continue

        try:
            drive_link = parse_drive_link(link)
            metadata = drive_service.files().get(
                fileId=drive_link.file_id,
                fields=(
                    "id,name,mimeType,size,"
                    "videoMediaMetadata(durationMillis,width,height)"
                ),
                supportsAllDrives=True,
            ).execute()
            duration_millis = video_duration_millis(metadata)
            if duration_millis is None:
                raise ValueError("Google Drive 尚未提供这个文件的视频时长")
        except (DownloadError, ValueError) as error:
            skipped = _skip(row, str(error), link)
            result["skipped_rows"].append(skipped)
            _report(progress_callback, f"第 {row_number} 行：{error}，已跳过。")
            continue
        except Exception as error:
            reason = f"读取 Google Drive 元数据失败：{type(error).__name__}: {error}"
            skipped = _skip(row, reason, link)
            result["skipped_rows"].append(skipped)
            _report(progress_callback, f"第 {row_number} 行：{reason}")
            continue

        result["checked_count"] += 1
        duration_text = format_duration(duration_millis)
        file_name = str(metadata.get("name") or "").strip()
        _report(
            progress_callback,
            f"[{index}/{len(candidates)}] 第 {row_number} 行："
            f"{file_name or '未命名视频'}，时长 {duration_text}。",
        )
        if duration_millis <= MAX_SHORT_VIDEO_DURATION_MILLIS:
            continue

        cell = "{}{}".format(column_letter(column_map["video_type"]), row_number)
        updates.append({
            "range": sheet_range(sheet_name, cell),
            "values": [[REPLACEMENT_VIDEO_TYPE]],
        })
        result["updated_rows"].append({
            "row": row_number,
            "cell": cell,
            "task_name": str(row.get("chinese") or "").strip(),
            "file_name": file_name,
            "duration_millis": duration_millis,
            "duration": duration_text,
            "old_video_type": SOURCE_VIDEO_TYPE,
            "new_video_type": REPLACEMENT_VIDEO_TYPE,
            "link": link,
        })

    if updates:
        sheets_service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "RAW",
                "data": updates,
            },
        ).execute()
        result["updated_count"] = len(updates)
        _report(progress_callback, f"已修改 {len(updates)} 行的视频类型。")
    else:
        _report(progress_callback, "没有发现超过 1 分钟且需要修改的行。")

    return result
