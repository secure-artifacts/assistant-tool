import copy
import json
import os
import re
from pathlib import Path

from app_paths import APP_ROOT

DEFAULT_CONFIG_FILE = APP_ROOT / "config.json"
LEGACY_TASK_TABLE_SCHEMA_FILE = APP_ROOT / "TaskTableSchema.json"
TASK_TABLE_SCHEMA_CONFIG_KEY = "task_table_schema"

FIELD_ORDER = (
    "task_id",
    "admin",
    "creator",
    "task_name",
    "task_type",
    "submission_task_type",
    "task_date",
    "task_reference_link",
    "task_audio_text",
    "task_audio_type",
    "review_required",
    "subcategory",
)

# User-facing labels and real spreadsheet aliases belong in the ignored local
# config.  Canonical field names are deliberately neutral and safe to publish.
FIELD_LABELS = {field_name: field_name for field_name in FIELD_ORDER}

SUBMISSION_REQUIRED_FIELDS = (
    "requester",
    "chinese",
    "creator",
    "completed_at",
    "review_status",
    "product_link",
)

SUBMISSION_OPTIONAL_FIELDS = ("task_date", "video_type")

SUBMISSION_FIELD_ORDER = (
    "task_date",
    "requester",
    "chinese",
    "video_type",
    "creator",
    "completed_at",
    "review_status",
    "product_link",
)

SUBMISSION_FIELD_LABELS = {
    field_name: field_name for field_name in SUBMISSION_FIELD_ORDER
}

DEFAULT_TASK_TABLE_SCHEMA = {
    "version": 2,
    "sheet_name": "",
    "sheet_index": 0,
    "header_row": 0,
    "header_scan_rows": 20,
    "labels": dict(FIELD_LABELS),
    "fields": {
        "task_id": {"aliases": [], "default": "{row}"},
        "admin": {"aliases": [], "default": ""},
        "creator": {"aliases": [], "default": ""},
        "task_name": {"aliases": [], "default": "{task_id}"},
        "task_type": {"aliases": [], "default": ""},
        "submission_task_type": {"aliases": [], "default": ""},
        "task_date": {"aliases": [], "default": ""},
        "task_reference_link": {"aliases": [], "default": ""},
        "task_audio_text": {"aliases": [], "default": ""},
        "task_audio_type": {"aliases": [], "default": ""},
        "review_required": {"aliases": [], "default": ""},
        "subcategory": {"aliases": [], "default": ""},
    },
    "submission_sheet": {
        "header_row": 0,
        "header_scan_rows": 20,
        "labels": dict(SUBMISSION_FIELD_LABELS),
        "fields": {
            field_name: {"aliases": []}
            for field_name in SUBMISSION_FIELD_ORDER
        },
    },
}


class TaskTableSchemaError(ValueError):
    pass


def _alias_list(value):
    if isinstance(value, list):
        raw = value
    elif value is None:
        raw = []
    else:
        raw = re.split(r"[,，;；\n]+", str(value))
    result = []
    seen = set()
    for item in raw:
        text = str(item or "").strip()
        key = normalize_header(text)
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def normalize_header(value):
    return re.sub(r"[\s:：_\-—/\\（）()【】\[\]]+", "", str(value or "")).casefold()


def field_label(schema, field_name):
    labels = schema.get("labels", {}) if isinstance(schema, dict) else {}
    return str(labels.get(field_name, FIELD_LABELS.get(field_name, field_name)))


def submission_field_label(schema, field_name):
    submission = schema.get("submission_sheet", {}) if isinstance(schema, dict) else {}
    labels = submission.get("labels", {}) if isinstance(submission, dict) else {}
    return str(
        labels.get(
            field_name,
            SUBMISSION_FIELD_LABELS.get(field_name, field_name),
        )
    )


def normalize_task_table_schema(value):
    raw = value if isinstance(value, dict) else {}
    result = copy.deepcopy(DEFAULT_TASK_TABLE_SCHEMA)
    result["sheet_name"] = str(raw.get("sheet_name", "") or "").strip()
    try:
        result["sheet_index"] = max(0, int(raw.get("sheet_index", 0)))
    except (TypeError, ValueError):
        result["sheet_index"] = 0
    try:
        result["header_row"] = max(0, int(raw.get("header_row", 0)))
    except (TypeError, ValueError):
        result["header_row"] = 0
    try:
        result["header_scan_rows"] = min(100, max(1, int(raw.get("header_scan_rows", 20))))
    except (TypeError, ValueError):
        result["header_scan_rows"] = 20

    raw_labels = raw.get("labels", {})
    if not isinstance(raw_labels, dict):
        raw_labels = {}
    for field_name in FIELD_ORDER:
        result["labels"][field_name] = str(
            raw_labels.get(field_name, FIELD_LABELS[field_name]) or field_name
        ).strip()

    raw_fields = raw.get("fields", {})
    if not isinstance(raw_fields, dict):
        raw_fields = {}
    for field_name in FIELD_ORDER:
        default_spec = DEFAULT_TASK_TABLE_SCHEMA["fields"][field_name]
        raw_spec = raw_fields.get(field_name, {})
        if not isinstance(raw_spec, dict):
            raw_spec = {}
        aliases_source = (
            raw_spec["aliases"] if "aliases" in raw_spec else default_spec["aliases"]
        )
        aliases = _alias_list(aliases_source)
        result["fields"][field_name] = {
            "aliases": aliases,
            "default": str(raw_spec.get("default", default_spec["default"]) or ""),
        }

    raw_submission = raw.get("submission_sheet", {})
    if not isinstance(raw_submission, dict):
        raw_submission = {}
    submission = result["submission_sheet"]
    try:
        submission["header_row"] = max(0, int(raw_submission.get("header_row", 0)))
    except (TypeError, ValueError):
        submission["header_row"] = 0
    try:
        submission["header_scan_rows"] = min(
            100, max(1, int(raw_submission.get("header_scan_rows", 20)))
        )
    except (TypeError, ValueError):
        submission["header_scan_rows"] = 20
    raw_submission_fields = raw_submission.get("fields", {})
    if not isinstance(raw_submission_fields, dict):
        raw_submission_fields = {}
    raw_submission_labels = raw_submission.get("labels", {})
    if not isinstance(raw_submission_labels, dict):
        raw_submission_labels = {}
    for field_name in SUBMISSION_FIELD_ORDER:
        submission["labels"][field_name] = str(
            raw_submission_labels.get(
                field_name, SUBMISSION_FIELD_LABELS[field_name]
            )
            or field_name
        ).strip()
        default_aliases = DEFAULT_TASK_TABLE_SCHEMA["submission_sheet"]["fields"][field_name]["aliases"]
        raw_spec = raw_submission_fields.get(field_name, {})
        if not isinstance(raw_spec, dict):
            raw_spec = {}
        aliases_source = raw_spec["aliases"] if "aliases" in raw_spec else default_aliases
        submission["fields"][field_name] = {"aliases": _alias_list(aliases_source)}
    return result


def _read_json_object(path, description):
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as error:
        raise TaskTableSchemaError("无法读取{}：{}".format(description, error)) from error
    except json.JSONDecodeError as error:
        raise TaskTableSchemaError(
            "{} JSON 错误（第 {} 行）：{}".format(
                description, error.lineno, error.msg
            )
        ) from error
    if not isinstance(raw, dict):
        raise TaskTableSchemaError("{}最外层必须是对象".format(description))
    return raw


def load_task_table_schema(path=None):
    config_path = Path(path or DEFAULT_CONFIG_FILE)
    config = {}
    if config_path.exists():
        config = _read_json_object(config_path, "主配置")

    raw_schema = config.get(TASK_TABLE_SCHEMA_CONFIG_KEY)
    if raw_schema is None and LEGACY_TASK_TABLE_SCHEMA_FILE.exists():
        raw_schema = _read_json_object(
            LEGACY_TASK_TABLE_SCHEMA_FILE,
            "旧任务表格结构配置",
        )
        save_task_table_schema(raw_schema, config_path)

    if raw_schema is not None and not isinstance(raw_schema, dict):
        raise TaskTableSchemaError(
            "config.json 中的 {} 必须是对象".format(
                TASK_TABLE_SCHEMA_CONFIG_KEY
            )
        )
    return normalize_task_table_schema(raw_schema or {})


def save_task_table_schema(schema, path=None):
    config_path = Path(path or DEFAULT_CONFIG_FILE)
    normalized = normalize_task_table_schema(schema)
    config = {}
    if config_path.exists():
        config = _read_json_object(config_path, "主配置")
    config[TASK_TABLE_SCHEMA_CONFIG_KEY] = normalized
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = config_path.with_name(config_path.name + ".tmp")
    try:
        temp_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        os.replace(str(temp_path), str(config_path))
    except OSError:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    return normalized


class _TemplateValues(dict):
    def __missing__(self, key):
        return ""


def render_field_default(template, values, row_number, data_row_number):
    context = _TemplateValues(values)
    context["row"] = row_number
    context["data_row"] = data_row_number
    try:
        return str(template or "").format_map(context).strip()
    except (ValueError, AttributeError) as error:
        raise TaskTableSchemaError("默认值模板格式错误：{}".format(error)) from error
