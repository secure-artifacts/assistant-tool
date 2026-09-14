import threading

from PyQt5 import QtCore

from model.GoogleSheetsHelper import (
    extract_sheet_gid,
    extract_spreadsheet_id,
    get_sheet_info,
    load_sheets_service,
    normalize_header,
    sheet_range,
)
from model.ReviewSubmissionHistory import (
    apply_review_statuses,
    canonical_review_link,
    review_history_snapshot,
)


DEFAULT_REVIEW_STATUS_SETTINGS = {
    "review_status_monitor_enabled": True,
    "review_status_monitor_poll_seconds": 300,
}


def normalize_review_status_settings(config=None):
    source = config if isinstance(config, dict) else {}
    enabled = source.get("review_status_monitor_enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().casefold() in {"1", "true", "yes", "on", "是"}
    try:
        poll_seconds = int(source.get("review_status_monitor_poll_seconds", 300))
    except (TypeError, ValueError):
        poll_seconds = 300
    return {
        "review_status_monitor_enabled": bool(enabled),
        "review_status_monitor_poll_seconds": min(86400, max(60, poll_seconds)),
    }


def _indices(headers, text):
    wanted = normalize_header(text)
    return [
        index
        for index, header in enumerate(headers)
        if normalize_header(header) == wanted
    ]


def detect_review_columns(headers):
    """Recognize the current review sheet while tolerating moved columns."""
    normalized = [normalize_header(value) for value in headers]

    def exact(*names):
        for name in names:
            wanted = normalize_header(name)
            if wanted in normalized:
                return normalized.index(wanted)
        return None

    link = exact("成品-视频链接", "成品视频链接", "视频链接", "成品链接")
    if link is None:
        link = next(
            (
                index
                for index, value in enumerate(normalized)
                if "链接" in value and ("成品" in value or "视频" in value)
                and "返修" not in value
            ),
            None,
        )
    notes = _indices(headers, "文字建议")
    severities = _indices(headers, "严重程度")
    columns = {
        "link": link,
        "result1": exact("审核结果1"),
        "result2": exact("审核结果2"),
        "note1": notes[0] if len(notes) > 0 else None,
        "note2": notes[1] if len(notes) > 1 else None,
        "severity1": severities[0] if len(severities) > 0 else None,
        "severity2": severities[1] if len(severities) > 1 else None,
        "rework_link": exact("返修链接"),
        "rework_result1": exact("返修审核结果1"),
        "rework_result2": exact("返修审核结果2"),
        "rework_note1": notes[2] if len(notes) > 2 else None,
        "rework_note2": notes[3] if len(notes) > 3 else None,
    }
    if columns["link"] is None:
        raise ValueError("审核表中没有找到成品视频链接列")
    if columns["result1"] is None and columns["result2"] is None:
        raise ValueError("审核表中没有找到审核结果列")
    return columns


def _cell(row, index):
    if index is None or index < 0 or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _combine(row, columns, names):
    result = []
    for name in names:
        value = _cell(row, columns.get(name))
        if value and value not in result:
            result.append(value)
    return "；".join(result)


def _review_state(results):
    values = [str(value or "").strip() for value in results]
    nonempty = [value for value in values if value]
    if any("需要修改" in value for value in nonempty):
        return "needs_changes"
    # The sheet has two independent reviewers.  Do not announce approval
    # until every configured result column explicitly says it can be used.
    if values and all(value and "可以使用" in value for value in values):
        return "passed"
    return "pending"


def review_status_from_row(row, columns, sheet_row=0):
    rework_link = _cell(row, columns.get("rework_link"))
    rework_results = [
        _cell(row, columns.get(name))
        for name in ("rework_result1", "rework_result2")
        if columns.get(name) is not None
    ]
    use_rework = bool(rework_link or any(rework_results))
    if use_rework:
        return {
            "status": _review_state(rework_results),
            "phase": "返修",
            "note": _combine(row, columns, ("rework_note1", "rework_note2")),
            "severity": "",
            "sheet_row": sheet_row,
        }
    initial_results = [
        _cell(row, columns.get(name))
        for name in ("result1", "result2")
        if columns.get(name) is not None
    ]
    return {
        "status": _review_state(initial_results),
        "phase": "初审",
        "note": _combine(row, columns, ("note1", "note2")),
        "severity": _combine(row, columns, ("severity1", "severity2")),
        "sheet_row": sheet_row,
    }


def statuses_from_review_values(values):
    header_index = None
    columns = None
    for index, row in enumerate((values or [])[:10]):
        try:
            candidate = detect_review_columns(row)
        except ValueError:
            continue
        header_index = index
        columns = candidate
        break
    if header_index is None:
        raise ValueError("审核表前 10 行中没有找到可识别的表头")
    result = {}
    for offset, row in enumerate(values[header_index + 1 :], start=header_index + 2):
        key = canonical_review_link(_cell(row, columns["link"]))
        if key:
            result[key] = review_status_from_row(row, columns, offset)
    return result


class ReviewStatusMonitorThread(QtCore.QThread):
    status = QtCore.pyqtSignal(str)
    snapshot = QtCore.pyqtSignal(object)
    changed = QtCore.pyqtSignal(object)
    log = QtCore.pyqtSignal(str)

    def __init__(self, config, history_path=None, parent=None):
        super().__init__(parent)
        self.config = dict(config or {})
        self.settings = normalize_review_status_settings(config)
        self.history_path = history_path
        self._wake_event = threading.Event()
        self._last_error = ""

    def request_check(self):
        self._wake_event.set()

    def stop(self):
        self.requestInterruption()
        self._wake_event.set()

    def _wait(self):
        settings = normalize_review_status_settings(self.config)
        self._wake_event.wait(settings["review_status_monitor_poll_seconds"])
        self._wake_event.clear()

    def _read_values(self, service):
        spreadsheet_id = extract_spreadsheet_id(
            str(self.config.get("review_sheet_url") or "")
        )
        if not spreadsheet_id:
            raise ValueError("没有配置审核表格链接")
        gid = extract_sheet_gid(str(self.config.get("review_sheet_url") or ""))
        sheet_name, _sheet_id = get_sheet_info(service, spreadsheet_id, gid)
        return service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=sheet_range(sheet_name, "A:X"),
            valueRenderOption="FORMULA",
        ).execute().get("values", [])

    def _emit_snapshot(self):
        snapshot = review_history_snapshot(self.history_path)
        self.snapshot.emit(snapshot)
        return snapshot

    def run(self):
        service = None
        while not self.isInterruptionRequested():
            try:
                snapshot = self._emit_snapshot()
                if not snapshot["all"]:
                    self.status.emit("等待新的审核提交记录")
                else:
                    if service is None:
                        self.status.emit("正在读取审核表…")
                        service = load_sheets_service(self.config)
                    values = self._read_values(service)
                    statuses = statuses_from_review_values(values)
                    transitions = apply_review_statuses(
                        statuses,
                        self.history_path,
                    )
                    snapshot = self._emit_snapshot()
                    self.status.emit("运行中")
                    if transitions:
                        self.changed.emit(
                            {
                                "items": transitions,
                                "passed": [
                                    item for item in transitions
                                    if item.get("status") == "passed"
                                ],
                                "needs_changes": [
                                    item for item in transitions
                                    if item.get("status") == "needs_changes"
                                ],
                                "snapshot": snapshot,
                            }
                        )
                self._last_error = ""
            except (Exception, SystemExit) as error:
                message = "{}: {}".format(type(error).__name__, error)
                self.status.emit("异常")
                if message != self._last_error:
                    self.log.emit("审核结果监视失败：{}".format(message))
                    self._last_error = message
                service = None
            if not self.isInterruptionRequested():
                self._wait()
