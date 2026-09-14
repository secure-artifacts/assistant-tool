import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from app_paths import APP_ROOT


DEFAULT_REVIEW_HISTORY_FILE = APP_ROOT / "ReviewSubmissionHistory.json"
_HISTORY_LOCK = threading.RLock()


def _empty_history():
    return {"version": 1, "items": {}}


def canonical_review_link(value):
    """Return a stable key even when Drive displays the link differently."""
    text = str(value or "").strip()
    if not text:
        return ""
    formula = re.search(r'HYPERLINK\("([^"]+)"', text, flags=re.I)
    if formula:
        text = formula.group(1).strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text.casefold()
    host = parsed.netloc.casefold().split(":", 1)[0]
    if host.endswith("drive.google.com") or host.endswith("docs.google.com"):
        path_match = re.search(
            r"/(?:file/d|document/d|spreadsheets/d|presentation/d|folders)/([^/?#]+)",
            parsed.path,
            flags=re.I,
        )
        if path_match:
            return "google:{}".format(path_match.group(1))
        file_id = parse_qs(parsed.query).get("id", [""])[0]
        if file_id:
            return "google:{}".format(file_id)
    if parsed.scheme and parsed.netloc:
        return urlunsplit(
            (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/"), "", "")
        )
    return text.casefold()


def _load_unlocked(path):
    history_path = Path(path or DEFAULT_REVIEW_HISTORY_FILE)
    if not history_path.exists():
        return _empty_history()
    try:
        raw = json.loads(history_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return _empty_history()
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), dict):
        return _empty_history()
    raw["version"] = 1
    return raw


def read_review_history(path=None):
    with _HISTORY_LOCK:
        return _load_unlocked(path)


def _write_unlocked(history, path):
    history_path = Path(path or DEFAULT_REVIEW_HISTORY_FILE)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = history_path.with_name(history_path.name + ".tmp")
    temp_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(str(temp_path), str(history_path))


def record_review_submissions(records, path=None, now=None):
    """Remember successfully submitted links and their local administrator."""
    timestamp = float(now if now is not None else time.time())
    changed = 0
    with _HISTORY_LOCK:
        history = _load_unlocked(path)
        items = history["items"]
        for record in records or ():
            link = str(
                record.get("webViewLink")
                or record.get("webContentLink")
                or (
                    "https://drive.google.com/file/d/{}/view".format(record.get("id"))
                    if record.get("id")
                    else ""
                )
            ).strip()
            key = canonical_review_link(link)
            if not key:
                continue
            name = str(
                record.get("name")
                or Path(str(record.get("local_file") or "")).name
                or "未命名视频"
            ).strip()
            existing = items.get(key, {})
            updated = dict(existing)
            updated.update({"key": key, "link": link, "name": name})
            for field, record_field in (
                ("admin", "local_admin"),
                ("task_name", "local_task_name"),
                ("task_id", "local_task_id"),
            ):
                value = str(record.get(record_field) or "").strip()
                if value or field not in existing:
                    updated[field] = value
            if not existing:
                updated.update(
                    {
                        "submitted_at": timestamp,
                        "status": "pending",
                        "phase": "初审",
                        "note": "",
                        "severity": "",
                        "acknowledged_status": "",
                        "last_notified_status": "",
                    }
                )
            items[key] = updated
            if updated != existing:
                changed += 1
        if changed:
            _write_unlocked(history, path)
    return changed


def apply_review_statuses(status_by_key, path=None, now=None):
    """Update tracked rows and return status transitions needing notification."""
    timestamp = float(now if now is not None else time.time())
    transitions = []
    with _HISTORY_LOCK:
        history = _load_unlocked(path)
        changed = False
        for key, result in (status_by_key or {}).items():
            item = history["items"].get(key)
            if item is None:
                continue
            new_status = str(result.get("status") or "pending")
            old_status = str(item.get("status") or "pending")
            for field in ("phase", "note", "severity", "sheet_row"):
                new_value = result.get(field, "")
                if item.get(field) != new_value:
                    item[field] = new_value
                    changed = True
            if new_status != old_status:
                item["status"] = new_status
                item["status_updated_at"] = timestamp
                item["acknowledged_status"] = ""
                item["last_notified_status"] = ""
                changed = True
            if (
                new_status in {"passed", "needs_changes"}
                and item.get("last_notified_status") != new_status
            ):
                transitions.append(dict(item))
                item["last_notified_status"] = new_status
                changed = True
        if changed:
            _write_unlocked(history, path)
    return transitions


def acknowledge_review_items(keys, path=None):
    key_set = {str(key) for key in keys or () if str(key)}
    changed = 0
    with _HISTORY_LOCK:
        history = _load_unlocked(path)
        for key in key_set:
            item = history["items"].get(key)
            if item is None:
                continue
            status = str(item.get("status") or "pending")
            if status in {"passed", "needs_changes"} and item.get(
                "acknowledged_status"
            ) != status:
                item["acknowledged_status"] = status
                changed += 1
        if changed:
            _write_unlocked(history, path)
    return changed


def review_history_snapshot(path=None):
    history = read_review_history(path)
    items = [dict(item) for item in history["items"].values()]
    items.sort(key=lambda item: float(item.get("submitted_at") or 0), reverse=True)
    passed = [
        item
        for item in items
        if item.get("status") == "passed"
        and item.get("acknowledged_status") != "passed"
    ]
    needs_changes = [
        item
        for item in items
        if item.get("status") == "needs_changes"
        and item.get("acknowledged_status") != "needs_changes"
    ]
    return {
        "passed": passed,
        "needs_changes": needs_changes,
        "all": items,
        "passed_count": len(passed),
        "needs_changes_count": len(needs_changes),
    }
