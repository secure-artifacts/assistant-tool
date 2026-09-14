import math
import os
import re
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets
from pydub import AudioSegment, silence

from app_plugins.api import TASK_CONTEXT_MENU, TOOLS_MENU, PluginCommand, PluginSettingsPage


AUDIO_SPLITTER_CONFIG_KEY = "audio_splitter"
DEFAULT_AUDIO_SPLITTER_SETTINGS = {
    "max_length_seconds": 30,
    "tolerance_seconds": 30,
    "min_silence_ms": 600,
    "silence_offset_db": 16.0,
    "extensions": [".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"],
    "recursive": True,
    "include_source_name": False,
}
_GENERATED_CHUNK_PATTERN = re.compile(r"(?:^|_)片段\d+$", re.IGNORECASE)


def _number(value, default, minimum, maximum, value_type=float):
    try:
        result = value_type(value)
    except (TypeError, ValueError):
        result = value_type(default)
    return max(value_type(minimum), min(value_type(maximum), result))


def _extensions(value):
    if isinstance(value, str):
        raw_values = re.split(r"[,，;；\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = DEFAULT_AUDIO_SPLITTER_SETTINGS["extensions"]
    result = []
    for raw in raw_values:
        extension = str(raw or "").strip().casefold()
        if not extension:
            continue
        if not extension.startswith("."):
            extension = "." + extension
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
            continue
        if extension not in result:
            result.append(extension)
    return result or list(DEFAULT_AUDIO_SPLITTER_SETTINGS["extensions"])


def normalize_audio_splitter_settings(value):
    source = value if isinstance(value, dict) else {}
    maximum = _number(
        source.get("max_length_seconds"),
        DEFAULT_AUDIO_SPLITTER_SETTINGS["max_length_seconds"],
        5,
        600,
        int,
    )
    tolerance = _number(
        source.get("tolerance_seconds"),
        maximum,
        maximum,
        3600,
        int,
    )
    return {
        "max_length_seconds": maximum,
        "tolerance_seconds": tolerance,
        "min_silence_ms": _number(
            source.get("min_silence_ms"),
            DEFAULT_AUDIO_SPLITTER_SETTINGS["min_silence_ms"],
            100,
            5000,
            int,
        ),
        "silence_offset_db": _number(
            source.get("silence_offset_db"),
            DEFAULT_AUDIO_SPLITTER_SETTINGS["silence_offset_db"],
            1,
            60,
            float,
        ),
        "extensions": _extensions(source.get("extensions")),
        "recursive": bool(
            source.get("recursive", DEFAULT_AUDIO_SPLITTER_SETTINGS["recursive"])
        ),
        "include_source_name": bool(
            source.get(
                "include_source_name",
                DEFAULT_AUDIO_SPLITTER_SETTINGS["include_source_name"],
            )
        ),
    }


def settings_from_config(config):
    source = config.get(AUDIO_SPLITTER_CONFIG_KEY, {}) if isinstance(config, dict) else {}
    if not isinstance(source, dict):
        source = {}
    # Compatibility with experimental/local keys that may have been used
    # before this feature became a plugin.
    if "max_length_seconds" not in source and isinstance(config, dict):
        for key in ("split_audio_max_length_seconds", "split_audio_length_seconds"):
            if key in config:
                source = dict(source)
                source["max_length_seconds"] = config[key]
                break
    return normalize_audio_splitter_settings(source)


def _path_key(path):
    return os.path.normcase(os.path.abspath(str(path)))


def is_generated_chunk(path):
    return bool(_GENERATED_CHUNK_PATTERN.search(Path(path).stem))


def collect_audio_files(paths, settings):
    normalized = normalize_audio_splitter_settings(settings)
    supported = set(normalized["extensions"])
    recursive = normalized["recursive"]
    result = []
    seen = set()
    for raw_path in paths or ():
        path = Path(str(raw_path)).expanduser()
        candidates = []
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            iterator = path.rglob("*") if recursive else path.glob("*")
            candidates = [candidate for candidate in iterator if candidate.is_file()]
        for candidate in candidates:
            if candidate.suffix.casefold() not in supported or is_generated_chunk(candidate):
                continue
            key = _path_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            result.append(candidate)
    return result


def merge_split_points(points, max_length_ms):
    if not points or len(points) < 2:
        return points
    result = [points[0]]
    begin = points[0]
    previous = points[1]
    for point in points[1:]:
        if point - begin > max_length_ms:
            result.append(previous)
            begin = previous
        previous = point
    if result[-1] != points[-1]:
        result.append(points[-1])
    return result


def split_audio_file(input_file, settings, progress_callback=None):
    settings = normalize_audio_splitter_settings(settings)
    path = Path(input_file)
    max_length_ms = settings["max_length_seconds"] * 1000
    tolerance_ms = settings["tolerance_seconds"] * 1000

    def report(message):
        if progress_callback is not None:
            progress_callback(str(message))

    report(f"读取音频：{path}")
    audio = AudioSegment.from_file(path)
    duration_ms = len(audio)
    if duration_ms <= max_length_ms:
        return {
            "path": str(path),
            "status": "skipped",
            "reason": f"时长 {duration_ms / 1000:.2f} 秒，未超过分段上限",
            "outputs": [],
        }
    if duration_ms <= tolerance_ms:
        return {
            "path": str(path),
            "status": "skipped",
            "reason": f"时长 {duration_ms / 1000:.2f} 秒，在容忍范围内",
            "outputs": [],
        }

    threshold = audio.dBFS - settings["silence_offset_db"]
    if not math.isfinite(threshold):
        threshold = -50.0
    silent_ranges = silence.detect_silence(
        audio,
        min_silence_len=settings["min_silence_ms"],
        silence_thresh=threshold,
    )
    split_points = [0]
    for _start, end in silent_ranges:
        if end - split_points[-1] > max_length_ms:
            split_points.append(split_points[-1] + max_length_ms)
        split_points.append(end)
    split_points.append(duration_ms)
    split_points = merge_split_points(split_points, max_length_ms)

    prefix = (
        f"{path.stem}_片段"
        if settings["include_source_name"]
        else "片段"
    )
    output_paths = []
    index = 0
    for start, end in zip(split_points, split_points[1:]):
        chunk = audio[start:end]
        for sub_index in range(math.ceil(len(chunk) / max_length_ms)):
            sub = chunk[
                sub_index * max_length_ms:(sub_index + 1) * max_length_ms
            ]
            if not len(sub):
                continue
            index += 1
            output_path = path.parent / f"{prefix}{index}.mp3"
            sub.export(
                output_path,
                format="mp3",
                bitrate="320k",
                parameters=["-q:a", "0"],
            )
            output_paths.append(str(output_path))
            report(f"已保存：{output_path}")
    return {
        "path": str(path),
        "status": "split",
        "reason": "",
        "outputs": output_paths,
    }


class AudioSplitWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(str)
    file_status = QtCore.pyqtSignal(str, str)
    completed = QtCore.pyqtSignal(dict)

    def __init__(self, paths, settings, parent=None):
        super().__init__(parent)
        self.paths = [str(path) for path in paths or ()]
        self.settings = normalize_audio_splitter_settings(settings)

    def run(self):
        files = collect_audio_files(self.paths, self.settings)
        result = {
            "source_count": len(self.paths),
            "file_count": len(files),
            "split_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "chunk_count": 0,
            "files": [],
        }
        self.progress.emit(f"找到 {len(files)} 个待检查音频文件。")
        for index, path in enumerate(files, start=1):
            if self.isInterruptionRequested():
                result["cancelled"] = True
                break
            self.file_status.emit(str(path), f"处理中（{index}/{len(files)}）")
            try:
                item = split_audio_file(path, self.settings, self.progress.emit)
            except Exception as error:
                item = {
                    "path": str(path),
                    "status": "failed",
                    "reason": f"{type(error).__name__}: {error}",
                    "outputs": [],
                }
                self.progress.emit(f"处理失败：{path} -> {item['reason']}")
            status = item["status"]
            result[f"{status}_count"] += 1
            result["chunk_count"] += len(item.get("outputs", []))
            result["files"].append(item)
            label = {
                "split": f"已切分 {len(item.get('outputs', []))} 段",
                "skipped": item.get("reason") or "无需切分",
                "failed": "失败：" + str(item.get("reason") or "未知错误"),
            }[status]
            self.file_status.emit(str(path), label)
        self.completed.emit(result)


class DropPathListWidget(QtWidgets.QListWidget):
    paths_dropped = QtCore.pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DropOnly)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.paths_dropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class AudioSplitBatchDialog(QtWidgets.QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self._force_close = False
        self.setWindowTitle("批量切分音频")
        self.resize(760, 520)
        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel(
            "把一个或多个音频文件、或者包含音频的文件夹拖到下面。"
            "支持的格式和切分参数在“程序设置 → 切分音频插件”中维护。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.path_list = DropPathListWidget(self)
        self.path_list.setToolTip("可同时拖入文件和文件夹；重复路径会自动忽略。")
        self.path_list.paths_dropped.connect(self.add_paths)
        self.path_list.itemDoubleClicked.connect(self.open_item)
        layout.addWidget(self.path_list, 1)

        edit_buttons = QtWidgets.QHBoxLayout()
        add_files = QtWidgets.QPushButton("添加文件…", self)
        add_folder = QtWidgets.QPushButton("添加文件夹…", self)
        remove_selected = QtWidgets.QPushButton("移除选中", self)
        clear = QtWidgets.QPushButton("清空", self)
        add_files.clicked.connect(self.choose_files)
        add_folder.clicked.connect(self.choose_folder)
        remove_selected.clicked.connect(self.remove_selected)
        clear.clicked.connect(self.path_list.clear)
        for button in (add_files, add_folder, remove_selected, clear):
            edit_buttons.addWidget(button)
        edit_buttons.addStretch(1)
        layout.addLayout(edit_buttons)

        self.status_label = QtWidgets.QLabel("尚未开始", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        action_buttons = QtWidgets.QHBoxLayout()
        action_buttons.addStretch(1)
        self.start_button = QtWidgets.QPushButton("开始批量切分", self)
        close_button = QtWidgets.QPushButton("关闭", self)
        self.start_button.clicked.connect(self.start_split)
        close_button.clicked.connect(self.hide)
        action_buttons.addWidget(self.start_button)
        action_buttons.addWidget(close_button)
        layout.addLayout(action_buttons)

    def paths(self):
        return [
            str(self.path_list.item(index).data(QtCore.Qt.UserRole) or "")
            for index in range(self.path_list.count())
        ]

    def add_paths(self, paths):
        existing = {_path_key(path) for path in self.paths()}
        for raw_path in paths or ():
            path = Path(str(raw_path)).expanduser()
            if not path.exists():
                continue
            key = _path_key(path)
            if key in existing:
                continue
            item = QtWidgets.QListWidgetItem(str(path))
            item.setData(QtCore.Qt.UserRole, str(path))
            item.setToolTip(str(path))
            self.path_list.addItem(item)
            existing.add(key)

    def choose_files(self):
        extensions = normalize_audio_splitter_settings(
            self.plugin.settings
        )["extensions"]
        patterns = " ".join(f"*{extension}" for extension in extensions)
        paths, _selected_filter = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "选择音频文件",
            "",
            f"音频文件 ({patterns});;所有文件 (*)",
        )
        self.add_paths(paths)

    def choose_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择包含音频的文件夹")
        if path:
            self.add_paths([path])

    def remove_selected(self):
        for item in self.path_list.selectedItems():
            self.path_list.takeItem(self.path_list.row(item))

    def open_item(self, item):
        path = Path(str(item.data(QtCore.Qt.UserRole) or ""))
        target = path if path.is_dir() else path.parent
        if target.exists():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target.resolve())))

    def start_split(self):
        paths = self.paths()
        if not paths:
            QtWidgets.QMessageBox.information(self, "批量切分音频", "请先拖入文件或文件夹。")
            return
        self.plugin.start_paths(paths, origin="批量窗口")

    def set_busy(self, busy, message=""):
        self.start_button.setEnabled(not busy)
        self.start_button.setText("正在切分…" if busy else "开始批量切分")
        if message:
            self.status_label.setText(str(message))

    def update_file_status(self, path, status):
        key = _path_key(path)
        for index in range(self.path_list.count()):
            item = self.path_list.item(index)
            item_path = str(item.data(QtCore.Qt.UserRole) or "")
            if _path_key(item_path) == key:
                item.setText(f"{item_path}    [{status}]")
                break
        self.status_label.setText(status)

    def shutdown(self):
        self._force_close = True
        self.close()

    def closeEvent(self, event):
        if self._force_close:
            event.accept()
        else:
            event.ignore()
            self.hide()


class AudioSplitterSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        title = QtWidgets.QLabel("切分音频", self.widget)
        title.setStyleSheet("font-weight:600;font-size:14px;")
        layout.addWidget(title)
        form = QtWidgets.QFormLayout()
        self.max_length_spin = QtWidgets.QSpinBox(self.widget)
        self.max_length_spin.setRange(5, 600)
        self.max_length_spin.setSuffix(" 秒")
        self.tolerance_spin = QtWidgets.QSpinBox(self.widget)
        self.tolerance_spin.setRange(5, 3600)
        self.tolerance_spin.setSuffix(" 秒")
        self.min_silence_spin = QtWidgets.QSpinBox(self.widget)
        self.min_silence_spin.setRange(100, 5000)
        self.min_silence_spin.setSuffix(" ms")
        self.silence_offset_spin = QtWidgets.QDoubleSpinBox(self.widget)
        self.silence_offset_spin.setRange(1, 60)
        self.silence_offset_spin.setDecimals(1)
        self.silence_offset_spin.setSuffix(" dB")
        self.extensions_edit = QtWidgets.QLineEdit(self.widget)
        self.recursive_checkbox = QtWidgets.QCheckBox("递归检查文件夹中的子目录", self.widget)
        self.include_source_name_checkbox = QtWidgets.QCheckBox(
            "输出文件名包含源文件名（避免同目录多个音频相互覆盖）",
            self.widget,
        )
        form.addRow("每段最大时长：", self.max_length_spin)
        form.addRow("短文件容忍上限：", self.tolerance_spin)
        form.addRow("最短静音长度：", self.min_silence_spin)
        form.addRow("静音阈值偏移：", self.silence_offset_spin)
        form.addRow("输入扩展名：", self.extensions_edit)
        form.addRow("扫描方式：", self.recursive_checkbox)
        form.addRow("输出命名：", self.include_source_name_checkbox)
        layout.addLayout(form)
        hint = QtWidgets.QLabel(
            "只有达到容忍上限的音频才会切分；切点优先放在静音结束处，"
            "仍然过长时才按最大时长强制切开。输出为源目录下的 320 kbps MP3。",
            self.widget,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        layout.addWidget(hint)
        layout.addStretch(1)

    def load_config(self, config):
        settings = settings_from_config(config)
        self.max_length_spin.setValue(settings["max_length_seconds"])
        self.tolerance_spin.setValue(settings["tolerance_seconds"])
        self.min_silence_spin.setValue(settings["min_silence_ms"])
        self.silence_offset_spin.setValue(settings["silence_offset_db"])
        self.extensions_edit.setText(", ".join(settings["extensions"]))
        self.recursive_checkbox.setChecked(settings["recursive"])
        self.include_source_name_checkbox.setChecked(settings["include_source_name"])

    def validate(self):
        if self.tolerance_spin.value() < self.max_length_spin.value():
            raise ValueError("短文件容忍上限不能小于每段最大时长")
        if not _extensions(self.extensions_edit.text()):
            raise ValueError("请至少填写一个输入扩展名")

    def update_config(self, config):
        self.validate()
        config[AUDIO_SPLITTER_CONFIG_KEY] = normalize_audio_splitter_settings({
            "max_length_seconds": self.max_length_spin.value(),
            "tolerance_seconds": self.tolerance_spin.value(),
            "min_silence_ms": self.min_silence_spin.value(),
            "silence_offset_db": self.silence_offset_spin.value(),
            "extensions": self.extensions_edit.text(),
            "recursive": self.recursive_checkbox.isChecked(),
            "include_source_name": self.include_source_name_checkbox.isChecked(),
        })


class AudioSplitterPlugin:
    plugin_id = "audio_splitter"
    display_name = "切分音频"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.settings = dict(DEFAULT_AUDIO_SPLITTER_SETTINGS)
        self.dialog = None
        self.worker = None
        self.worker_origin = ""

    def register(self, context):
        self.context = context
        context.register_command(PluginCommand(
            command_id="batch",
            title="批量切分音频…",
            callback=lambda _rows: self.open_batch_dialog(),
            locations=frozenset({TOOLS_MENU}),
            tooltip="拖入多个音频文件或文件夹后批量切分",
            order=30,
        ))
        context.register_command(PluginCommand(
            command_id="task",
            title="切分任务音频…",
            callback=self.split_task_rows,
            locations=frozenset({TASK_CONTEXT_MENU}),
            tooltip="递归切分所选任务目录中的音频文件",
            order=20,
            enabled=lambda rows: bool(rows),
        ))
        context.register_settings_page(PluginSettingsPage(
            page_id="settings",
            title="切分音频插件",
            factory=AudioSplitterSettingsPage,
            order=220,
        ))

    def start(self):
        self.settings = settings_from_config(self.context.load_config())
        self.dialog = AudioSplitBatchDialog(self, self.context.parent_widget)

    def apply_settings(self, config):
        self.settings = settings_from_config(config)
        return True

    def update_config(self, config):
        config[AUDIO_SPLITTER_CONFIG_KEY] = normalize_audio_splitter_settings(self.settings)

    def open_batch_dialog(self):
        if self.dialog is None:
            self.dialog = AudioSplitBatchDialog(self, self.context.parent_widget)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def split_task_rows(self, rows):
        targets = self.context.task_targets(rows)
        paths = [target["target_dir"] for target in targets]
        if not paths:
            return False
        return self.start_paths(paths, origin=f"所选 {len(paths)} 个任务")

    def start_paths(self, paths, origin=""):
        if self.worker is not None and self.worker.isRunning():
            QtWidgets.QMessageBox.information(
                self.context.parent_widget,
                "切分音频",
                "已有切分任务正在运行，请等待完成。",
            )
            return False
        candidates = collect_audio_files(paths, self.settings)
        if not candidates:
            QtWidgets.QMessageBox.information(
                self.context.parent_widget,
                "切分音频",
                "没有找到符合插件设置的音频文件。",
            )
            if self.dialog is not None:
                self.dialog.set_busy(False, "没有找到符合插件设置的音频文件。")
            return False
        self.worker_origin = str(origin or "切分音频")
        worker = AudioSplitWorker(paths, self.settings, self.context.parent_widget)
        worker.progress.connect(self.context.log)
        worker.file_status.connect(self._on_file_status)
        worker.completed.connect(self._on_completed)
        worker.finished.connect(self._on_finished)
        self.worker = worker
        if self.dialog is not None:
            self.dialog.set_busy(True, f"准备处理 {len(candidates)} 个音频文件……")
        self.context.log(f"{self.worker_origin}：开始检查 {len(candidates)} 个音频文件。")
        worker.start()
        return True

    def _on_file_status(self, path, status):
        if self.dialog is not None:
            self.dialog.update_file_status(path, status)

    def _on_completed(self, result):
        summary = (
            f"完成：切分 {result.get('split_count', 0)} 个文件，"
            f"生成 {result.get('chunk_count', 0)} 个片段，"
            f"跳过 {result.get('skipped_count', 0)} 个，"
            f"失败 {result.get('failed_count', 0)} 个。"
        )
        self.context.log(f"{self.worker_origin}：{summary}")
        if self.dialog is not None:
            self.dialog.set_busy(False, summary)
        self.context.notify(
            "切分音频完成" if not result.get("failed_count") else "切分音频有失败项",
            summary,
            critical=bool(result.get("failed_count")),
        )

    def _on_finished(self):
        worker = self.worker
        self.worker = None
        if self.dialog is not None:
            self.dialog.set_busy(False)
        if worker is not None:
            worker.deleteLater()

    def can_close(self):
        if self.worker is not None and self.worker.isRunning():
            return False, "切分音频插件仍在处理文件，请等待完成后再退出。"
        return True, ""

    def stop(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait(5000)
        self.worker = None
        if self.dialog is not None:
            self.dialog.shutdown()
            self.dialog.deleteLater()
            self.dialog = None
