import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from app_paths import APP_ROOT
from model.ReviewSubmissionHistory import record_review_submissions

DEFAULT_CREDENTIALS_FILE = APP_ROOT / "GoogleSheetsCredentials.json"
DEFAULT_TOKEN_FILE = APP_ROOT / "GoogleSheetsToken.json"
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def config_bool(config: Dict, name: str, default: bool) -> bool:
    value = config.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "是"}
    return bool(value)


def config_str(config: Dict, name: str, default: str = "") -> str:
    value = config.get(name, default)
    return str(value).strip() if value is not None else default


def extract_spreadsheet_id(value: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", value or "")
    if match:
        return match.group(1)
    return (value or "").strip()


def extract_sheet_gid(value: str) -> Optional[int]:
    match = re.search(r"[?#&]gid=(\d+)", value or "")
    if not match:
        return None
    return int(match.group(1))


def sheet_range(sheet_name: str, a1: str) -> str:
    safe_name = sheet_name.replace("'", "''")
    return f"'{safe_name}'!{a1}"


def load_sheets_service(config: Dict, config_prefix: str = "review_sheet"):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        missing_name = getattr(exc, "name", "Google Sheets API 依赖")
        raise SystemExit(
            "缺少 Google Sheets API 依赖。请先在你的 Python 环境安装：\n"
            "pip install google-api-python-client google-auth google-auth-oauthlib\n"
            f"当前缺少：{missing_name}"
        )

    credentials_default = config_str(config, "review_sheet_credentials_file", str(DEFAULT_CREDENTIALS_FILE))
    token_default = config_str(config, "review_sheet_token_file", str(DEFAULT_TOKEN_FILE))
    credentials_file = Path(config_str(config, f"{config_prefix}_credentials_file", credentials_default))
    token_file = Path(config_str(config, f"{config_prefix}_token_file", token_default))
    if not credentials_file.is_absolute():
        credentials_file = APP_ROOT / credentials_file
    if not token_file.is_absolute():
        token_file = APP_ROOT / token_file

    credentials = None
    if token_file.exists():
        credentials = Credentials.from_authorized_user_file(str(token_file), SHEETS_SCOPES)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not credentials_file.exists():
                raise FileNotFoundError(f"没找到表格凭据文件：{credentials_file}")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SHEETS_SCOPES)
            credentials = flow.run_local_server(port=0)

        token_file.write_text(credentials.to_json(), encoding="utf-8")

    return build("sheets", "v4", credentials=credentials)


def get_sheet_info(service, spreadsheet_id: str, gid: Optional[int]):
    metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = metadata.get("sheets", [])

    if gid is not None:
        for item in sheets:
            props = item.get("properties", {})
            if props.get("sheetId") == gid:
                return props["title"], props["sheetId"]

    if not sheets:
        raise RuntimeError("表格里没有工作表")

    props = sheets[0].get("properties", {})
    return props["title"], props["sheetId"]


def normalize_header(text: str) -> str:
    return str(text or "").strip().replace(" ", "")


def find_header_row(values: List[List[str]], expected_headers=None) -> int:
    expected = {
        normalize_header(value)
        for value in (expected_headers or [])
        if normalize_header(value)
    }
    for index, row in enumerate(values[:10], start=1):
        text = "".join(str(cell) for cell in row)
        if "http://" in text or "https://" in text:
            continue
        headers = {normalize_header(cell) for cell in row if normalize_header(cell)}
        if expected and headers.intersection(expected):
            return index
    return 0


def find_column(headers: List[str], wanted: str, keywords: List[str], fallback: int) -> int:
    wanted_text = normalize_header(wanted)
    normalized = [normalize_header(item) for item in headers]

    if wanted_text:
        for index, header in enumerate(normalized, start=1):
            if header == wanted_text:
                return index

    for keyword in keywords:
        for index, header in enumerate(normalized, start=1):
            if keyword in header:
                return index

    return fallback


def row_has_data(row: List[str]) -> bool:
    return any(str(cell).strip() for cell in row)


def insert_rows(service, spreadsheet_id: str, sheet_id: int, start_row: int, count: int) -> None:
    if count <= 0:
        return

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": start_row - 1,
                            "endIndex": start_row - 1 + count,
                        },
                        "inheritFromBefore": False,
                    }
                }
            ]
        },
    ).execute()


def choose_write_start_row(values: List[List[str]], header_row: int, row_count: int) -> tuple[int, int]:
    data_start = header_row + 1
    first_data_row = None

    for row_index in range(data_start, len(values) + 1):
        row = values[row_index - 1]
        if row_has_data(row):
            first_data_row = row_index
            break

    if first_data_row is None:
        return data_start, 0

    blank_count = max(0, first_data_row - data_start)
    need_insert = max(0, row_count - blank_count)

    if need_insert:
        return data_start, need_insert

    return first_data_row - row_count, 0


def drive_link_from_record(record: Dict) -> str:
    link = record.get("webViewLink") or record.get("webContentLink")
    if link:
        return str(link)

    file_id = record.get("id")
    if file_id:
        return f"https://drive.google.com/file/d/{file_id}/view"

    return ""



def escape_formula_text(text: str) -> str:
    return str(text or "").replace('"', '""')


def make_hyperlink_formula(url: str, title: str) -> str:
    return f'=HYPERLINK("{escape_formula_text(url)}","{escape_formula_text(title)}")'


def extract_link_text(value: str) -> str:
    text = str(value or "").strip()
    match = re.search(r'HYPERLINK\("([^"]+)"', text, flags=re.I)
    if match:
        return match.group(1).strip()
    return text


def remember_review_submissions(records: List[Dict]) -> None:
    try:
        record_review_submissions(records)
    except (OSError, ValueError, TypeError) as error:
        # The sheet operation itself has already succeeded.  A local history
        # write failure must be visible, but must not report the remote write
        # as failed and tempt the user to submit everything again.
        print(f"保存审核提交历史失败：{type(error).__name__}: {error}")

def existing_links(values: List[List[str]], link_col: int, header_row: int) -> set:
    result = set()
    for row in values[header_row:]:
        if len(row) >= link_col:
            link = extract_link_text(row[link_col - 1])
            if link:
                result.add(link)
    return result


def build_rows(config: Dict, records: List[Dict], values: List[List[str]], header_row: int, headers: List[str]) -> List[List[str]]:
    submitter = config_str(config, "review_sheet_submitter", "")
    date_col = find_column(headers, config_str(config, "review_sheet_date_column"), [], int(config.get("review_sheet_date_column_index", 1) or 1))
    submitter_col = find_column(headers, config_str(config, "review_sheet_submitter_column"), [], int(config.get("review_sheet_submitter_column_index", 2) or 2))
    link_col = find_column(headers, config_str(config, "review_sheet_link_column"), [], int(config.get("review_sheet_link_column_index", 3) or 3))

    max_col = max(date_col, submitter_col, link_col, len(headers), 3)
    old_links = existing_links(values, link_col, header_row)
    today_text = date.today().strftime("%Y-%m-%d")
    rows = []

    for record in records:
        link = drive_link_from_record(record)
        if not link:
            continue
        if link in old_links:
            print(f"表格里已存在链接，跳过：{record.get('name') or record.get('local_file')}")
            continue

        file_name = str(record.get("name") or Path(str(record.get("local_file", ""))).name)
        row = [""] * max_col
        row[date_col - 1] = today_text
        row[submitter_col - 1] = submitter
        row[link_col - 1] = make_hyperlink_formula(link, file_name)
        rows.append(row)
        old_links.add(link)

    return rows


def write_review_video_links(config: Dict, records: List[Dict]) -> int:
    if not records:
        return 0

    if not config_bool(config, "review_sheet_enabled", True):
        print("配置中已关闭人工检查表格写入。")
        return 0

    sheet_url = config_str(config, "review_sheet_url")
    spreadsheet_id = extract_spreadsheet_id(sheet_url)
    if not spreadsheet_id:
        print("未配置 review_sheet_url，跳过人工检查表格写入。")
        return 0

    gid = extract_sheet_gid(sheet_url)
    service = load_sheets_service(config)
    sheet_name, sheet_id = get_sheet_info(service, spreadsheet_id, gid)

    read_range = sheet_range(sheet_name, "A1:Z2000")
    values = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=read_range,
        valueRenderOption="FORMULA",
    ).execute().get("values", [])

    if not values:
        values = [[]]

    header_row = find_header_row(
        values,
        [
            config_str(config, "review_sheet_date_column"),
            config_str(config, "review_sheet_submitter_column"),
            config_str(config, "review_sheet_link_column"),
        ],
    )
    headers = values[header_row - 1] if header_row > 0 and len(values) >= header_row else []
    rows = build_rows(config, records, values, header_row, headers)
    if not rows:
        # Existing rows are still useful history (for example after the
        # feature is installed on a machine with earlier submissions).
        remember_review_submissions(records)
        print("没有新的人工检查链接需要写入表格。")
        return 0

    start_row, insert_count = choose_write_start_row(values, header_row, len(rows))
    if insert_count:
        insert_at = header_row + 1 if header_row > 0 else 1
        insert_rows(service, spreadsheet_id, sheet_id, insert_at, insert_count)
        start_row = insert_at

    end_row = start_row + len(rows) - 1
    write_range = sheet_range(sheet_name, f"A{start_row}:Z{end_row}")
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=write_range,
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()

    remember_review_submissions(records)
    print(f"已写入人工检查表格：{len(rows)} 条")
    return len(rows)
