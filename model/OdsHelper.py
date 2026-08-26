import re
import unicodedata

from odf.opendocument import load
from odf.table import Table, TableRow, TableCell
from odf.text import A, P

from model.TaskTableSchema import (
    FIELD_ORDER,
    TaskTableSchemaError,
    field_label,
    load_task_table_schema,
    normalize_header,
    normalize_task_table_schema,
    render_field_default,
)


MAX_TABLE_COLUMNS = 512


def cell_text_values(cell, include_hrefs=False):
    """Read visible text plus optional hyperlink targets from an ODS cell."""
    values = []
    for paragraph in cell.getElementsByType(P):
        visible_text = str(paragraph).strip()
        if visible_text and visible_text not in values:
            values.append(visible_text)
        if include_hrefs:
            for link in paragraph.getElementsByType(A):
                href = str(link.getAttribute("href") or "").strip()
                if href and href not in values:
                    values.append(href)
    return values


def _repeat_count(cell, attribute, maximum):
    raw = cell.getAttribute(attribute)
    try:
        value = int(raw) if raw else 1
    except (TypeError, ValueError):
        value = 1
    return min(maximum, max(1, value))


def _row_records(row, max_columns=MAX_TABLE_COLUMNS):
    records = []
    for cell in row.getElementsByType(TableCell):
        if len(records) >= max_columns:
            break
        visible = "\n".join(cell_text_values(cell)).strip()
        with_links = "\n".join(cell_text_values(cell, include_hrefs=True)).strip()
        repeat = _repeat_count(cell, "numbercolumnsrepeated", max_columns - len(records))
        records.extend([(visible, with_links)] * repeat)
    return records


def _trim_headers(records):
    headers = [visible.strip() for visible, _with_links in records]
    while headers and not headers[-1]:
        headers.pop()
    return headers


def _select_sheet(doc, schema):
    sheets = doc.getElementsByType(Table)
    if not sheets:
        raise TaskTableSchemaError("任务表格中没有工作表")
    wanted_name = str(schema.get("sheet_name", "") or "").strip()
    if wanted_name:
        wanted_key = normalize_header(wanted_name)
        for sheet in sheets:
            actual_name = str(sheet.getAttribute("name") or "")
            if normalize_header(actual_name) == wanted_key:
                return sheet, actual_name
        available = "、".join(str(sheet.getAttribute("name") or "") for sheet in sheets[:10])
        raise TaskTableSchemaError(
            "找不到工作表“{}”。现有工作表：{}".format(wanted_name, available or "无名称")
        )
    index = int(schema.get("sheet_index", 0))
    if index >= len(sheets):
        raise TaskTableSchemaError(
            "工作表序号 {} 超出范围，文件中只有 {} 张工作表".format(index, len(sheets))
        )
    sheet = sheets[index]
    return sheet, str(sheet.getAttribute("name") or "工作表{}".format(index + 1))


def _header_match_score(headers, schema):
    header_keys = {normalize_header(header) for header in headers if header}
    score = 0
    for spec in schema["fields"].values():
        if any(normalize_header(alias) in header_keys for alias in spec["aliases"]):
            score += 1
    return score


def _find_header(rows, schema):
    configured = int(schema.get("header_row", 0))
    if configured:
        if configured > len(rows):
            raise TaskTableSchemaError(
                "指定的表头行 {} 超出表格范围（共 {} 行）".format(configured, len(rows))
            )
        headers = _trim_headers(_row_records(rows[configured - 1]))
        if not headers:
            raise TaskTableSchemaError("指定的第 {} 行没有表头内容".format(configured))
        return configured - 1, headers, _header_match_score(headers, schema)

    best = None
    scan_count = min(len(rows), int(schema.get("header_scan_rows", 20)))
    for index in range(scan_count):
        headers = _trim_headers(_row_records(rows[index]))
        score = _header_match_score(headers, schema)
        if best is None or score > best[2]:
            best = (index, headers, score)
    if best is None or best[2] < 2:
        raise TaskTableSchemaError(
            "前 {} 行中没有识别到任务表头；请打开“程序设置 → 任务表格”设置表头行和字段别名。".format(
                scan_count
            )
        )
    return best


def _field_columns(headers, schema):
    normalized_headers = [normalize_header(header) for header in headers]
    result = {}
    matched_headers = {}
    for field_name in FIELD_ORDER:
        columns = []
        names = []
        for alias in schema["fields"][field_name]["aliases"]:
            alias_key = normalize_header(alias)
            for index, header_key in enumerate(normalized_headers):
                if alias_key and header_key == alias_key and index not in columns:
                    columns.append(index)
                    names.append(headers[index])
        result[field_name] = columns
        matched_headers[field_name] = names
    return result, matched_headers


def normalize_review_required(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    text = unicodedata.normalize(
        "NFKC",
        str(value if value is not None else ""),
    ).strip().casefold()
    compact = re.sub(r"[\s_\-—/\\（）()【】\[\]]+", "", text)
    if compact in {
        "是", "需要", "需要审核", "要审核", "审核", "强制审核",
        "yes", "y", "true", "1", "on",
    }:
        return True
    if compact in {
        "否", "不需要", "不需要审核", "无需", "无需审核", "跳过审核",
        "no", "n", "false", "0", "off",
    }:
        return False
    return None


def normalize_subcategory_path(value):
    """Return a safe, relative slash-separated Google Drive folder path."""
    text = unicodedata.normalize(
        "NFKC",
        str(value if value is not None else ""),
    ).strip()
    result = []
    for raw_part in re.split(r"[\\/]+", text):
        part = raw_part.strip()
        if part in {"", ".", ".."}:
            continue
        part = re.sub(r'[\x00-\x1f<>:"|?*]', "_", part).strip().rstrip(". ")
        if part and part not in {".", ".."}:
            result.append(part[:100])
    return "/".join(result)


class TaskData:
    def __init__(self, task_info: dict):
        def value(canonical_name):
            return task_info.get(canonical_name)

        self.admin = self.safe_filename(value("admin"))
        self.creator = str(value("creator") or "").strip()
        full_task_name = str(value("task_name") or "").strip()
        self.task_name = self.safe_filename(full_task_name.replace("\n", "_"))[:100]
        self._full_task_name = full_task_name
        self.task_id = self.safe_filename(value("task_id"))
        self.task_type = self.safe_filename(value("task_type"))
        self.defaulted_fields = set(task_info.get("_defaulted_fields", ()))
        self.task_type_from_table = bool(self.task_type) and (
            "task_type" not in self.defaulted_fields
        )
        self.task_date = str(value("task_date") or "").strip()
        self.task_reference_link = str(
            value("task_reference_link") or ""
        ).strip()
        self.task_audio_text = str(value("task_audio_text") or "").strip()
        self.task_audio_type = str(value("task_audio_type") or "").strip()
        self.review_required_text = str(value("review_required") or "").strip()
        self.review_required = normalize_review_required(self.review_required_text)
        self.subcategory_text = str(value("subcategory") or "").strip()
        self.subcategory = normalize_subcategory_path(self.subcategory_text)
        self.source_row = task_info.get("_source_row")
        self.raw = dict(task_info.get("_raw", {}))

    @staticmethod
    def safe_filename(name: str, replacement: str = "_") -> str:
        return re.sub(r'[\\/:*?"<>|]', replacement, str(name or "")).strip()

def ReadTaskOds2(doc_path, schema=None, return_report=False):
    effective_schema = (
        load_task_table_schema() if schema is None else normalize_task_table_schema(schema)
    )
    doc = load(str(doc_path))
    sheet, sheet_name = _select_sheet(doc, effective_schema)
    rows = sheet.getElementsByType(TableRow)
    header_index, headers, header_score = _find_header(rows, effective_schema)
    columns, matched_headers = _field_columns(headers, effective_schema)

    tasks = []
    invalid_required_rows = []
    defaulted_counts = {field_name: 0 for field_name in FIELD_ORDER}
    for physical_index in range(header_index + 1, len(rows)):
        records = _row_records(rows[physical_index], len(headers))
        if len(records) < len(headers):
            records.extend([("", "")] * (len(headers) - len(records)))
        raw = {
            headers[index]: records[index][0]
            for index in range(len(headers))
            if headers[index] and records[index][0]
        }
        mapped = {}
        has_source_value = False
        for field_name in FIELD_ORDER:
            use_links = field_name == "task_reference_link"
            field_value = ""
            for column in columns[field_name]:
                visible, with_links = records[column]
                candidate = with_links if use_links else visible
                candidate = str(candidate or "").strip()
                if candidate:
                    field_value = candidate
                    has_source_value = True
                    break
            mapped[field_name] = field_value

        if not has_source_value:
            continue

        sheet_row_number = physical_index + 1
        data_row_number = physical_index - header_index
        defaulted_fields = set()
        for field_name in FIELD_ORDER:
            if mapped[field_name]:
                continue
            default_value = render_field_default(
                effective_schema["fields"][field_name]["default"],
                mapped,
                sheet_row_number,
                data_row_number,
            )
            if default_value:
                mapped[field_name] = default_value
                defaulted_counts[field_name] += 1
                defaulted_fields.add(field_name)
        missing_required = [
            field_label(effective_schema, field_name)
            for field_name in ("task_id", "task_type")
            if not mapped[field_name]
        ]
        if missing_required:
            invalid_required_rows.append(
                "第{}行缺少{}".format(sheet_row_number, "、".join(missing_required))
            )
            continue
        mapped["_source_row"] = sheet_row_number
        mapped["_raw"] = raw
        mapped["_defaulted_fields"] = defaulted_fields
        tasks.append(TaskData(mapped))

    if invalid_required_rows:
        raise TaskTableSchemaError(
            "；".join(invalid_required_rows[:10])
            + ("；还有更多行" if len(invalid_required_rows) > 10 else "")
            + "。请在“程序设置 → 任务表格”中设置对应表头或默认值。"
        )

    report = {
        "sheet_name": sheet_name,
        "header_row": header_index + 1,
        "header_score": header_score,
        "headers": headers,
        "matched_headers": matched_headers,
        "unmatched_fields": [
            field_name for field_name in FIELD_ORDER if not matched_headers[field_name]
        ],
        "defaulted_counts": defaulted_counts,
        "labels": dict(effective_schema.get("labels", {})),
        "task_count": len(tasks),
    }
    if return_report:
        return tasks, report
    return tasks


def ReadTaskOds(doc_path, schema=None):
    return ReadTaskOds2(doc_path, schema=schema)


def format_task_table_report(report):
    labels = report.get("labels", {})
    matched = []
    for field_name in FIELD_ORDER:
        headers = report["matched_headers"].get(field_name, [])
        if headers:
            matched.append("{}←{}".format(labels.get(field_name, field_name), "/".join(headers)))
    generated = []
    for field_name, count in report.get("defaulted_counts", {}).items():
        if count:
            generated.append("{} {}项".format(labels.get(field_name, field_name), count))
    text = "工作表“{}”第 {} 行作为表头，读取 {} 个任务".format(
        report["sheet_name"], report["header_row"], report["task_count"]
    )
    if matched:
        text += "；字段映射：" + "，".join(matched)
    if generated:
        text += "；使用默认值：" + "，".join(generated)
    return text
