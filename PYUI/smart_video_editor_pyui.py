import copy
import shutil
import time
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from model.SmartVideoEditor import (
    apply_clip_review,
    format_smart_video_export_blockers,
    render_task_problem_report,
    save_analysis_reports,
    set_smart_video_missing_review,
    smart_video_export_blockers,
    smart_video_missing_findings,
)


STATUS_TEXT = {
    "green": "通过",
    "orange": "需核对",
    "pink": "严重异常",
}
STATUS_COLORS = {
    "green": "#D9EAD3",
    "orange": "#FCE5CD",
    "pink": "#F4CCCC",
}


def _read_only_item(text, data=None):
    item = QtWidgets.QTableWidgetItem(str(text or ""))
    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
    if data is not None:
        item.setData(QtCore.Qt.UserRole, data)
    return item


class SmartVideoSourceDialog(QtWidgets.QDialog):
    """Let the user confirm which task clips belong to this edit run."""

    def __init__(self, jobs, parent=None):
        super().__init__(parent)
        self.jobs = copy.deepcopy(list(jobs))
        self.selected_jobs = []
        self._changing_checks = False
        self.setWindowTitle("智能剪辑：选择视频片段")
        self.resize(900, 560)

        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            "程序会按每个视频实际朗读的文案位置重新排序，不依赖文件名或当前列表顺序。"
            "取消勾选不属于本任务的文件；"
            "双击文件可先用系统默认播放器查看。原视频不会被覆盖。",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.tree = QtWidgets.QTreeWidget(self)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["任务 / 视频", "任务文案", "所在目录"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemDoubleClicked.connect(self._open_item)
        layout.addWidget(self.tree, 1)

        for job_index, job in enumerate(self.jobs):
            script = str(job.get("script") or "").strip()
            parent_item = QtWidgets.QTreeWidgetItem([
                str(job.get("label") or job.get("task_id") or "任务"),
                script[:120],
                str(job.get("task_dir") or ""),
            ])
            parent_item.setData(0, QtCore.Qt.UserRole, ("task", job_index))
            parent_item.setFlags(parent_item.flags() | QtCore.Qt.ItemIsUserCheckable)
            parent_item.setCheckState(0, QtCore.Qt.Checked)
            if not script:
                parent_item.setForeground(0, QtGui.QBrush(QtGui.QColor("#B3261E")))
                parent_item.setToolTip(0, "没有任务语音文案，无法进行可靠核对")
            self.tree.addTopLevelItem(parent_item)
            for source_index, source in enumerate(job.get("sources", [])):
                path = Path(source)
                child = QtWidgets.QTreeWidgetItem([
                    path.name,
                    "",
                    str(path.parent),
                ])
                child.setData(
                    0,
                    QtCore.Qt.UserRole,
                    ("source", job_index, source_index, str(path)),
                )
                child.setFlags(child.flags() | QtCore.Qt.ItemIsUserCheckable)
                child.setCheckState(0, QtCore.Qt.Checked)
                child.setToolTip(0, str(path))
                parent_item.addChild(child)
            if not job.get("sources"):
                missing = QtWidgets.QTreeWidgetItem(["没有找到可用视频", "", ""])
                missing.setForeground(0, QtGui.QBrush(QtGui.QColor("#B3261E")))
                parent_item.addChild(missing)
                parent_item.setCheckState(0, QtCore.Qt.Unchecked)
        self.tree.expandAll()
        header = self.tree.header()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            parent=self,
        )
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("开始分析")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_item_changed(self, item, column):
        if self._changing_checks or column != 0:
            return
        data = item.data(0, QtCore.Qt.UserRole)
        if not data:
            return
        self._changing_checks = True
        try:
            if data[0] == "task":
                state = item.checkState(0)
                for child_index in range(item.childCount()):
                    child = item.child(child_index)
                    if child.data(0, QtCore.Qt.UserRole):
                        child.setCheckState(0, state)
            elif data[0] == "source":
                parent = item.parent()
                states = [
                    parent.child(index).checkState(0)
                    for index in range(parent.childCount())
                    if parent.child(index).data(0, QtCore.Qt.UserRole)
                ]
                if states and all(state == QtCore.Qt.Checked for state in states):
                    parent.setCheckState(0, QtCore.Qt.Checked)
                elif states and all(state == QtCore.Qt.Unchecked for state in states):
                    parent.setCheckState(0, QtCore.Qt.Unchecked)
                else:
                    parent.setCheckState(0, QtCore.Qt.PartiallyChecked)
        finally:
            self._changing_checks = False

    def _open_item(self, item, _column):
        data = item.data(0, QtCore.Qt.UserRole)
        if not data or data[0] != "source":
            return
        path = Path(data[3])
        if not path.is_file():
            QtWidgets.QMessageBox.warning(self, "打开视频", f"文件不存在：\n{path}")
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path.resolve())))

    def accept(self):
        selected = []
        missing_scripts = []
        for job_index, job in enumerate(self.jobs):
            parent_item = self.tree.topLevelItem(job_index)
            sources = []
            for child_index in range(parent_item.childCount()):
                child = parent_item.child(child_index)
                data = child.data(0, QtCore.Qt.UserRole)
                if data and child.checkState(0) == QtCore.Qt.Checked:
                    sources.append(data[3])
            if not sources:
                continue
            if not str(job.get("script") or "").strip():
                missing_scripts.append(str(job.get("label") or job.get("task_id")))
                continue
            selected_job = copy.deepcopy(job)
            selected_job["sources"] = sources
            selected.append(selected_job)
        if missing_scripts:
            QtWidgets.QMessageBox.warning(
                self,
                "缺少任务文案",
                "以下任务没有语音文案，不能进行智能核对：\n"
                + "\n".join(missing_scripts),
            )
            return
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "没有视频", "请至少勾选一个带任务文案的视频。"
            )
            return
        self.selected_jobs = selected
        super().accept()

    @classmethod
    def get_jobs(cls, jobs, parent=None):
        dialog = cls(jobs, parent)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            return dialog.selected_jobs
        return None


class SmartVideoPendingDialog(QtWidgets.QDialog):
    """Browse persisted missing-segment tasks and request a fresh analysis."""

    def __init__(self, records, parent=None):
        super().__init__(parent)
        self.records = copy.deepcopy(list(records or []))
        self.action = ""
        self.selected_records = []
        self.setWindowTitle("待处理智能剪辑")
        self.resize(1050, 560)

        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            "这里保留了上次没有处理或选择暂缓的缺段任务。"
            "选择任务后可重新加载视频并分析；双击可打开任务目录。",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.tree = QtWidgets.QTreeWidget(self)
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels([
            "状态", "记录时间", "任务", "检测到的缺段", "任务目录",
        ])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.itemDoubleClicked.connect(self.open_selected_directory)
        layout.addWidget(self.tree, 1)
        for index, record in enumerate(self.records):
            missing_text = " / ".join(
                str(item.get("text") or "").replace("\n", " / ")
                for item in record.get("missing_blocks", [])
            )
            updated_at = int(record.get("updated_at") or 0)
            timestamp = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(updated_at))
                if updated_at else "—"
            )
            row = QtWidgets.QTreeWidgetItem([
                "已暂缓" if record.get("status") == "skipped" else "待处理",
                timestamp,
                str(record.get("label") or record.get("task_id") or "任务"),
                missing_text,
                str(record.get("task_dir") or ""),
            ])
            row.setData(0, QtCore.Qt.UserRole, index)
            if record.get("status") == "skipped":
                row.setBackground(0, QtGui.QColor(STATUS_COLORS["orange"]))
            else:
                row.setBackground(0, QtGui.QColor(STATUS_COLORS["pink"]))
            self.tree.addTopLevelItem(row)
        header = self.tree.header()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

        buttons = QtWidgets.QHBoxLayout()
        open_button = QtWidgets.QPushButton("打开任务目录", self)
        remove_button = QtWidgets.QPushButton("移除所选记录", self)
        reanalyze_button = QtWidgets.QPushButton("重新加载视频并分析", self)
        close_button = QtWidgets.QPushButton("关闭", self)
        open_button.clicked.connect(self.open_selected_directory)
        remove_button.clicked.connect(lambda: self.choose_action("remove"))
        reanalyze_button.clicked.connect(lambda: self.choose_action("reanalyze"))
        close_button.clicked.connect(self.reject)
        buttons.addWidget(open_button)
        buttons.addWidget(remove_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        buttons.addWidget(reanalyze_button)
        layout.addLayout(buttons)

    def selected_record_values(self):
        rows = self.tree.selectedItems()
        if not rows and self.tree.currentItem() is not None:
            rows = [self.tree.currentItem()]
        indexes = sorted({
            int(row.data(0, QtCore.Qt.UserRole)) for row in rows
            if row.data(0, QtCore.Qt.UserRole) is not None
        })
        return [self.records[index] for index in indexes]

    def open_selected_directory(self, *_args):
        records = self.selected_record_values()
        if not records:
            QtWidgets.QMessageBox.information(self, "打开目录", "请先选择一个任务。")
            return
        path = Path(records[0].get("task_dir") or "")
        if not path.is_dir():
            QtWidgets.QMessageBox.warning(self, "打开目录", f"任务目录不存在：\n{path}")
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path.resolve())))

    def choose_action(self, action):
        records = self.selected_record_values()
        if not records:
            QtWidgets.QMessageBox.information(self, "待处理智能剪辑", "请先选择任务。")
            return
        self.action = str(action)
        self.selected_records = records
        self.accept()


class SmartVideoReviewDialog(QtWidgets.QDialog):
    COL_INCLUDE = 0
    COL_ORDER = 1
    COL_TASK = 2
    COL_FILE = 3
    COL_STATUS = 4
    COL_SCORE = 5
    COL_START = 6
    COL_END = 7
    COL_REMOVED = 8
    COL_ISSUES = 9
    COL_EXPECTED = 10
    COL_RECOGNIZED = 11

    def __init__(self, bundle, parent=None):
        super().__init__(parent)
        self.bundle = copy.deepcopy(bundle)
        self.reviewed_bundle = None
        self._loading_detail = False
        self.setWindowTitle("智能剪辑：视频与文案核对")
        self.resize(1480, 860)

        layout = QtWidgets.QVBoxLayout(self)
        summary = self.bundle.get("summary", {})
        label = QtWidgets.QLabel(
            "共 {clip_count} 个片段：通过 {green_count}，需核对 {orange_count}，"
            "严重异常 {pink_count}，确认缺段 {missing_count} 段，"
            "识别边界待核对 {unverified_count} 处。"
            "橘色表示有明显单词差异；粉色必须人工确认。"
            "自动排序不合适时，可直接修改“顺序”；也可修改起止时间或取消问题片段。".format(**{
                "clip_count": summary.get("clip_count", 0),
                "green_count": summary.get("green_count", 0),
                "orange_count": summary.get("orange_count", 0),
                "pink_count": summary.get("pink_count", 0),
                "missing_count": summary.get("missing_count", 0),
                "unverified_count": summary.get("unverified_count", 0),
            }),
            self,
        )
        label.setWordWrap(True)
        layout.addWidget(label)

        self.blocker_banner = QtWidgets.QLabel(self)
        self.blocker_banner.setWordWrap(True)
        self.blocker_banner.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.blocker_banner.setStyleSheet(
            "QLabel { background:#B3261E; color:white; border-radius:4px; "
            "padding:10px; font-weight:600; }"
        )
        layout.addWidget(self.blocker_banner)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical, self)
        self.table = QtWidgets.QTableWidget(self.main_splitter)
        self.table.setColumnCount(12)
        self.table.setHorizontalHeaderLabels([
            "生成", "顺序", "任务", "视频片段", "结果", "相似度", "起点/秒", "终点/秒",
            "预计删除/秒", "问题", "任务正确文案", "视频识别内容",
        ])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.cellDoubleClicked.connect(self._cell_double_clicked)
        self.table.currentCellChanged.connect(self._load_clip_detail)
        rows = [
            (task_index, clip_index, task, clip)
            for task_index, task in enumerate(self.bundle.get("tasks", []))
            for clip_index, clip in enumerate(task.get("clips", []))
        ]
        self.table.setRowCount(len(rows))
        for row, (task_index, clip_index, task, clip) in enumerate(rows):
            include_item = QtWidgets.QTableWidgetItem()
            include_item.setFlags(
                (include_item.flags() | QtCore.Qt.ItemIsUserCheckable)
                & ~QtCore.Qt.ItemIsEditable
            )
            include_item.setCheckState(
                QtCore.Qt.Checked if clip.get("included", True) else QtCore.Qt.Unchecked
            )
            include_item.setData(QtCore.Qt.UserRole, (task_index, clip_index))
            self.table.setItem(row, self.COL_INCLUDE, include_item)
            self.table.setItem(
                row,
                self.COL_ORDER,
                QtWidgets.QTableWidgetItem(str(clip.get("export_order", clip_index + 1))),
            )
            self.table.setItem(row, self.COL_TASK, _read_only_item(task.get("label", "")))
            self.table.setItem(
                row,
                self.COL_FILE,
                _read_only_item(clip.get("file_name", ""), clip.get("source", "")),
            )
            status = str(clip.get("status") or "pink")
            status_item = _read_only_item(STATUS_TEXT.get(status, status))
            status_item.setBackground(QtGui.QColor(STATUS_COLORS.get(status, "#FFFFFF")))
            status_item.setToolTip(str(clip.get("issue_reason") or ""))
            self.table.setItem(row, self.COL_STATUS, status_item)
            self.table.setItem(
                row,
                self.COL_SCORE,
                _read_only_item(f"{float(clip.get('similarity') or 0.0) * 100:.1f}%"),
            )
            self.table.setItem(row, self.COL_START, QtWidgets.QTableWidgetItem(
                f"{float(clip.get('trim_start') or 0.0):.3f}"
            ))
            self.table.setItem(row, self.COL_END, QtWidgets.QTableWidgetItem(
                f"{float(clip.get('trim_end') or 0.0):.3f}"
            ))
            self.table.setItem(
                row,
                self.COL_REMOVED,
                _read_only_item(f"{float(clip.get('removed_seconds') or 0.0):.2f}"),
            )
            problem_count = sum(
                issue.get("severity") != "info"
                for issue in clip.get("issues", [])
            )
            self.table.setItem(
                row,
                self.COL_ISSUES,
                _read_only_item(str(problem_count) if problem_count else "—"),
            )
            self.table.setItem(
                row, self.COL_EXPECTED, QtWidgets.QTableWidgetItem(clip.get("expected_text", ""))
            )
            recognized = _read_only_item(clip.get("recognized_text", ""))
            recognized.setToolTip(str(clip.get("recognized_text") or ""))
            self.table.setItem(row, self.COL_RECOGNIZED, recognized)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_FILE, QtWidgets.QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnHidden(self.COL_EXPECTED, True)
        self.table.setColumnHidden(self.COL_RECOGNIZED, True)
        self.main_splitter.addWidget(self.table)

        self.detail_tabs = QtWidgets.QTabWidget(self.main_splitter)
        clip_detail = QtWidgets.QWidget(self.detail_tabs)
        clip_detail_layout = QtWidgets.QVBoxLayout(clip_detail)
        clip_detail_layout.setContentsMargins(7, 7, 7, 7)
        self.detail_title = QtWidgets.QLabel("请在上方选择一个视频片段", clip_detail)
        self.detail_title.setWordWrap(True)
        self.detail_title.setStyleSheet("font-weight:600;")
        clip_detail_layout.addWidget(self.detail_title)

        text_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal, clip_detail)
        expected_group = QtWidgets.QGroupBox("任务正确文案（可修改）", text_splitter)
        expected_layout = QtWidgets.QVBoxLayout(expected_group)
        self.detail_expected = QtWidgets.QPlainTextEdit(expected_group)
        self.detail_expected.setPlaceholderText("此片段没有可靠对应文案")
        self.detail_expected.textChanged.connect(self._detail_expected_changed)
        expected_layout.addWidget(self.detail_expected)
        recognized_group = QtWidgets.QGroupBox("视频实际识别内容（只读）", text_splitter)
        recognized_layout = QtWidgets.QVBoxLayout(recognized_group)
        self.detail_recognized = QtWidgets.QPlainTextEdit(recognized_group)
        self.detail_recognized.setReadOnly(True)
        recognized_layout.addWidget(self.detail_recognized)
        text_splitter.addWidget(expected_group)
        text_splitter.addWidget(recognized_group)
        text_splitter.setSizes([700, 700])
        clip_detail_layout.addWidget(text_splitter, 1)

        issue_header = QtWidgets.QHBoxLayout()
        issue_header.addWidget(QtWidgets.QLabel(
            "具体问题与自动裁切依据（双击问题可试听附近时间）", clip_detail
        ))
        issue_header.addStretch(1)
        self.preview_issue_button = QtWidgets.QPushButton("试听选中问题", clip_detail)
        self.preview_issue_button.clicked.connect(self.preview_selected_issue)
        issue_header.addWidget(self.preview_issue_button)
        clip_detail_layout.addLayout(issue_header)
        self.issue_tree = QtWidgets.QTreeWidget(clip_detail)
        self.issue_tree.setColumnCount(7)
        self.issue_tree.setHeaderLabels([
            "级别", "时间", "文案行", "问题类型", "正确内容", "识别内容", "具体说明",
        ])
        self.issue_tree.setRootIsDecorated(False)
        self.issue_tree.setAlternatingRowColors(True)
        self.issue_tree.itemDoubleClicked.connect(
            lambda _item, _column: self.preview_selected_issue()
        )
        issue_tree_header = self.issue_tree.header()
        issue_tree_header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        issue_tree_header.setSectionResizeMode(6, QtWidgets.QHeaderView.Stretch)
        clip_detail_layout.addWidget(self.issue_tree, 2)
        self.detail_tabs.addTab(clip_detail, "所选片段完整核对")

        missing_page = QtWidgets.QWidget(self.detail_tabs)
        missing_layout = QtWidgets.QVBoxLayout(missing_page)
        self.missing_tree = QtWidgets.QTreeWidget(missing_page)
        self.missing_tree.setColumnCount(5)
        self.missing_tree.setHeaderLabels([
            "级别", "任务", "原文位置", "未覆盖原文", "相邻片段与说明",
        ])
        self.missing_tree.setRootIsDecorated(False)
        missing_header = self.missing_tree.header()
        missing_header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        missing_header.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        missing_header.setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)
        missing_layout.addWidget(self.missing_tree)
        missing_actions = QtWidgets.QHBoxLayout()
        self.approve_missing_button = QtWidgets.QPushButton(
            "人工确认：这个任务没问题", missing_page
        )
        self.skip_missing_button = QtWidgets.QPushButton(
            "暂缓这个任务，先导出其余任务", missing_page
        )
        self.clear_missing_decision_button = QtWidgets.QPushButton(
            "清除所选决定", missing_page
        )
        self.approve_missing_button.setToolTip(
            "只在你试听后确认识别误报时使用；视频选择或裁切变化后，本决定自动失效"
        )
        self.skip_missing_button.setToolTip(
            "这个任务本次不导出，并保存到“工具 → 待处理智能剪辑”"
        )
        self.approve_missing_button.clicked.connect(
            lambda: self._set_missing_decision("approved")
        )
        self.skip_missing_button.clicked.connect(
            lambda: self._set_missing_decision("skipped")
        )
        self.clear_missing_decision_button.clicked.connect(
            lambda: self._set_missing_decision("")
        )
        missing_actions.addWidget(self.approve_missing_button)
        missing_actions.addWidget(self.skip_missing_button)
        missing_actions.addWidget(self.clear_missing_decision_button)
        missing_actions.addStretch(1)
        missing_layout.addLayout(missing_actions)
        self.coverage_tab_index = self.detail_tabs.addTab(
            missing_page,
            "缺段 / 识别未覆盖",
        )
        self.main_splitter.addWidget(self.detail_tabs)
        self.main_splitter.setSizes([330, 420])
        layout.addWidget(self.main_splitter, 1)

        hint = QtWidgets.QLabel(
            "双击“视频片段”可播放原视频。识别内容只用于校时和核对；"
            "最终 SRT 使用“任务正确文案”。点击取消不会丢失分析报告，下次会复用识别缓存。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        layout.addWidget(hint)

        button_layout = QtWidgets.QHBoxLayout()
        open_button = QtWidgets.QPushButton("打开所选视频", self)
        report_button = QtWidgets.QPushButton("打开可读问题报告", self)
        copy_report_button = QtWidgets.QPushButton("复制全部问题", self)
        save_button = QtWidgets.QPushButton("保存核对结果", self)
        cancel_button = QtWidgets.QPushButton("取消", self)
        self.export_button = QtWidgets.QPushButton("确认并生成视频与 SRT", self)
        self.export_button.setDefault(True)
        open_button.clicked.connect(self.open_selected_video)
        report_button.clicked.connect(self.open_selected_report)
        copy_report_button.clicked.connect(self.copy_problem_report)
        save_button.clicked.connect(self.save_review)
        cancel_button.clicked.connect(self.reject)
        self.export_button.clicked.connect(self.accept)
        button_layout.addWidget(open_button)
        button_layout.addWidget(report_button)
        button_layout.addWidget(copy_report_button)
        button_layout.addWidget(save_button)
        button_layout.addStretch(1)
        button_layout.addWidget(cancel_button)
        button_layout.addWidget(self.export_button)
        layout.addLayout(button_layout)
        self.table.itemChanged.connect(self._review_item_changed)
        self._refresh_export_blockers(select_tab=True)
        if self.table.rowCount():
            self.table.setCurrentCell(0, self.COL_FILE)

    @staticmethod
    def _coverage_position(block):
        start_line = int(block.get("script_start_line", 0)) + 1
        end_line = int(block.get("script_end_line", 0)) + 1
        line_text = (
            f"第 {start_line} 行"
            if start_line == end_line
            else f"第 {start_line}-{end_line} 行"
        )
        start_word = int(block.get("script_word_start", 0)) + 1
        end_word = int(block.get("script_word_end", start_word - 1)) + 1
        return f"{line_text}（原文词 {start_word}-{end_word}）"

    @staticmethod
    def _coverage_context(block):
        parts = []
        if block.get("previous_clip"):
            parts.append(f"前一片段：{block['previous_clip']}")
        if block.get("following_clip"):
            parts.append(f"后一片段：{block['following_clip']}")
        if block.get("before_text"):
            parts.append(
                "前文：" + str(block["before_text"]).replace("\n", " / ")
            )
        if block.get("after_text"):
            parts.append(
                "后文：" + str(block["after_text"]).replace("\n", " / ")
            )
        parts.append(str(block.get("issue_reason") or ""))
        return "；".join(part for part in parts if part)

    def _apply_current_include_states(self):
        for row in range(self.table.rowCount()):
            clip = self._clip_for_row(row)
            item = self.table.item(row, self.COL_INCLUDE)
            if clip is not None and item is not None:
                clip["included"] = item.checkState() == QtCore.Qt.Checked

    def _refresh_export_blockers(self, select_tab=False):
        self._apply_current_include_states()
        findings = smart_video_missing_findings(self.bundle)
        blockers = smart_video_export_blockers(self.bundle)
        blocker_keys = {
            (
                block.get("task_label"),
                int(block.get("script_word_start", -1)),
                int(block.get("script_word_end", -1)),
            )
            for block in blockers
        }
        unverified = []
        for task in self.bundle.get("tasks", []):
            task_label = str(task.get("label") or task.get("task_id") or "任务")
            for block in task.get("unverified_blocks", []):
                key = (
                    task_label,
                    int(block.get("script_word_start", -1)),
                    int(block.get("script_word_end", -1)),
                )
                if key in blocker_keys:
                    continue
                unverified.append({**block, "task_label": task_label})

        self.missing_tree.clear()
        for block, severity, color in (
            [(
                item,
                {
                    "approved": "人工通过",
                    "skipped": "本次暂缓",
                }.get(item.get("review_decision"), "未处理"),
                STATUS_COLORS["green"]
                if item.get("review_decision") == "approved"
                else STATUS_COLORS["orange"]
                if item.get("review_decision") == "skipped"
                else STATUS_COLORS["pink"],
            ) for item in findings]
            + [(item, "必须核对", STATUS_COLORS["orange"]) for item in unverified]
        ):
            row = QtWidgets.QTreeWidgetItem([
                severity,
                str(block.get("task_label") or "任务"),
                self._coverage_position(block),
                str(block.get("text") or ""),
                self._coverage_context(block),
            ])
            brush = QtGui.QBrush(QtGui.QColor(color))
            for column in range(self.missing_tree.columnCount()):
                row.setBackground(column, brush)
            task_index = block.get("task_index", -1)
            row.setData(
                0,
                QtCore.Qt.UserRole,
                int(task_index) if task_index is not None else -1,
            )
            self.missing_tree.addTopLevelItem(row)
        self.detail_tabs.setTabText(
            self.coverage_tab_index,
            f"缺段 {len(findings)}（未处理 {len(blockers)}） / "
            f"边界待核对 {len(unverified)}",
        )

        blocked = bool(blockers)
        self.export_button.setEnabled(not blocked)
        self.export_button.setToolTip(
            "请补齐缺失视频并重新分析后再生成。" if blocked else ""
        )
        self.blocker_banner.setVisible(bool(findings))
        if blocked:
            self.export_button.setText("确认并生成视频与 SRT")
            self.blocker_banner.setStyleSheet(
                "QLabel { background:#B3261E; color:white; border-radius:4px; "
                "padding:10px; font-weight:600; }"
            )
            self.blocker_banner.setText(
                f"⛔ 还有 {len(blockers)} 个缺段没有处理，当前不能导出。\n"
                "请选择对应任务：试听后人工确认没问题，或暂缓该任务并先导出其余任务。"
            )
            if select_tab:
                self.detail_tabs.setCurrentIndex(self.coverage_tab_index)
        elif findings:
            approved_tasks = {
                item.get("task_index") for item in findings
                if item.get("review_decision") == "approved"
            }
            skipped_tasks = {
                item.get("task_index") for item in findings
                if item.get("review_decision") == "skipped"
            }
            self.blocker_banner.setStyleSheet(
                "QLabel { background:#7A5A00; color:white; border-radius:4px; "
                "padding:10px; font-weight:600; }"
            )
            self.blocker_banner.setText(
                f"缺段决定已处理：人工通过 {len(approved_tasks)} 个任务，"
                f"本次暂缓 {len(skipped_tasks)} 个任务。"
                "人工通过项将使用完整任务原文生成 SRT。"
            )
            self.export_button.setText(
                "导出其余任务" if skipped_tasks else "确认并生成视频与 SRT"
            )
        else:
            self.export_button.setText("确认并生成视频与 SRT")
        return blockers

    def _set_missing_decision(self, decision):
        item = self.missing_tree.currentItem()
        task_index = item.data(0, QtCore.Qt.UserRole) if item is not None else -1
        try:
            task_index = int(task_index)
        except (TypeError, ValueError):
            task_index = -1
        if task_index < 0 or task_index >= len(self.bundle.get("tasks", [])):
            QtWidgets.QMessageBox.information(
                self, "处理缺段", "请先选择一条红色、绿色或橙色的缺段记录。"
            )
            return
        try:
            self.collect()
        except ValueError as error:
            QtWidgets.QMessageBox.warning(self, "核对内容无效", str(error))
            return
        task = self.bundle["tasks"][task_index]
        if decision == "approved":
            answer = QtWidgets.QMessageBox.question(
                self,
                "人工确认缺段误报",
                "请确认你已经试听过这个任务，并确定视频内容完整。\n\n"
                "继续后将允许导出，并使用完整任务原文生成 SRT。",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return
        try:
            set_smart_video_missing_review(task, decision)
            save_analysis_reports(self.bundle)
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, "处理缺段", str(error))
            return
        self._refresh_export_blockers(select_tab=True)

    def _review_item_changed(self, item):
        if item is not None and item.column() == self.COL_INCLUDE:
            self._refresh_export_blockers(select_tab=True)

    def _clip_for_row(self, row):
        item = self.table.item(row, self.COL_INCLUDE)
        if item is None:
            return None
        task_index, clip_index = item.data(QtCore.Qt.UserRole)
        return self.bundle["tasks"][task_index]["clips"][clip_index]

    def _task_for_row(self, row):
        item = self.table.item(row, self.COL_INCLUDE)
        if item is None:
            return None
        task_index, _clip_index = item.data(QtCore.Qt.UserRole)
        return self.bundle["tasks"][task_index]

    def _load_clip_detail(
        self, current_row, _current_column=-1, _previous_row=-1, _previous_column=-1
    ):
        clip = self._clip_for_row(current_row)
        self._loading_detail = True
        try:
            self.issue_tree.clear()
            if clip is None:
                self.detail_title.setText("请在上方选择一个视频片段")
                self.detail_expected.clear()
                self.detail_recognized.clear()
                return
            threshold = clip.get(
                "silence_threshold_db",
                self.bundle.get("settings", {}).get("silence_threshold_db", -35),
            )
            minimum = clip.get(
                "min_silence_ms",
                self.bundle.get("settings", {}).get("min_silence_ms", 350),
            )
            self.detail_title.setText(
                f"{clip.get('file_name', '')}　｜　{STATUS_TEXT.get(clip.get('status'), clip.get('status', ''))}"
                f"　｜　裁切 {float(clip.get('trim_start') or 0):.3f}-"
                f"{float(clip.get('trim_end') or 0):.3f} 秒 / "
                f"原片 {float(clip.get('original_duration') or 0):.3f} 秒"
                f"　｜　静音阈值 {threshold} dB，最短 {minimum} ms"
            )
            self.detail_expected.setPlainText(str(clip.get("expected_text") or ""))
            self.detail_recognized.setPlainText(str(clip.get("recognized_text") or ""))
            severity_text = {
                "info": "裁切依据",
                "orange": "需核对",
                "pink": "严重",
                "green": "轻微",
            }
            severity_color = {
                "info": "#D9EAF7",
                "orange": STATUS_COLORS["orange"],
                "pink": STATUS_COLORS["pink"],
                "green": STATUS_COLORS["green"],
            }
            for issue in clip.get("issues", []):
                start = float(issue.get("start") or 0.0)
                end = float(issue.get("end") or start)
                line_start = issue.get("line_start")
                line_end = issue.get("line_end")
                if line_start is None:
                    line_text = "—"
                elif line_start == line_end or line_end is None:
                    line_text = str(line_start)
                else:
                    line_text = f"{line_start}-{line_end}"
                severity = str(issue.get("severity") or "orange")
                item = QtWidgets.QTreeWidgetItem([
                    severity_text.get(severity, severity),
                    f"{start:.3f}-{end:.3f}s",
                    line_text,
                    str(issue.get("title") or issue.get("kind") or ""),
                    str(issue.get("expected") or ""),
                    str(issue.get("recognized") or ""),
                    str(issue.get("detail") or ""),
                ])
                item.setData(0, QtCore.Qt.UserRole, copy.deepcopy(issue))
                brush = QtGui.QBrush(
                    QtGui.QColor(severity_color.get(severity, "#FFFFFF"))
                )
                for column in range(self.issue_tree.columnCount()):
                    item.setBackground(column, brush)
                self.issue_tree.addTopLevelItem(item)
            if not clip.get("issues"):
                item = QtWidgets.QTreeWidgetItem([
                    "通过", "—", "—", "未发现问题", "", "",
                    "未发现单词差异，也没有需要人工确认的切点。",
                ])
                brush = QtGui.QBrush(QtGui.QColor(STATUS_COLORS["green"]))
                for column in range(self.issue_tree.columnCount()):
                    item.setBackground(column, brush)
                self.issue_tree.addTopLevelItem(item)
        finally:
            self._loading_detail = False

    def _detail_expected_changed(self):
        if self._loading_detail:
            return
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, self.COL_EXPECTED)
        if item is not None:
            item.setText(self.detail_expected.toPlainText())

    def preview_selected_issue(self):
        row = self.table.currentRow()
        clip = self._clip_for_row(row)
        if clip is None:
            return
        path = Path(clip.get("source", ""))
        if not path.is_file():
            QtWidgets.QMessageBox.warning(self, "试听问题", f"文件不存在：\n{path}")
            return
        issue_item = self.issue_tree.currentItem()
        issue = issue_item.data(0, QtCore.Qt.UserRole) if issue_item else None
        start = max(0.0, float((issue or {}).get("start") or 0.0) - 1.0)
        end = float((issue or {}).get("end") or start + 4.0) + 1.0
        duration = max(3.0, end - start)
        configured = str(
            self.bundle.get("settings", {}).get("ffmpeg_path") or ""
        ).strip()
        ffplay = ""
        if configured:
            sibling = Path(configured).with_name("ffplay.exe")
            if sibling.is_file():
                ffplay = str(sibling)
        ffplay = ffplay or shutil.which("ffplay") or ""
        if ffplay:
            QtCore.QProcess.startDetached(ffplay, [
                "-loglevel", "quiet", "-autoexit", "-ss", f"{start:.3f}",
                "-t", f"{duration:.3f}", str(path.resolve()),
            ])
            return
        QtWidgets.QApplication.clipboard().setText(f"{start + 1.0:.3f}")
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path.resolve())))
        QtWidgets.QMessageBox.information(
            self,
            "试听问题",
            f"没有找到 ffplay，已用默认播放器打开视频。\n"
            f"问题时间 {start + 1.0:.3f} 秒已复制，可在播放器中定位。",
        )

    def open_selected_report(self):
        row = self.table.currentRow()
        task = self._task_for_row(row)
        if task is None:
            QtWidgets.QMessageBox.information(self, "问题报告", "请先选择一个片段。")
            return
        try:
            self.collect()
            save_analysis_reports(self.bundle)
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, "问题报告", str(error))
            return
        path = Path(task.get("text_report_path") or "")
        if not path.is_file():
            QtWidgets.QMessageBox.warning(self, "问题报告", f"报告不存在：\n{path}")
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path.resolve())))

    def copy_problem_report(self):
        try:
            self.collect()
        except ValueError as error:
            QtWidgets.QMessageBox.warning(self, "复制问题", str(error))
            return
        settings = self.bundle.get("settings", {})
        text = "\n\n".join(
            render_task_problem_report(task, settings).strip()
            for task in self.bundle.get("tasks", [])
        )
        QtWidgets.QApplication.clipboard().setText(text)
        QtWidgets.QMessageBox.information(
            self, "复制问题", "完整问题、时间点和自动裁切依据已复制。"
        )

    def _cell_double_clicked(self, row, column):
        if column == self.COL_FILE:
            self.open_video_for_row(row)

    def open_video_for_row(self, row):
        clip = self._clip_for_row(row)
        if clip is None:
            return
        path = Path(clip.get("source", ""))
        if not path.is_file():
            QtWidgets.QMessageBox.warning(self, "打开视频", f"文件不存在：\n{path}")
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path.resolve())))

    def open_selected_video(self):
        row = self.table.currentRow()
        if row < 0:
            QtWidgets.QMessageBox.information(self, "打开视频", "请先选中一个片段。")
            return
        self.open_video_for_row(row)

    def collect(self):
        for row in range(self.table.rowCount()):
            clip = self._clip_for_row(row)
            include_item = self.table.item(row, self.COL_INCLUDE)
            try:
                export_order = int(
                    self.table.item(row, self.COL_ORDER).text().strip()
                )
                if export_order < 1:
                    raise ValueError("顺序必须是大于 0 的整数")
                start = float(self.table.item(row, self.COL_START).text().strip())
                end = float(self.table.item(row, self.COL_END).text().strip())
                expected = self.table.item(row, self.COL_EXPECTED).text().strip()
                apply_clip_review(
                    clip,
                    include_item.checkState() == QtCore.Qt.Checked,
                    start,
                    end,
                    expected,
                )
                clip["export_order"] = export_order
            except (TypeError, ValueError) as error:
                self.table.selectRow(row)
                raise ValueError(f"第 {row + 1} 行：{error}") from error
        return self.bundle

    def save_review(self):
        try:
            self.collect()
            save_analysis_reports(self.bundle)
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, "保存核对结果", str(error))
            return
        QtWidgets.QMessageBox.information(
            self, "保存核对结果", "已保存到各任务的智能剪辑结果目录。"
        )

    def accept(self):
        try:
            bundle = self.collect()
            if not any(
                clip.get("included", True)
                for task in bundle.get("tasks", [])
                for clip in task.get("clips", [])
            ):
                raise ValueError("至少保留一个需要生成的片段。")
            blockers = smart_video_export_blockers(bundle)
            if blockers:
                save_analysis_reports(bundle)
                self._refresh_export_blockers(select_tab=True)
                QtWidgets.QMessageBox.critical(
                    self,
                    "还有缺段未处理",
                    "以下任务原文没有对应的视频片段。当前核对结果已经保存，"
                    "请先人工确认没问题，或把对应任务设为本次暂缓：\n\n"
                    + format_smart_video_export_blockers(blockers),
                )
                return
            save_analysis_reports(bundle)
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, "核对内容无效", str(error))
            return
        self.reviewed_bundle = bundle
        super().accept()

    @classmethod
    def get_reviewed_bundle(cls, bundle, parent=None):
        dialog = cls(bundle, parent)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            return dialog.reviewed_bundle
        return None


class SmartVideoExportResultDialog(QtWidgets.QDialog):
    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.setWindowTitle("智能剪辑完成")
        self.resize(900, 480)
        layout = QtWidgets.QVBoxLayout(self)
        completed = result.get("completed", [])
        failed = result.get("failed", [])
        skipped = result.get("skipped", [])
        label = QtWidgets.QLabel(
            f"成功 {len(completed)} 个任务，暂缓 {len(skipped)} 个任务，"
            f"失败 {len(failed)} 个任务。"
            "双击成功项可打开视频，双击暂缓项可打开任务目录。",
            self,
        )
        layout.addWidget(label)
        tree = QtWidgets.QTreeWidget(self)
        tree.setColumnCount(4)
        tree.setHeaderLabels(["任务", "状态", "视频 / 原因", "SRT"])
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        for item in completed:
            status = "已跳过" if item.get("status") == "skipped" else "已生成"
            row = QtWidgets.QTreeWidgetItem([
                str(item.get("task_id") or ""),
                status,
                str(item.get("video") or item.get("message") or ""),
                str(item.get("srt") or ""),
            ])
            row.setData(0, QtCore.Qt.UserRole, str(item.get("video") or ""))
            tree.addTopLevelItem(row)
        for item in failed:
            row = QtWidgets.QTreeWidgetItem([
                str(item.get("label") or item.get("task_id") or ""),
                "失败",
                str(item.get("error") or ""),
                "",
            ])
            row.setBackground(1, QtGui.QColor("#F4CCCC"))
            tree.addTopLevelItem(row)
        for item in skipped:
            row = QtWidgets.QTreeWidgetItem([
                str(item.get("label") or item.get("task_id") or ""),
                "缺段暂缓",
                str(item.get("message") or ""),
                "",
            ])
            row.setData(0, QtCore.Qt.UserRole, str(item.get("task_dir") or ""))
            row.setBackground(1, QtGui.QColor(STATUS_COLORS["orange"]))
            tree.addTopLevelItem(row)
        tree.itemDoubleClicked.connect(self._open_item)
        header = tree.header()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        self.tree = tree
        layout.addWidget(tree, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _open_item(self, item, _column):
        value = str(item.data(0, QtCore.Qt.UserRole) or "")
        path = Path(value)
        if path.is_file() or path.is_dir():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path.resolve())))
