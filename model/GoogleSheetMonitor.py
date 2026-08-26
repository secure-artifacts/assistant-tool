import json
import os
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

from PyQt5 import QtCore

from app_paths import APP_ROOT
from model.GoogleDriveDownloader import DownloadError, download_one, parse_drive_link
from model.GoogleSheetsHelper import (
    extract_sheet_gid,
    extract_spreadsheet_id,
    get_sheet_info,
    load_sheets_service,
    sheet_range,
)
from model.TaskReferenceDownloader import (
    DEFAULT_EXPORT_FORMATS,
    extract_google_drive_urls,
)


DEFAULT_MONITOR_STATE_FILE = APP_ROOT / "GoogleSheetMonitorState.json"
DEFAULT_SETTINGS = {
    "enabled": False,
    "sheet_url": "",
    "sheet_range": "",
    "poll_seconds": 60,
    "download_folder": "谷歌表格下载",
}


def normalize_monitor_settings(value):
    raw = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_SETTINGS)
    result["enabled"] = bool(raw.get("enabled", False))
    result["sheet_url"] = str(raw.get("sheet_url", "") or "").strip()
    result["sheet_range"] = str(raw.get("sheet_range", "") or "").strip()
    try:
        result["poll_seconds"] = min(3600, max(15, int(raw.get("poll_seconds", 60))))
    except (TypeError, ValueError):
        result["poll_seconds"] = 60
    folder = str(raw.get("download_folder", "谷歌表格下载") or "").strip()
    result["download_folder"] = folder or "谷歌表格下载"
    return result


def _default_state():
    return {
        "version": 2,
        "source": "",
        "initialized": False,
        # Version 2 stores only links present in the latest successful poll.
        # This lets a deleted link become new again if it is pasted back later.
        "seen_keys": [],
        "pending": [],
        "history": [],
    }


def read_monitor_state(path=None):
    state_path = Path(path or DEFAULT_MONITOR_STATE_FILE)
    if not state_path.exists():
        return _default_state()
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return _default_state()
    if not isinstance(raw, dict):
        return _default_state()
    state = _default_state()
    state.update(raw)
    state["version"] = 2
    for key in ("seen_keys", "pending", "history"):
        if not isinstance(state.get(key), list):
            state[key] = []
    return state


def write_monitor_state(state, path=None):
    state_path = Path(path or DEFAULT_MONITOR_STATE_FILE)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_name(state_path.name + ".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temp_path), str(state_path))


def reset_monitor_baseline(path=None):
    state = read_monitor_state(path)
    state["source"] = ""
    state["initialized"] = False
    state["seen_keys"] = []
    write_monitor_state(state, path)


def _append_links(target, value):
    if value is None:
        return
    for url in extract_google_drive_urls(str(value)):
        if url not in target:
            target.append(url)


def extract_links_from_grid_response(response):
    links = []
    for sheet in response.get("sheets", []):
        for grid in sheet.get("data", []):
            for row in grid.get("rowData", []):
                for cell in row.get("values", []):
                    _append_links(links, cell.get("hyperlink"))
                    for value_key in (
                        "formattedValue",
                        "userEnteredValue",
                        "effectiveValue",
                    ):
                        value = cell.get(value_key)
                        if isinstance(value, dict):
                            for nested in value.values():
                                _append_links(links, nested)
                        else:
                            _append_links(links, value)
                    formats = [cell.get("userEnteredFormat", {}).get("textFormat", {})]
                    formats.extend(
                        run.get("format", {}) for run in cell.get("textFormatRuns", [])
                    )
                    for text_format in formats:
                        link = text_format.get("link", {}) if isinstance(text_format, dict) else {}
                        _append_links(links, link.get("uri"))
    return links


def link_key(url):
    try:
        parsed = parse_drive_link(url)
        return "{}:{}".format(parsed.google_type or "file", parsed.file_id)
    except (DownloadError, ValueError):
        return str(url).strip()


def _safe_download_root(project_dir, folder):
    project_root = Path(project_dir).resolve()
    folder_path = Path(str(folder or "谷歌表格下载"))
    if folder_path.is_absolute() or ".." in folder_path.parts:
        raise ValueError("下载子目录必须是项目目录内的相对路径")
    target = project_root.joinpath(folder_path).resolve()
    if os.path.commonpath((str(project_root), str(target))) != str(project_root):
        raise ValueError("下载目录超出了当前项目目录")
    target.mkdir(parents=True, exist_ok=True)
    return target


class GoogleSheetMonitorThread(QtCore.QThread):
    status = QtCore.pyqtSignal(str)
    detected = QtCore.pyqtSignal(object)
    download_result = QtCore.pyqtSignal(object)
    queue_changed = QtCore.pyqtSignal(int)
    log = QtCore.pyqtSignal(str)

    def __init__(self, settings, api_config=None, state_path=None, parent=None):
        super().__init__(parent)
        self.settings = normalize_monitor_settings(settings)
        self.api_config = dict(api_config or {})
        self.state_path = Path(state_path or DEFAULT_MONITOR_STATE_FILE)
        self._target_lock = threading.Lock()
        self._project_dir = None
        self._wake_event = threading.Event()
        self._last_error = ""

    def set_project_dir(self, project_dir):
        with self._target_lock:
            self._project_dir = Path(project_dir).resolve() if project_dir else None
        self.request_check()

    def request_check(self):
        self._wake_event.set()

    def stop(self):
        self.requestInterruption()
        self._wake_event.set()

    def _project_snapshot(self):
        with self._target_lock:
            return self._project_dir

    def _wait(self):
        self._wake_event.wait(self.settings["poll_seconds"])
        self._wake_event.clear()

    def _source_signature(self):
        return "{}:{}:{}".format(
            extract_spreadsheet_id(self.settings["sheet_url"]),
            extract_sheet_gid(self.settings["sheet_url"]),
            self.settings["sheet_range"],
        )

    def _read_links(self, service):
        spreadsheet_id = extract_spreadsheet_id(self.settings["sheet_url"])
        if not spreadsheet_id:
            raise ValueError("没有设置 Google 表格链接")
        gid = extract_sheet_gid(self.settings["sheet_url"])
        sheet_name, _sheet_id = get_sheet_info(service, spreadsheet_id, gid)
        configured_range = self.settings["sheet_range"]
        if configured_range:
            range_name = sheet_range(sheet_name, configured_range)
        else:
            safe_name = sheet_name.replace("'", "''")
            range_name = "'{}'".format(safe_name)
        response = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            ranges=[range_name],
            includeGridData=True,
        ).execute()
        return extract_links_from_grid_response(response)

    def _detect(self, state, links):
        signature = self._source_signature()
        if state.get("source") != signature:
            state["source"] = signature
            state["initialized"] = False
            state["seen_keys"] = []

        pairs = []
        current_keys = set()
        for url in links:
            key = link_key(url)
            if key in current_keys:
                continue
            current_keys.add(key)
            pairs.append((key, url))
        current_order = [key for key, _url in pairs]
        if not state.get("initialized"):
            state["seen_keys"] = current_order
            state["initialized"] = True
            write_monitor_state(state, self.state_path)
            self.log.emit("表格监视器已建立基线：当前已有 {} 个 Google 链接，不下载旧链接。".format(len(pairs)))
            return

        previous_order = [str(value) for value in state.get("seen_keys", [])]
        previous_keys = set(previous_order)
        pending_keys = {
            str(item.get("key"))
            for item in state.get("pending", [])
            if isinstance(item, dict)
        }
        new_items = []
        now = time.time()
        for key, url in pairs:
            if key in previous_keys:
                continue
            if key not in pending_keys:
                item = {
                    "key": key,
                    "url": url,
                    "detected_at": now,
                    "attempts": 0,
                    "next_retry": 0,
                    "last_error": "",
                }
                state["pending"].append(item)
                pending_keys.add(key)
                new_items.append(item)

        removed_count = len(previous_keys - current_keys)
        snapshot_changed = previous_keys != current_keys
        state["seen_keys"] = current_order
        if snapshot_changed or new_items:
            write_monitor_state(state, self.state_path)
        if removed_count:
            self.log.emit(
                "表格中有 {} 个链接已被移除；以后重新粘贴时会按新任务处理。".format(
                    removed_count
                )
            )
        if new_items:
            self.detected.emit({"count": len(new_items), "items": new_items})

    def _drain_pending(self, state):
        project_dir = self._project_snapshot()
        if project_dir is None or not state.get("pending"):
            self.queue_changed.emit(len(state.get("pending", [])))
            return
        output_dir = _safe_download_root(project_dir, self.settings["download_folder"])
        changed = False
        now = time.time()
        for item in list(state["pending"]):
            if self.isInterruptionRequested():
                break
            if float(item.get("next_retry", 0) or 0) > now:
                continue
            url = str(item.get("url", ""))
            try:
                status, target = download_one(
                    parse_drive_link(url),
                    output_dir,
                    overwrite=False,
                    timeout=60,
                    export_formats=DEFAULT_EXPORT_FORMATS,
                )
                state["pending"].remove(item)
                history_item = {
                    "key": item.get("key"),
                    "url": url,
                    "finished_at": time.time(),
                    "status": status,
                    "path": str(target),
                }
                state["history"].append(history_item)
                state["history"] = state["history"][-200:]
                changed = True
                self.download_result.emit(
                    {
                        "ok": True,
                        "status": status,
                        "url": url,
                        "path": str(target),
                        "pending": len(state["pending"]),
                    }
                )
            except (DownloadError, HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
                attempts = int(item.get("attempts", 0) or 0) + 1
                item["attempts"] = attempts
                item["last_error"] = str(error)
                item["next_retry"] = time.time() + min(3600, 300 * (2 ** min(attempts - 1, 3)))
                changed = True
                self.download_result.emit(
                    {
                        "ok": False,
                        "url": url,
                        "error": str(error),
                        "pending": len(state["pending"]),
                    }
                )
        if changed:
            write_monitor_state(state, self.state_path)
        self.queue_changed.emit(len(state.get("pending", [])))

    def run(self):
        state = read_monitor_state(self.state_path)
        self.queue_changed.emit(len(state.get("pending", [])))
        service = None
        while not self.isInterruptionRequested():
            try:
                if service is None:
                    self.status.emit("正在连接 Google 表格…")
                    service = load_sheets_service(self.api_config, "sheet_monitor")
                links = self._read_links(service)
                self._detect(state, links)
                self._drain_pending(state)
                self.status.emit("运行中")
                self._last_error = ""
            except SystemExit as error:
                self.status.emit("异常：{}".format(error))
                message = str(error)
                if message != self._last_error:
                    self.log.emit("表格监视器异常：{}".format(error))
                    self._last_error = message
                service = None
            except Exception as error:
                self.status.emit("异常：{}".format(error))
                message = str(error)
                if message != self._last_error:
                    self.log.emit("表格监视器异常：{}".format(error))
                    self._last_error = message
                service = None
            if not self.isInterruptionRequested():
                self._wait()
