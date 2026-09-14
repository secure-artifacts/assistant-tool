import json
import logging
import os
import pathlib
import random
import shutil
import traceback
from datetime import date

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import QDate, pyqtSignal
from PyQt5.QtWidgets import QMessageBox, QHeaderView

from app_plugins import PluginHost
from app_plugins.builtin import AudioSplitterPlugin, ChromeLauncherPlugin, InventoryPlugin
from PYUI.main_setting_pyui import MainSettingDialog
from PYUI.review_status_pyui import ReviewStatusDialog
from PYUI.smart_video_editor_pyui import (
    SmartVideoExportResultDialog,
    SmartVideoPendingDialog,
    SmartVideoReviewDialog,
    SmartVideoSourceDialog,
)
from PYUI.utility_managers_pyui import (
    AboutDialog,
    GoogleSheetMonitorDialog,
)
from QTUI.main_ui import Ui_MainDialog
from globalValue import globalValue
from model.AppLogger import configure_application_logging
from model.AudioHelper import CreateTTSAudio, CreateAudio, CreateAudio3
from model.ClipboardHelper import set_internal_clipboard_text
from model.ApiKeyHelper import (
    API_KEY_STATUSES_CONFIG_KEY,
    format_unix_time,
    normalize_api_keys,
    select_api_keys_for_attempt,
)
from model.DailyLinkHistory import (
    DAILY_LINK_HISTORY_CONFIG_KEY,
    daily_link_counts,
    daily_task_sheet_failure_count,
    daily_task_sheet_failures,
    format_daily_links,
    format_daily_task_sheet_failures,
    format_person_daily_links,
    history_dates,
    normalize_daily_link_history,
    record_daily_person_links,
    update_daily_task_sheet_results,
)
from model.FlowParameterGuard import (
    FLOW_GUARD_CONFIG_KEY,
    FlowParameterGuardThread,
    format_flow_guard_targets,
    normalize_flow_guard_settings,
)
from model.GlobalHotkey import (
    DEFAULT_LOAD_TASK_HOTKEY,
    DEFAULT_TASK_RESULT_HOTKEY,
    GlobalHotkeyManager,
    LOAD_TASK_HOTKEY_CONFIG_KEY,
    LOAD_TASK_HOTKEY_ID,
    TASK_RESULT_HOTKEY_CONFIG_KEY,
    TASK_RESULT_HOTKEY_ID,
    normalize_hotkey_sequence,
)
from model.GoogleSheetMonitor import (
    GoogleSheetMonitorThread,
    normalize_monitor_settings,
    reset_monitor_baseline,
)
from model.MusicDucker import (
    DEFAULT_MUSIC_DUCKER_SETTINGS,
    MusicDuckerThread,
    normalize_music_ducker_settings,
)
from model.OdsHelper import ReadTaskOds2, TaskData, format_task_table_report
from model.OralVideoDurationChecker import check_oral_video_durations
from model.ReviewStatusMonitor import (
    ReviewStatusMonitorThread,
    normalize_review_status_settings,
)
from model.ReviewSubmissionHistory import review_history_snapshot
from model.SubtitleHelper import generate_srt_whisper_only, get_text_language
from model.SmartVideoEditor import (
    SMART_VIDEO_EDITOR_CONFIG_KEY,
    SMART_VIDEO_PENDING_CONFIG_KEY,
    SmartVideoEditorThread,
    discover_task_videos,
    format_smart_video_export_blockers,
    normalize_smart_video_editor_settings,
    normalize_smart_video_pending_reviews,
    smart_video_export_blockers,
    update_smart_video_pending_reviews,
)
from model.TaskResultOrganizer import (
    TaskResultOrganizerThread,
    load_effective_config as load_task_result_config,
    migrate_legacy_task_result_config,
)
from model.TaskSubmissionAudit import (
    repair_submission_items,
    scan_task_submission_sheet,
)
from model.TaskReferenceDownloader import (
    TaskReferenceDownloadThread,
    build_task_reference_jobs,
    partition_cached_reference_jobs,
    task_directory_for,
)
from model.TaskTableSchema import (
    field_label,
    load_task_table_schema,
)
from model.UiInterval import IntervalPrompt
from model.VideoHelper import FeatureMatcher


class UpdatedFilesDetectionDialog(QtWidgets.QDialog):
    """Show the files exported in this run before choosing review behavior."""

    video_suffixes = {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm'}

    def __init__(self, updated_files, parent=None):
        super().__init__(parent)
        self.updated_files = [pathlib.Path(path) for path in updated_files]
        self.selected_mode = 'cancel'
        self.setWindowTitle('选择视频审核方式')
        self.setModal(True)
        self.resize(900, 520)

        layout = QtWidgets.QVBoxLayout(self)
        video_count = sum(
            path.suffix.lower() in self.video_suffixes
            for path in self.updated_files
        )
        summary_label = QtWidgets.QLabel(
            f'本次实际新增/更新 {len(self.updated_files)} 个文件，'
            f'其中视频 {video_count} 个。\n'
            '双击文件可先用系统默认程序打开，确认后再选择本轮处理方式。'
        )
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        self.file_tree = QtWidgets.QTreeWidget(self)
        self.file_tree.setColumnCount(3)
        self.file_tree.setHeaderLabels(['文件名', '大小', '所在目录'])
        self.file_tree.setRootIsDecorated(False)
        self.file_tree.setAlternatingRowColors(True)
        self.file_tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.file_tree.setToolTip('双击文件可用系统默认程序打开')
        self.file_tree.itemDoubleClicked.connect(self.openFileItem)
        layout.addWidget(self.file_tree, 1)

        for file_path in self.updated_files:
            item = QtWidgets.QTreeWidgetItem([
                file_path.name,
                self.formatFileSize(file_path),
                str(file_path.parent),
            ])
            item.setData(0, QtCore.Qt.UserRole, str(file_path))
            item.setToolTip(0, str(file_path))
            item.setToolTip(2, str(file_path.parent))
            self.file_tree.addTopLevelItem(item)

        header = self.file_tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        self.file_tree.setColumnWidth(2, 330)
        if self.file_tree.topLevelItemCount():
            self.file_tree.setCurrentItem(self.file_tree.topLevelItem(0))

        button_layout = QtWidgets.QHBoxLayout()
        open_button = QtWidgets.QPushButton('打开选中文件', self)
        open_button.clicked.connect(self.openSelectedFile)
        button_layout.addWidget(open_button)
        button_layout.addStretch(1)

        cancel_button = QtWidgets.QPushButton('取消本次操作', self)
        cancel_button.setToolTip('停止本次整理和上传，并保留文件列表供下次继续')
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)

        manual_button = QtWidgets.QPushButton('逐个手动审核', self)
        manual_button.setToolTip('逐个显示抽帧图，由你人工判断')
        manual_button.clicked.connect(lambda: self.chooseMode('manual'))
        button_layout.addWidget(manual_button)

        skip_button = QtWidgets.QPushButton('无需检测', self)
        skip_button.setToolTip('不做检测，本轮视频全部按正常文件处理')
        skip_button.clicked.connect(lambda: self.chooseMode('skip'))
        button_layout.addWidget(skip_button)

        ai_button = QtWidgets.QPushButton('使用 AI 检测', self)
        ai_button.setToolTip('使用 AI 检测视频元素并自动分流')
        ai_button.clicked.connect(lambda: self.chooseMode('ai'))
        ai_button.setDefault(True)
        button_layout.addWidget(ai_button)
        layout.addLayout(button_layout)

    @staticmethod
    def formatFileSize(file_path):
        try:
            size = file_path.stat().st_size
        except OSError:
            return '无法读取'
        units = ('B', 'KB', 'MB', 'GB', 'TB')
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
            value /= 1024
        return str(size)

    def chooseMode(self, mode):
        self.selected_mode = mode
        self.accept()

    def openSelectedFile(self):
        item = self.file_tree.currentItem()
        if item is not None:
            self.openFileItem(item)

    def openFileItem(self, item, _column=0):
        file_path = pathlib.Path(str(item.data(0, QtCore.Qt.UserRole) or ''))
        if not file_path.is_file():
            QMessageBox.warning(self, '打开文件', f'文件不存在：\n{file_path}')
            return
        opened = QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(file_path.resolve()))
        )
        if not opened:
            QMessageBox.warning(self, '打开文件', f'无法调用系统默认程序：\n{file_path}')


class OralDurationCheckThread(QtCore.QThread):
    log = pyqtSignal(str)
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str, str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = dict(config)

    def run(self):
        try:
            result = check_oral_video_durations(
                self.config,
                progress_callback=self.log.emit,
            )
        except (Exception, SystemExit) as error:
            self.failed.emit(
                f'{type(error).__name__}: {error}',
                traceback.format_exc(),
            )
            return
        self.succeeded.emit(result)


class TaskSubmissionAuditThread(QtCore.QThread):
    log = pyqtSignal(str)
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str, str)

    def __init__(self, config, operation='scan', items=None, parent=None):
        super().__init__(parent)
        self.config = dict(config)
        self.operation = str(operation)
        self.items = list(items or [])

    def run(self):
        try:
            repair_result = None
            if self.operation == 'repair':
                repair_result = repair_submission_items(
                    self.config,
                    self.items,
                    progress_callback=self.log.emit,
                )
            result = scan_task_submission_sheet(
                self.config,
                progress_callback=self.log.emit,
            )
            if repair_result is not None:
                result['repair_result'] = repair_result
        except (Exception, SystemExit) as error:
            self.failed.emit(
                f'{type(error).__name__}: {error}',
                traceback.format_exc(),
            )
            return
        self.succeeded.emit(result)


class TaskSubmissionAuditDialog(QtWidgets.QDialog):
    item_role = QtCore.Qt.UserRole

    def __init__(self, result, config, parent=None):
        super().__init__(parent)
        self.result = dict(result)
        self.config = dict(config)
        self.worker = None
        self.setWindowTitle('任务提交表自查')
        self.resize(1120, 680)

        layout = QtWidgets.QVBoxLayout(self)
        self.summary_label = QtWidgets.QLabel(self)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        filter_layout = QtWidgets.QHBoxLayout()
        filter_layout.addWidget(QtWidgets.QLabel('任务类型：', self))
        self.type_filter = QtWidgets.QComboBox(self)
        self.type_filter.setMinimumWidth(260)
        self.type_filter.currentTextChanged.connect(self.applyFilter)
        filter_layout.addWidget(self.type_filter)
        filter_layout.addStretch(1)
        self.progress_label = QtWidgets.QLabel('', self)
        filter_layout.addWidget(self.progress_label)
        layout.addLayout(filter_layout)

        self.tabs = QtWidgets.QTabWidget(self)
        self.missing_tree = self.createTree([
            '视频名称', '任务类型', '需求人', '任务日期', '任务编号', '网盘链接'
        ])
        self.blank_type_tree = self.createTree([
            '表格行号', '视频名称', '历史/建议类型', '需求人', '网盘链接'
        ])
        self.duplicate_tree = self.createTree([
            '表格行号', '视频名称', '任务类型', '需求人', '网盘链接'
        ])
        self.wrong_date_tree = self.createTree([
            '表格行号', '视频名称', '当前完成日期', '应为上传日期', '任务类型', '网盘链接'
        ])
        self.tabs.addTab(self.missing_tree, '未填写（0）')
        self.tabs.addTab(self.blank_type_tree, '视频类型为空（0）')
        self.tabs.addTab(self.duplicate_tree, '重复登记（0）')
        self.tabs.addTab(self.wrong_date_tree, '历史补填日期有误（0）')
        layout.addWidget(self.tabs, 1)

        hint = QtWidgets.QLabel(
            '“一键补填/修正”处理当前任务类型下的未填写视频，并修正旧版自查功能'
            '曾错误写成同一天的完成日期；视频类型为空和重复登记不会自动修改。'
            '双击条目可打开对应网盘文件。',
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        button_layout = QtWidgets.QHBoxLayout()
        self.open_sheet_button = QtWidgets.QPushButton('打开任务提交表格', self)
        self.open_sheet_button.clicked.connect(self.openSheet)
        button_layout.addWidget(self.open_sheet_button)
        self.refresh_button = QtWidgets.QPushButton('重新检查', self)
        self.refresh_button.clicked.connect(self.refresh)
        button_layout.addWidget(self.refresh_button)
        button_layout.addStretch(1)
        self.repair_button = QtWidgets.QPushButton('一键补填/修正当前筛选', self)
        self.repair_button.clicked.connect(self.repairCurrentFilter)
        button_layout.addWidget(self.repair_button)
        self.close_button = QtWidgets.QPushButton('关闭', self)
        self.close_button.clicked.connect(self.accept)
        button_layout.addWidget(self.close_button)
        layout.addLayout(button_layout)

        self.populate()

    def createTree(self, headers):
        tree = QtWidgets.QTreeWidget(self)
        tree.setColumnCount(len(headers))
        tree.setHeaderLabels(headers)
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        tree.itemDoubleClicked.connect(self.openItem)
        header = tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        return tree

    def populateTree(self, tree, items, row_builder):
        tree.clear()
        for data in items:
            item = QtWidgets.QTreeWidgetItem(row_builder(data))
            item.setData(0, self.item_role, dict(data))
            item.setToolTip(0, str(data.get('task_name') or ''))
            tree.addTopLevelItem(item)

    def populate(self):
        missing = list(self.result.get('missing', []))
        blank_type = list(self.result.get('blank_type', []))
        duplicates = list(self.result.get('duplicates', []))
        wrong_date = list(self.result.get('wrong_date', []))
        self.summary_label.setText(
            '上传历史中共找到 {history_count} 个当前视频；任务提交表已登记 '
            '{present_count} 个；未填写 {missing_count} 个；视频类型为空 '
            '{blank_count} 个；重复登记 {duplicate_count} 个；历史补填日期有误 '
            '{wrong_date_count} 个。'.format(
                history_count=self.result.get('history_count', 0),
                present_count=self.result.get('present_count', 0),
                missing_count=len(missing),
                blank_count=len(blank_type),
                duplicate_count=len(duplicates),
                wrong_date_count=len(wrong_date),
            )
        )
        previous_filter = self.type_filter.currentText()
        self.type_filter.blockSignals(True)
        self.type_filter.clear()
        self.type_filter.addItem('全部类型')
        self.type_filter.addItems(self.result.get('type_values', []))
        previous_index = self.type_filter.findText(previous_filter)
        self.type_filter.setCurrentIndex(max(0, previous_index))
        self.type_filter.blockSignals(False)

        self.populateTree(
            self.missing_tree,
            missing,
            lambda data: [
                str(data.get('file_name') or ''),
                str(data.get('expected_type') or '（未设置）'),
                str(data.get('admin') or ''),
                str(data.get('task_date') or ''),
                str(data.get('task_id') or ''),
                '双击打开' if data.get('drive_link') else '',
            ],
        )
        self.populateTree(
            self.blank_type_tree,
            blank_type,
            lambda data: [
                str(data.get('sheet_row') or ''),
                str(data.get('file_name') or ''),
                str(data.get('expected_type') or '（历史中也未设置）'),
                str(data.get('admin') or ''),
                '双击打开' if (data.get('sheet_link') or data.get('drive_link')) else '',
            ],
        )
        self.populateTree(
            self.duplicate_tree,
            duplicates,
            lambda data: [
                str(data.get('sheet_row') or ''),
                str(data.get('file_name') or ''),
                str(data.get('actual_type') or data.get('expected_type') or ''),
                str(data.get('admin') or ''),
                '双击打开' if (data.get('sheet_link') or data.get('drive_link')) else '',
            ],
        )
        self.populateTree(
            self.wrong_date_tree,
            wrong_date,
            lambda data: [
                str(data.get('sheet_row') or ''),
                str(data.get('file_name') or ''),
                str(data.get('actual_completed_at') or ''),
                str(data.get('expected_completed_at') or ''),
                str(data.get('actual_type') or data.get('expected_type') or '（未设置）'),
                '双击打开' if (data.get('sheet_link') or data.get('drive_link')) else '',
            ],
        )
        self.applyFilter()

    def filteredItems(self, tree):
        return [
            tree.topLevelItem(index).data(0, self.item_role)
            for index in range(tree.topLevelItemCount())
            if not tree.topLevelItem(index).isHidden()
        ]

    def applyFilter(self, *_args):
        selected_type = self.type_filter.currentText()
        visible_counts = []
        for tree in (
            self.missing_tree,
            self.blank_type_tree,
            self.duplicate_tree,
            self.wrong_date_tree,
        ):
            visible = 0
            for index in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(index)
                data = item.data(0, self.item_role) or {}
                hidden = (
                    selected_type != '全部类型'
                    and str(data.get('filter_type') or '') != selected_type
                )
                item.setHidden(hidden)
                if not hidden:
                    visible += 1
            visible_counts.append(visible)
        self.tabs.setTabText(0, f'未填写（{visible_counts[0]}）')
        self.tabs.setTabText(1, f'视频类型为空（{visible_counts[1]}）')
        self.tabs.setTabText(2, f'重复登记（{visible_counts[2]}）')
        self.tabs.setTabText(3, f'历史补填日期有误（{visible_counts[3]}）')
        self.repair_button.setEnabled(
            self.worker is None and (visible_counts[0] + visible_counts[3]) > 0
        )

    def openItem(self, item, _column=0):
        data = item.data(0, self.item_role) or {}
        link = str(data.get('sheet_link') or data.get('drive_link') or '').strip()
        if not link or not QtGui.QDesktopServices.openUrl(QtCore.QUrl(link)):
            QMessageBox.warning(self, '打开网盘文件', '这个条目没有可打开的网盘链接。')

    def openSheet(self):
        url = str(self.result.get('sheet_url') or '').strip()
        if not url or not QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)):
            QMessageBox.warning(self, '打开任务提交表格', '无法打开任务提交表格链接。')

    def setBusy(self, busy, text=''):
        self.progress_label.setText(text)
        self.type_filter.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)
        self.open_sheet_button.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        if busy:
            self.repair_button.setEnabled(False)
        else:
            self.applyFilter()

    def startWorker(self, operation, items=None):
        if self.worker is not None:
            return
        self.setBusy(True, '正在补填并复查……' if operation == 'repair' else '正在重新检查……')
        thread = TaskSubmissionAuditThread(
            self.config,
            operation=operation,
            items=items,
            parent=self,
        )
        parent = self.parent()
        if hasattr(parent, 'appendLog'):
            thread.log.connect(
                lambda message: parent.appendLog(f'[任务表自查] {message}', end='')
            )
        thread.succeeded.connect(self.onWorkerSucceeded)
        thread.failed.connect(self.onWorkerFailed)
        thread.finished.connect(self.onWorkerFinished)
        self.worker = thread
        thread.start()

    def refresh(self):
        self.startWorker('scan')

    def repairCurrentFilter(self):
        items = self.filteredItems(self.missing_tree)
        items.extend(self.filteredItems(self.wrong_date_tree))
        if not items:
            QMessageBox.information(
                self,
                '一键补填/修正',
                '当前筛选下没有缺失视频或需要修正的历史补填日期。',
            )
            return
        selected_type = self.type_filter.currentText()
        answer = QMessageBox.question(
            self,
            '确认一键补填/修正',
            f'将处理当前筛选“{selected_type}”下的 {len(items)} 个条目：'
            '补填缺失视频，或把旧版自查写错的完成日期恢复为实际上传日期。'
            '\n\n视频类型为空和重复登记的条目不会自动修改。继续吗？',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.startWorker('repair', items)

    def onWorkerSucceeded(self, result):
        self.result = dict(result)
        self.populate()
        repair_result = result.get('repair_result')
        if repair_result is not None:
            report = repair_result.get('report', {})
            failure_count = len(report.get('failed_files', []))
            date_failure_count = len(repair_result.get('date_failures', []))
            QMessageBox.information(
                self,
                '一键补填/修正完成',
                '本次补填 {} 个，修正历史日期 {} 个，失败 {} 个。'
                '列表已经重新检查。'.format(
                    repair_result.get('write_count', 0),
                    repair_result.get('date_updated_count', 0),
                    failure_count + date_failure_count,
                ),
            )

    def onWorkerFailed(self, message, traceback_text):
        parent = self.parent()
        if hasattr(parent, 'appendLog'):
            parent.appendLog(
                f'[任务表自查] 操作失败：{message}\n{traceback_text}',
                end='',
                level=logging.ERROR,
            )
        QMessageBox.critical(
            self,
            '任务提交表自查失败',
            f'{message}\n\n详细信息已写入程序日志。',
        )

    def onWorkerFinished(self):
        thread = self.sender()
        if self.worker is thread:
            self.worker = None
        self.setBusy(False)
        thread.deleteLater()

    def reject(self):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, '任务提交表自查', '正在读写表格，请等待操作完成。')
            return
        super().reject()

    def accept(self):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, '任务提交表自查', '正在读写表格，请等待操作完成。')
            return
        super().accept()


class OralDurationCheckResultDialog(QtWidgets.QDialog):
    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.result = result
        self.setWindowTitle('口播长度检测结果')
        self.resize(920, 560)

        layout = QtWidgets.QVBoxLayout(self)
        summary = QtWidgets.QLabel(
            '符合筛选条件：{candidate_count} 行；成功读取时长：{checked_count} 行；'
            '实际修改：{updated_count} 行；未能检测：{skipped_count} 行。'.format(
                candidate_count=result.get('candidate_count', 0),
                checked_count=result.get('checked_count', 0),
                updated_count=result.get('updated_count', 0),
                skipped_count=len(result.get('skipped_rows', [])),
            ),
            self,
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self.tabs = QtWidgets.QTabWidget(self)
        self.updated_tree = QtWidgets.QTreeWidget(self.tabs)
        self.updated_tree.setColumnCount(5)
        self.updated_tree.setHeaderLabels([
            '表格行号', '视频文件', '时长', '原视频类型', '新视频类型'
        ])
        self.updated_tree.setRootIsDecorated(False)
        self.updated_tree.setAlternatingRowColors(True)
        for item in result.get('updated_rows', []):
            row_item = QtWidgets.QTreeWidgetItem([
                str(item.get('row', '')),
                item.get('file_name') or item.get('task_name') or '未命名视频',
                str(item.get('duration', '')),
                str(item.get('old_video_type', '')),
                str(item.get('new_video_type', '')),
            ])
            row_item.setToolTip(1, str(item.get('task_name') or ''))
            self.updated_tree.addTopLevelItem(row_item)
        self.tabs.addTab(
            self.updated_tree,
            f"已修改（{result.get('updated_count', 0)}）",
        )

        self.skipped_tree = QtWidgets.QTreeWidget(self.tabs)
        self.skipped_tree.setColumnCount(3)
        self.skipped_tree.setHeaderLabels(['表格行号', '任务内容', '未检测原因'])
        self.skipped_tree.setRootIsDecorated(False)
        self.skipped_tree.setAlternatingRowColors(True)
        for item in result.get('skipped_rows', []):
            self.skipped_tree.addTopLevelItem(QtWidgets.QTreeWidgetItem([
                str(item.get('row', '')),
                str(item.get('task_name', '')),
                str(item.get('reason', '')),
            ]))
        self.tabs.addTab(
            self.skipped_tree,
            f"未检测（{len(result.get('skipped_rows', []))}）",
        )
        layout.addWidget(self.tabs, 1)

        for tree in (self.updated_tree, self.skipped_tree):
            header = tree.header()
            header.setSectionResizeMode(QHeaderView.ResizeToContents)
            header.setStretchLastSection(True)

        button_layout = QtWidgets.QHBoxLayout()
        open_sheet_button = QtWidgets.QPushButton('打开任务提交表格', self)
        open_sheet_button.clicked.connect(self.openSheet)
        button_layout.addWidget(open_sheet_button)
        button_layout.addStretch(1)
        close_button = QtWidgets.QPushButton('关闭', self)
        close_button.clicked.connect(self.accept)
        close_button.setDefault(True)
        button_layout.addWidget(close_button)
        layout.addLayout(button_layout)

    def openSheet(self):
        url = str(self.result.get('sheet_url') or '').strip()
        if not url or not QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)):
            QMessageBox.warning(self, '打开任务提交表格', '无法打开任务提交表格链接。')


class DailyLinksDialog(QtWidgets.QDialog):
    link_role = QtCore.Qt.UserRole

    def __init__(self, history, parent=None):
        super().__init__(parent)
        self.history = normalize_daily_link_history(history)
        self.setWindowTitle('每日链接与任务表记录（最近 7 天）')
        self.resize(900, 680)

        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel(
            '上传完成后的人员文件夹链接会按日期和批次保存在这里。'
            '双击链接可以直接打开。',
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        date_row = QtWidgets.QHBoxLayout()
        date_row.addWidget(QtWidgets.QLabel('日期：', self))
        self.date_combo = QtWidgets.QComboBox(self)
        date_row.addWidget(self.date_combo, 1)
        layout.addLayout(date_row)

        self.link_tree = QtWidgets.QTreeWidget(self)
        self.link_tree.setHeaderLabels(['收件人', '上传批次', 'Google Drive 链接'])
        self.link_tree.setRootIsDecorated(True)
        self.link_tree.setAlternatingRowColors(True)
        self.link_tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.link_tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        self.link_tree.header().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.link_tree, 1)

        self.failure_title_label = QtWidgets.QLabel('任务提交表待核对视频', self)
        failure_font = self.failure_title_label.font()
        failure_font.setBold(True)
        self.failure_title_label.setFont(failure_font)
        layout.addWidget(self.failure_title_label)

        self.failure_tree = QtWidgets.QTreeWidget(self)
        self.failure_tree.setHeaderLabels(['视频名称', '上传批次', '原因'])
        self.failure_tree.setAlternatingRowColors(True)
        self.failure_tree.setMaximumHeight(190)
        self.failure_tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.failure_tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        self.failure_tree.header().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.failure_tree)

        failure_buttons = QtWidgets.QHBoxLayout()
        self.copy_failure_btn = QtWidgets.QPushButton('复制选中名称', self)
        self.copy_all_failures_btn = QtWidgets.QPushButton('复制全部待核对信息', self)
        failure_buttons.addWidget(self.copy_failure_btn)
        failure_buttons.addWidget(self.copy_all_failures_btn)
        failure_buttons.addStretch(1)
        layout.addLayout(failure_buttons)

        self.status_label = QtWidgets.QLabel('', self)
        layout.addWidget(self.status_label)

        buttons = QtWidgets.QHBoxLayout()
        self.open_btn = QtWidgets.QPushButton('打开选中链接', self)
        self.copy_link_btn = QtWidgets.QPushButton('复制选中链接', self)
        self.copy_person_btn = QtWidgets.QPushButton('复制选中人员', self)
        self.copy_day_btn = QtWidgets.QPushButton('复制当天全部', self)
        close_btn = QtWidgets.QPushButton('关闭', self)
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.copy_link_btn)
        buttons.addWidget(self.copy_person_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.copy_day_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        for day_key in history_dates(self.history):
            day = date.fromisoformat(day_key)
            people_count, link_count = daily_link_counts(self.history, day_key)
            failure_count = daily_task_sheet_failure_count(self.history, day_key)
            self.date_combo.addItem(
                f'{day:%Y-%m-%d}（{people_count} 人 / {link_count} 个链接 / '
                f'{failure_count} 个待核对）',
                day_key,
            )
        if self.date_combo.count() == 0:
            self.date_combo.addItem('最近 7 天暂无已保存链接', '')
            self.date_combo.setEnabled(False)

        self.date_combo.currentIndexChanged.connect(self.refreshLinks)
        self.link_tree.itemDoubleClicked.connect(lambda item, column: self.openSelectedLink())
        self.open_btn.clicked.connect(self.openSelectedLink)
        self.copy_link_btn.clicked.connect(self.copySelectedLink)
        self.copy_person_btn.clicked.connect(self.copySelectedPerson)
        self.copy_day_btn.clicked.connect(self.copyCurrentDay)
        self.copy_failure_btn.clicked.connect(self.copySelectedFailure)
        self.copy_all_failures_btn.clicked.connect(self.copyAllFailures)
        close_btn.clicked.connect(self.accept)
        self.refreshLinks()

    def currentDayKey(self):
        return str(self.date_combo.currentData() or '')

    def refreshLinks(self, _index=None):
        self.link_tree.clear()
        day_key = self.currentDayKey()
        people = self.history.get(day_key, {}).get('people', {})
        for person, slots in sorted(people.items()):
            person_item = QtWidgets.QTreeWidgetItem([person, '', f'{len(slots)} 个链接'])
            self.link_tree.addTopLevelItem(person_item)
            for slot, entry in sorted(slots.items()):
                link = str(entry.get('link', '')).strip()
                child = QtWidgets.QTreeWidgetItem(['', slot, link])
                child.setData(0, self.link_role, link)
                person_item.addChild(child)
            person_item.setExpanded(True)
        failures = daily_task_sheet_failures(self.history, day_key)
        self.failure_tree.clear()
        for failure in failures:
            item = QtWidgets.QTreeWidgetItem([
                failure['file_name'],
                failure['slot'],
                failure['reason'],
            ])
            item.setData(0, self.link_role, failure['file_name'])
            self.failure_tree.addTopLevelItem(item)
        self.failure_title_label.setText(
            f'任务提交表待核对视频（{len(failures)}）'
        )
        people_count, link_count = daily_link_counts(self.history, day_key)
        if day_key:
            self.status_label.setText(
                f'当天共 {people_count} 人、{link_count} 个批次链接；'
                f'{len(failures)} 个视频需要核对任务提交表。'
            )
        else:
            self.status_label.setText('上传成功后，链接会自动出现在这里。')

    def selectedLink(self):
        item = self.link_tree.currentItem()
        if item is None:
            return ''
        return str(item.data(0, self.link_role) or '').strip()

    def selectedPersonItem(self):
        item = self.link_tree.currentItem()
        if item is None:
            return None
        return item.parent() or item

    def openSelectedLink(self):
        link = self.selectedLink()
        if not link:
            QMessageBox.information(self, '查看每日链接', '请先选中一条具体链接。')
            return
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(link)):
            QMessageBox.warning(self, '查看每日链接', f'无法打开链接：\n{link}')

    def copyText(self, text, message):
        if not text:
            return
        set_internal_clipboard_text(text)
        self.status_label.setText(message)

    def copySelectedLink(self):
        link = self.selectedLink()
        if not link:
            QMessageBox.information(self, '查看每日链接', '请先选中一条具体链接。')
            return
        self.copyText(link, '已复制选中的链接。')

    def copySelectedPerson(self):
        item = self.selectedPersonItem()
        if item is None:
            QMessageBox.information(self, '查看每日链接', '请先选中一位收件人。')
            return
        person = item.text(0).strip()
        slots = self.history.get(self.currentDayKey(), {}).get('people', {}).get(person, {})
        self.copyText(format_person_daily_links(person, slots), f'已复制 {person} 的当天链接。')

    def copyCurrentDay(self):
        day_key = self.currentDayKey()
        _people_count, link_count = daily_link_counts(self.history, day_key)
        if not day_key or not link_count:
            QMessageBox.information(self, '查看每日链接', '当天没有可以复制的链接。')
            return
        self.copyText(format_daily_links(self.history, day_key), '已复制当天全部链接。')

    def copySelectedFailure(self):
        item = self.failure_tree.currentItem()
        if item is None:
            QMessageBox.information(self, '任务表待核对', '请先选中一个视频。')
            return
        self.copyText(item.text(0).strip(), '已复制选中的视频名称。')

    def copyAllFailures(self):
        day_key = self.currentDayKey()
        text = format_daily_task_sheet_failures(self.history, day_key)
        if not text:
            QMessageBox.information(self, '任务表待核对', '当天没有待核对视频。')
            return
        self.copyText(text, '已复制当天全部待核对视频。')


class MainDialog(QtWidgets.QDialog, Ui_MainDialog):
    config_name = "config.json"
    music_ducker_settings_key = 'music_ducker_settings'
    google_sheet_monitor_settings_key = 'google_sheet_monitor'
    log_max_blocks = 200

    # 斯洛伐克语不着色；其他语言按首次出现顺序分配不同的浅色。
    task_language_colors = (
        "#FFD6D6",  # 浅红
        "#D9E9FF",  # 浅蓝
        "#FFE7B3",  # 浅橙
        "#E5D9FF",  # 浅紫
        "#D5F2E3",  # 浅绿
        "#FFD9EF",  # 浅粉
        "#D7F0F0",  # 浅青
        "#E8E0D5",  # 浅棕灰
        "#FFF3A8",  # 浅黄
        "#DCE5C2",  # 浅橄榄
    )
    task_language_names = {
        "sk": "斯洛伐克语",
        "ar": "阿拉伯语",
        "pl": "波兰语",
        "cs": "捷克语",
        "sl": "斯洛文尼亚语",
        "hr": "克罗地亚语",
        "sr": "塞尔维亚语",
        "bg": "保加利亚语",
        "ru": "俄语",
        "uk": "乌克兰语",
        "ro": "罗马尼亚语",
        "hu": "匈牙利语",
        "de": "德语",
        "en": "英语",
        "es": "西班牙语",
        "fr": "法语",
        "it": "意大利语",
        "pt": "葡萄牙语",
        "tr": "土耳其语",
        "fa": "波斯语",
        "he": "希伯来语",
        "zh-cn": "简体中文",
        "zh-tw": "繁体中文",
        "ja": "日语",
        "ko": "韩语",
        "unknown": "无法识别",
    }

    printSignal = pyqtSignal(str,str)


    def __init__(self, parent=None):
        super(MainDialog, self).__init__(parent)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(QtCore.Qt.WindowMinimizeButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowMaximizeButtonHint, True)
        self.app_logger, self.app_log_file = configure_application_logging()
        self.setupUi(self)
        self.log_text_edit.setReadOnly(True)
        self.log_text_edit.setUndoRedoEnabled(False)
        self.log_text_edit.document().setMaximumBlockCount(self.log_max_blocks)
        self.log_text_edit.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.log_text_edit.customContextMenuRequested.connect(
            self.showLogContextMenu
        )
        self._elevenlabs_api_key_index = 0
        self._aux_click_count = 0
        self._aux_click_reset_timer = QtCore.QTimer(self)
        self._aux_click_reset_timer.setSingleShot(True)
        self._aux_click_reset_timer.setInterval(5000)
        self._aux_click_reset_timer.timeout.connect(self._resetAuxClicks)
        self._task_language_by_row = {}
        self._task_language_color_by_code = {}
        self.music_ducker_thread = None
        self.music_ducker_settings = dict(DEFAULT_MUSIC_DUCKER_SETTINGS)
        self.task_result_global_hotkey = DEFAULT_TASK_RESULT_HOTKEY
        self.load_task_global_hotkey = DEFAULT_LOAD_TASK_HOTKEY
        self.task_result_hotkey_manager = None
        self.load_task_hotkey_manager = None
        self.task_result_thread = None
        self.task_reference_download_thread = None
        self.smart_video_editor_thread = None
        self._smart_video_editor_phase = None
        self._smart_video_editor_result = None
        self._smart_video_editor_error = None
        self._smart_video_editor_settings = None
        self.smart_video_pending_reviews = []
        self.task_reference_download_root = None
        self._task_result_button_text = ''
        self.loaded_project_dir = None
        self.google_sheet_monitor_settings = normalize_monitor_settings({})
        self.google_sheet_monitor_thread = None
        self.google_sheet_monitor_status = "未启动"
        self.google_sheet_monitor_pending = 0
        self.google_sheet_monitor_unread = 0
        self.daily_link_history = normalize_daily_link_history({})
        self.notification_tray_icon = None
        self.oral_duration_check_thread = None
        self.task_submission_audit_thread = None
        self.flow_guard_settings = normalize_flow_guard_settings({})
        self.flow_guard_thread = None
        self.flow_guard_status = "未启动"
        self.review_status_settings = normalize_review_status_settings({})
        self.review_status_config = load_task_result_config({})
        self.review_status_thread = None
        self.review_status_state = "未启动"

        self.init()
        self.setupToolsMenu()
        self.setupNotificationTray()
        self.plugin_host = PluginHost(self)
        self.inventory_plugin = self.plugin_host.install(InventoryPlugin())
        self.chrome_plugin = self.plugin_host.install(ChromeLauncherPlugin())
        self.audio_splitter_plugin = self.plugin_host.install(AudioSplitterPlugin())
        self.plugin_host.attach_main_menu(self.main_menu_bar)
        self.plugin_host.attach_tools_menu(self.tools_menu)
        self.plugin_host.start_all()
        self.setupUtilityManagerButtons()

        self.task_result_hotkey_manager = GlobalHotkeyManager(
            self,
            hotkey_id=TASK_RESULT_HOTKEY_ID,
        )
        self.task_result_hotkey_manager.activated.connect(
            self.triggerTaskResultFromHotkey
        )
        self.load_task_hotkey_manager = GlobalHotkeyManager(
            self,
            hotkey_id=LOAD_TASK_HOTKEY_ID,
        )
        self.load_task_hotkey_manager.activated.connect(self.triggerLoadTaskFromHotkey)
        self.load_btn.clicked.connect(lambda clicked:self.loadTask())
        self.task_table_widget.cellDoubleClicked.connect(
            self.openTaskDirectoryForRow
        )
        self.task_table_widget.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection
        )
        self.task_table_widget.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.task_table_widget.customContextMenuRequested.connect(
            self.showTaskContextMenu
        )
        self.assign_video_btn.clicked.connect(lambda clicked:self.assignVideo())
        self.open_chrome_btn.clicked.connect(lambda clicked:self.chromeRunner())
        self.launch_next_chrome_btn.clicked.connect(
            lambda clicked: self.launchNextChromeProfile()
        )
        self.music_ducker_checkbox.toggled.connect(
            self.toggleMusicDucker
        )
        self.music_ducker_settings_btn.clicked.connect(
            lambda clicked: self.editMusicDuckerSettings()
        )
        self.gen_audio_btn.clicked.connect(lambda clicked:self.genTaskAudio())
        self.gen_vtt_btn.clicked.connect(lambda clicked:self.genAllTaskVtt())
        self.task_directory_toggle_btn.toggled.connect(
            self.toggleTaskDirectory
        )
        self.toggleTaskDirectory(False)
        self.tidy_task_result_btn.clicked.connect(lambda clicked:self.tidyTaskResult())
        self._task_result_button_text = self.tidy_task_result_btn.text()
        self.daily_links_btn.clicked.connect(lambda clicked: self.openDailyLinks())
        self.google_sheet_monitor_btn.clicked.connect(
            lambda clicked: self.openGoogleSheetMonitor()
        )
        self.review_status_btn.clicked.connect(
            lambda clicked: self.openReviewStatus()
        )
        self.setting_btn.clicked.connect(lambda clicked:self.openSettings())
        self.about_btn.clicked.connect(lambda clicked: self.openAbout())
        self.about_btn.installEventFilter(self)
        self.printSignal.connect(self.print)
        # 快捷键冲突不能阻止主窗口启动；启动阶段只记录日志。
        self.registerTaskResultGlobalHotkey(
            self.task_result_global_hotkey,
            show_error=False,
        )
        self.registerLoadTaskGlobalHotkey(
            self.load_task_global_hotkey,
            show_error=False,
        )
        self.updateActionShortcutTooltips()

        self.task_list = [] #type:list[TaskData]

        self.task_table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

        self.task_path_edit.textChanged.connect(self.clearLoadedProject)
        self.dateEdit.dateChanged.connect(self.clearLoadedProject)

        self.startGoogleSheetMonitor()
        self.startFlowParameterGuard()
        self.startReviewStatusMonitor()

        self.audio_settings = self._load_audio_settings()

        if self.app_log_file is not None:
            log_hint = (
                f"本地日志：{self.app_log_file}\n"
                "右键日志框可打开目录；自动轮转，最多保留约 10 MB；"
                "Python 致命错误另存于同一目录。"
            )
            self.log_text_edit.setToolTip(log_hint)
            self.appendLog(f"[日志] {log_hint.replace(chr(10), ' ')}", end="")

    def setupToolsMenu(self):
        self.main_menu_bar = QtWidgets.QMenuBar(self)
        self.main_menu_bar.setObjectName('main_menu_bar')
        self.tools_menu = self.main_menu_bar.addMenu('工具')
        self.task_submission_audit_action = self.tools_menu.addAction('任务提交表自查')
        self.task_submission_audit_action.setToolTip(
            '按上传历史检查所有任务类型：列出未填写、视频类型为空和重复登记的条目，'
            '并可按类型筛选后一键补填缺失项'
        )
        self.task_submission_audit_action.triggered.connect(
            self.startTaskSubmissionAudit
        )
        self.oral_duration_check_action = self.tools_menu.addAction('口播长度检测')
        self.oral_duration_check_action.setToolTip(
            '检查任务提交表格中属于当前制作人的 1 分钟以内口播视频，'
            '超过 60 秒时自动修改视频类型'
        )
        self.oral_duration_check_action.triggered.connect(
            self.startOralDurationCheck
        )
        self.smart_video_pending_action = self.tools_menu.addAction(
            '待处理智能剪辑'
        )
        self.smart_video_pending_action.setToolTip(
            '查看上次未处理或暂缓的缺段任务，并重新加载视频分析'
        )
        self.smart_video_pending_action.triggered.connect(
            self.openSmartVideoPendingReviews
        )
        self.updateSmartVideoPendingAction()
        self.tools_menu.addSeparator()
        self.flow_guard_action = self.tools_menu.addAction("Flow 参数守卫")
        self.flow_guard_action.setCheckable(True)
        self.flow_guard_action.setChecked(self.flow_guard_settings["enabled"])
        self.flow_guard_action.setToolTip(
            "统一检查所有 Chrome 中的 Google Flow，并守住程序设置里的视频参数"
        )
        self.flow_guard_action.toggled.connect(self.toggleFlowParameterGuard)
        self.gridLayout.setMenuBar(self.main_menu_bar)

    def _set_flow_guard_action_checked(self, checked):
        action = getattr(self, "flow_guard_action", None)
        if action is None:
            return
        action.blockSignals(True)
        action.setChecked(bool(checked))
        action.blockSignals(False)

    def updateFlowParameterGuardAction(self):
        action = getattr(self, "flow_guard_action", None)
        if action is None:
            return
        settings = self.flow_guard_settings
        action.setText("Flow 参数守卫")
        action.setToolTip(
            "状态：{}\n目标：{}\n可在“程序设置 → Flow 守卫”中调整。".format(
                self.flow_guard_status,
                format_flow_guard_targets(settings),
            )
        )

    def startFlowParameterGuard(self):
        if not self.flow_guard_settings.get("enabled"):
            self.flow_guard_status = "未启用"
            self.updateFlowParameterGuardAction()
            return
        if self.flow_guard_thread is not None:
            return
        thread = FlowParameterGuardThread(self.flow_guard_settings, self)
        thread.status.connect(self.onFlowParameterGuardStatus)
        thread.corrected.connect(self.onFlowParameterGuardCorrected)
        thread.error.connect(self.onFlowParameterGuardError)
        thread.finished.connect(self.onFlowParameterGuardFinished)
        self.flow_guard_thread = thread
        self.flow_guard_status = "正在启动…"
        self.updateFlowParameterGuardAction()
        thread.start()

    def stopFlowParameterGuard(self, wait_ms=5000):
        thread = self.flow_guard_thread
        if thread is None:
            return True
        if thread.isRunning():
            thread.stop()
            if not thread.wait(wait_ms):
                return False
        if self.flow_guard_thread is thread:
            self.flow_guard_thread = None
        thread.deleteLater()
        return True

    def restartFlowParameterGuard(self):
        if not self.stopFlowParameterGuard():
            QMessageBox.warning(self, "Flow 参数守卫", "后台检查仍在停止，请稍后再试。")
            return False
        self.flow_guard_status = "未启用"
        self.startFlowParameterGuard()
        return True

    def toggleFlowParameterGuard(self, enabled):
        previous_settings = dict(self.flow_guard_settings)
        self.flow_guard_settings["enabled"] = bool(enabled)
        if not self.stopFlowParameterGuard():
            self.flow_guard_settings = previous_settings
            self._set_flow_guard_action_checked(previous_settings["enabled"])
            QMessageBox.warning(self, "Flow 参数守卫", "后台检查仍在停止，请稍后再试。")
            return
        if not self.saveCurrentConfig():
            self.flow_guard_settings = previous_settings
            self._set_flow_guard_action_checked(previous_settings["enabled"])
            self.startFlowParameterGuard()
            return
        self.flow_guard_status = "正在启动…" if enabled else "未启用"
        self.startFlowParameterGuard()
        self.updateFlowParameterGuardAction()
        self.appendLog(
            "[Flow守卫] 已{}：{}。".format(
                "启用" if enabled else "关闭",
                format_flow_guard_targets(self.flow_guard_settings),
            ),
            end="",
        )

    def onFlowParameterGuardStatus(self, status):
        self.flow_guard_status = str(status)
        self.updateFlowParameterGuardAction()
        self.appendLog(f"[Flow守卫] {self.flow_guard_status}", end="")

    def onFlowParameterGuardCorrected(self, result):
        changes = result.get("changes", [])
        summary = "；".join(
            "{}：{} → {}".format(
                item.get("parameter", "参数"),
                item.get("from", "未知"),
                item.get("to", ""),
            )
            for item in changes
        )
        if not summary:
            return
        self.appendLog(f"[Flow守卫] 已自动修正 {summary}", end="")
        self.showDesktopNotification("Flow 参数已自动修正", summary)

    def onFlowParameterGuardError(self, message):
        self.flow_guard_status = "异常"
        self.updateFlowParameterGuardAction()
        self.appendLog(f"[Flow守卫错误] {message}", end="", level=logging.ERROR)
        self.showDesktopNotification("Flow 参数守卫异常", str(message), critical=True)

    def onFlowParameterGuardFinished(self):
        thread = self.sender()
        if self.flow_guard_thread is thread:
            self.flow_guard_thread = None
        if self.flow_guard_settings.get("enabled") and self.flow_guard_status != "异常":
            self.flow_guard_status = "已停止"
        self.updateFlowParameterGuardAction()

    def setupClipboardMonitor(self):
        """Compatibility wrapper; clipboard ownership now belongs to the plugin."""
        return None

    def toggleClipboardMonitor(self, enabled):
        """Compatibility wrapper; use the inventory plugin command for new code."""
        return self.inventory_plugin.set_clipboard_monitor(enabled)

    def scheduleClipboardInspection(self):
        return self.inventory_plugin.schedule_clipboard_inspection()

    def inspectClipboardForGoogleLinks(self):
        return self.inventory_plugin.inspect_clipboard()

    def startOralDurationCheck(self):
        thread = self.oral_duration_check_thread
        if thread is not None and thread.isRunning():
            QMessageBox.information(self, '口播长度检测', '检测正在进行，请稍候。')
            return

        config = self.load_config()
        self.appendLog('[口播长度] 开始检查任务提交表格……', end='')
        self.oral_duration_check_action.setEnabled(False)
        thread = OralDurationCheckThread(config, self)
        thread.log.connect(
            lambda message: self.appendLog(f'[口播长度] {message}', end='')
        )
        thread.succeeded.connect(self.onOralDurationCheckSucceeded)
        thread.failed.connect(self.onOralDurationCheckFailed)
        thread.finished.connect(self.onOralDurationCheckFinished)
        self.oral_duration_check_thread = thread
        thread.start()

    def startTaskSubmissionAudit(self):
        thread = self.task_submission_audit_thread
        if thread is not None and thread.isRunning():
            QMessageBox.information(self, '任务提交表自查', '检查正在进行，请稍候。')
            return

        config = load_task_result_config(self.load_config())
        self.appendLog('[任务表自查] 开始对照上传历史和任务提交表格……', end='')
        self.task_submission_audit_action.setEnabled(False)
        thread = TaskSubmissionAuditThread(config, parent=self)
        thread.log.connect(
            lambda message: self.appendLog(f'[任务表自查] {message}', end='')
        )
        thread.succeeded.connect(self.onTaskSubmissionAuditSucceeded)
        thread.failed.connect(self.onTaskSubmissionAuditFailed)
        thread.finished.connect(self.onTaskSubmissionAuditFinished)
        self.task_submission_audit_thread = thread
        thread.start()

    def onTaskSubmissionAuditSucceeded(self, result):
        self.appendLog(
            '[任务表自查] 未填写 {} 个，视频类型为空 {} 个，重复登记 {} 个，'
            '历史补填日期有误 {} 个。'.format(
                len(result.get('missing', [])),
                len(result.get('blank_type', [])),
                len(result.get('duplicates', [])),
                len(result.get('wrong_date', [])),
            ),
            end='',
        )
        config = load_task_result_config(self.load_config())
        TaskSubmissionAuditDialog(result, config, self).exec_()

    def onTaskSubmissionAuditFailed(self, message, traceback_text):
        self.appendLog(
            f'[任务表自查] 检查失败：{message}\n{traceback_text}',
            end='',
            level=logging.ERROR,
        )
        QMessageBox.critical(
            self,
            '任务提交表自查失败',
            f'{message}\n\n详细信息已写入程序日志。',
        )

    def onTaskSubmissionAuditFinished(self):
        thread = self.sender()
        if self.task_submission_audit_thread is thread:
            self.task_submission_audit_thread = None
        self.task_submission_audit_action.setEnabled(True)
        thread.deleteLater()

    def onOralDurationCheckSucceeded(self, result):
        updated_rows = result.get('updated_rows', [])
        if updated_rows:
            rows_text = '、'.join(str(item.get('row')) for item in updated_rows)
            self.appendLog(
                f'[口播长度] 已修改 {len(updated_rows)} 行：{rows_text}',
                end='',
            )
        else:
            self.appendLog('[口播长度] 检测完成，没有需要修改的行。', end='')
        OralDurationCheckResultDialog(result, self).exec_()

    def onOralDurationCheckFailed(self, message, traceback_text):
        self.appendLog(
            f'[口播长度] 检测失败：{message}\n{traceback_text}',
            end='',
            level=logging.ERROR,
        )
        QMessageBox.critical(
            self,
            '口播长度检测失败',
            f'{message}\n\n详细信息已写入程序日志。',
        )

    def onOralDurationCheckFinished(self):
        thread = self.sender()
        if self.oral_duration_check_thread is thread:
            self.oral_duration_check_thread = None
        self.oral_duration_check_action.setEnabled(True)
        thread.deleteLater()


    def init(self):
        self.dateEdit.setDate(QDate.currentDate())
        self.tabWidget.setCurrentIndex(0)

        try:
            migrate_legacy_task_result_config(self.config_name)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"迁移整理任务结果配置失败：{error}")

        if os.path.exists(f"./{self.config_name}"):
            with open(f'./{self.config_name}', "r", encoding="utf-8") as rf:
                config = json.load(rf)
                self.load(config)
        self.applyTaskTableLabels()

    def applyTaskTableLabels(self):
        try:
            schema = load_task_table_schema()
        except Exception:
            return
        fields = ("task_id", "admin", "task_type", "task_date", "task_audio_type")
        for column, field_name in enumerate(fields):
            item = self.task_table_widget.horizontalHeaderItem(column)
            if item is not None:
                item.setText(field_label(schema, field_name))

    def setupUtilityManagerButtons(self):
        self.daily_links_btn = QtWidgets.QPushButton(self.common_tools)
        self.daily_links_btn.setObjectName("daily_links_btn")
        self.manager_buttons_layout = QtWidgets.QHBoxLayout()
        self.manager_buttons_layout.setObjectName("manager_buttons_layout")
        self.google_sheet_monitor_btn = QtWidgets.QPushButton(self.common_tools)
        self.google_sheet_monitor_btn.setObjectName("google_sheet_monitor_btn")
        self.review_status_btn = QtWidgets.QPushButton(self.common_tools)
        self.review_status_btn.setObjectName("review_status_btn")
        self.manager_buttons_layout.addWidget(self.google_sheet_monitor_btn)
        self.manager_buttons_layout.addWidget(self.review_status_btn)
        insert_index = self.verticalLayout.indexOf(self.tidy_task_result_btn) + 1
        self.verticalLayout.insertWidget(insert_index, self.daily_links_btn)
        self.verticalLayout.insertLayout(insert_index + 1, self.manager_buttons_layout)
        self.updateDailyLinksButton()
        self.updateGoogleSheetMonitorButton()
        self.updateReviewStatusButton()

        setting_index = self.verticalLayout.indexOf(self.setting_btn)
        self.verticalLayout.removeWidget(self.setting_btn)
        self.footer_buttons_layout = QtWidgets.QHBoxLayout()
        self.footer_buttons_layout.setObjectName("footer_buttons_layout")
        self.about_btn = QtWidgets.QPushButton("关于", self.common_tools)
        self.about_btn.setObjectName("about_btn")
        self.about_btn.setToolTip("查看程序说明和已确认的维护复盘")
        self.footer_buttons_layout.addWidget(self.setting_btn, 1)
        self.footer_buttons_layout.addWidget(self.about_btn)
        self.verticalLayout.insertLayout(setting_index, self.footer_buttons_layout)

    def openAbout(self):
        AboutDialog(self).exec_()

    def _resetAuxClicks(self):
        self._aux_click_count = 0

    def _showAuxNotice(self):
        IntervalPrompt(self).exec_()

    def eventFilter(self, watched, event):
        if watched is getattr(self, 'about_btn', None) and event.type() in {
            QtCore.QEvent.MouseButtonPress,
            QtCore.QEvent.MouseButtonDblClick,
        }:
            if event.button() == QtCore.Qt.RightButton:
                self._aux_click_count += 1
                self._aux_click_reset_timer.start()
                if self._aux_click_count >= 10:
                    self._resetAuxClicks()
                    self._aux_click_reset_timer.stop()
                    QtCore.QTimer.singleShot(0, self._showAuxNotice)
                return True
            self._resetAuxClicks()
            self._aux_click_reset_timer.stop()
        return super().eventFilter(watched, event)

    def updateDailyLinksButton(self):
        today_key = date.today().isoformat()
        people_count, link_count = daily_link_counts(self.daily_link_history, today_key)
        failure_count = daily_task_sheet_failure_count(
            self.daily_link_history,
            today_key,
        )
        self.daily_links_btn.setText('查看每日链接')
        self.daily_links_btn.setToolTip(
            f'今天已保存 {people_count} 人、{link_count} 个批次链接；'
            f'任务提交表有 {failure_count} 个视频待核对。\n'
            '黄色表示今天已有链接，红色表示还有待核对视频。'
        )
        if failure_count:
            self.daily_links_btn.setStyleSheet('background-color: #FFD6D6;')
        elif link_count:
            self.daily_links_btn.setStyleSheet('background-color: #FFF1B8;')
        else:
            self.daily_links_btn.setStyleSheet('')

    def openDailyLinks(self):
        normalized = normalize_daily_link_history(self.daily_link_history)
        if normalized != self.daily_link_history:
            self.daily_link_history = normalized
            self.saveCurrentConfig()
        DailyLinksDialog(self.daily_link_history, self).exec_()

    def updateReviewStatusButton(self, snapshot=None):
        if snapshot is None:
            snapshot = review_history_snapshot()
        passed_count = int(snapshot.get('passed_count', 0) or 0)
        changes_count = int(snapshot.get('needs_changes_count', 0) or 0)
        if changes_count:
            self.review_status_btn.setText(
                '审核提醒：需修改 {}'.format(changes_count)
            )
        elif passed_count:
            self.review_status_btn.setText(
                '审核提醒：待发送 {}'.format(passed_count)
            )
        else:
            self.review_status_btn.setText('审核提醒')
        self.review_status_btn.setToolTip(
            '审核通过待发送：{} 个；需要修改：{} 个。\n'
            '红色优先表示存在需要修改的视频，绿色表示有审核通过的视频待发送。\n'
            '双击列表中的视频可先打开检查。'.format(
                passed_count,
                changes_count,
            )
        )
        if changes_count:
            self.review_status_btn.setStyleSheet('background-color: #FFD6D6;')
        elif passed_count:
            self.review_status_btn.setStyleSheet('background-color: #DDF3E4;')
        else:
            self.review_status_btn.setStyleSheet('')

    def openReviewStatus(self):
        ReviewStatusDialog(parent=self).exec_()
        self.updateReviewStatusButton()

    def startReviewStatusMonitor(self):
        settings = self.review_status_settings
        config = self.review_status_config
        if not settings.get('review_status_monitor_enabled'):
            self.review_status_state = '未启用'
            self.updateReviewStatusButton()
            return
        if not str(config.get('review_sheet_url') or '').strip():
            self.review_status_state = '未配置审核表'
            self.updateReviewStatusButton()
            return
        if self.review_status_thread is not None:
            return
        thread = ReviewStatusMonitorThread(config, parent=self)
        thread.status.connect(self.onReviewStatusMonitorStatus)
        thread.snapshot.connect(self.updateReviewStatusButton)
        thread.changed.connect(self.onReviewStatusChanged)
        thread.log.connect(
            lambda message: self.appendLog('[审核提醒] ' + message, end='')
        )
        thread.finished.connect(self.onReviewStatusMonitorFinished)
        self.review_status_thread = thread
        self.review_status_state = '正在启动…'
        thread.start()

    def stopReviewStatusMonitor(self, wait_ms=10000):
        thread = self.review_status_thread
        if thread is None:
            return True
        if thread.isRunning():
            thread.stop()
            if not thread.wait(wait_ms):
                return False
        if self.review_status_thread is thread:
            self.review_status_thread = None
        thread.deleteLater()
        return True

    def restartReviewStatusMonitor(self):
        if not self.stopReviewStatusMonitor():
            QMessageBox.warning(self, '审核提醒', '后台表格检查仍在停止，请稍后再试。')
            return False
        self.startReviewStatusMonitor()
        return True

    def requestReviewStatusCheck(self):
        thread = self.review_status_thread
        if thread is None:
            self.startReviewStatusMonitor()
            thread = self.review_status_thread
        if thread is not None:
            thread.request_check()

    def onReviewStatusMonitorStatus(self, status):
        status = str(status)
        if status != self.review_status_state:
            self.review_status_state = status
            if status == '异常':
                self.appendLog('[审核提醒] 监视器出现异常，请查看后续错误日志。', end='')

    def onReviewStatusChanged(self, result):
        passed = result.get('passed', [])
        needs_changes = result.get('needs_changes', [])
        self.updateReviewStatusButton(result.get('snapshot'))
        if needs_changes:
            names = '、'.join(
                str(item.get('name') or '未命名视频') for item in needs_changes[:3]
            )
            suffix = ' 等' if len(needs_changes) > 3 else ''
            self.showDesktopNotification(
                '有视频需要修改',
                '{} 个审核视频需要修改：{}{}'.format(
                    len(needs_changes), names, suffix
                ),
                critical=True,
            )
            self.appendLog(
                '[审核提醒] {} 个视频需要修改。'.format(len(needs_changes)),
                end='',
            )
        if passed:
            admins = {
                str(item.get('admin') or '未填写管理员') for item in passed
            }
            self.showDesktopNotification(
                '审核通过，待发送',
                '{} 个视频已通过，请发给对应的 {} 位管理员。'.format(
                    len(passed), len(admins)
                ),
            )
            self.appendLog(
                '[审核提醒] {} 个视频已通过，待发给 {} 位管理员。'.format(
                    len(passed), len(admins)
                ),
                end='',
            )

    def onReviewStatusMonitorFinished(self):
        thread = self.sender()
        if self.review_status_thread is thread:
            self.review_status_thread = None

    def recordTaskResultHistory(self, result):
        person_links = result.get('person_folder_links')
        upload_date = result.get('upload_date') or date.today().isoformat()
        upload_slot = result.get('upload_slot') or '未标记'
        history = self.daily_link_history
        saved_link_count = 0
        failed_file_count = 0
        try:
            if isinstance(person_links, dict) and person_links:
                history, saved_link_count = record_daily_person_links(
                    history,
                    upload_date,
                    upload_slot,
                    person_links,
                )
            history, failed_file_count = update_daily_task_sheet_results(
                history,
                upload_date,
                upload_slot,
                result.get('task_sheet_failed_files', ()),
                result.get('task_sheet_successful_files', ()),
            )
        except (TypeError, ValueError) as error:
            self.appendLog(f'[每日记录] 保存失败：{error}', end='')
            return 0, 0
        if history == self.daily_link_history:
            return saved_link_count, failed_file_count
        self.daily_link_history = history
        if not self.saveCurrentConfig():
            return 0, 0
        self.updateDailyLinksButton()
        if saved_link_count:
            self.appendLog(
                f'[每日链接] 已保存 {upload_date} / 批次 {upload_slot}：'
                f'{saved_link_count} 人',
                end='',
            )
        if failed_file_count:
            self.appendLog(
                f'[任务提交表] {failed_file_count} 个视频未能确认填写成功，'
                '已加入每日待核对记录。',
                end='',
            )
        return saved_link_count, failed_file_count

    def setupNotificationTray(self):
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = self.windowIcon()
        if icon.isNull():
            icon = self.style().standardIcon(QtWidgets.QStyle.SP_MessageBoxInformation)
        self.notification_tray_icon = QtWidgets.QSystemTrayIcon(icon, self)
        self.notification_tray_icon.setToolTip("辅助小工具提醒")
        self.notification_tray_icon.show()

    def showDesktopNotification(self, title, message, critical=False):
        QtWidgets.QApplication.beep()
        if self.notification_tray_icon is not None:
            icon = (
                QtWidgets.QSystemTrayIcon.Critical
                if critical
                else QtWidgets.QSystemTrayIcon.Information
            )
            self.notification_tray_icon.showMessage(title, message, icon, 10_000)

    def clearLoadedProject(self, _value=None):
        self.loaded_project_dir = None
        thread = self.google_sheet_monitor_thread
        if thread is not None:
            thread.set_project_dir(None)

    def updateGoogleSheetDownloadTarget(self):
        thread = self.google_sheet_monitor_thread
        if thread is not None:
            thread.set_project_dir(self.loaded_project_dir)

    def startGoogleSheetMonitor(self):
        if not self.google_sheet_monitor_settings.get("enabled"):
            self.google_sheet_monitor_status = "未启用"
            self.updateGoogleSheetMonitorButton()
            return
        if self.google_sheet_monitor_thread is not None:
            return
        thread = GoogleSheetMonitorThread(
            self.google_sheet_monitor_settings,
            api_config=self.load_config(),
            parent=self,
        )
        thread.status.connect(self.onGoogleSheetMonitorStatus)
        thread.detected.connect(self.onGoogleSheetLinksDetected)
        thread.download_result.connect(self.onGoogleSheetDownloadResult)
        thread.queue_changed.connect(self.onGoogleSheetQueueChanged)
        thread.log.connect(self.appendLog)
        thread.finished.connect(self.onGoogleSheetMonitorFinished)
        self.google_sheet_monitor_thread = thread
        thread.set_project_dir(self.loaded_project_dir)
        self.google_sheet_monitor_status = "正在启动…"
        self.updateGoogleSheetMonitorButton()
        thread.start()

    def stopGoogleSheetMonitor(self, wait_ms=5000):
        thread = self.google_sheet_monitor_thread
        if thread is None:
            return True
        if thread.isRunning():
            thread.stop()
            if not thread.wait(wait_ms):
                return False
        if self.google_sheet_monitor_thread is thread:
            self.google_sheet_monitor_thread = None
        thread.deleteLater()
        return True

    def restartGoogleSheetMonitor(self):
        if not self.stopGoogleSheetMonitor():
            QMessageBox.warning(self, "表格监视器", "后台检查仍在停止，请稍后再试。")
            return
        self.google_sheet_monitor_status = "未启用"
        self.startGoogleSheetMonitor()

    def onGoogleSheetMonitorFinished(self):
        thread = self.sender()
        if self.google_sheet_monitor_thread is thread:
            self.google_sheet_monitor_thread = None

    def onGoogleSheetMonitorStatus(self, status):
        self.google_sheet_monitor_status = str(status)
        self.updateGoogleSheetMonitorButton()

    def onGoogleSheetQueueChanged(self, count):
        self.google_sheet_monitor_pending = max(0, int(count))
        self.updateGoogleSheetMonitorButton()

    def onGoogleSheetLinksDetected(self, payload):
        count = int(payload.get("count", 0))
        self.google_sheet_monitor_unread += count
        if self.loaded_project_dir is None:
            message = "发现 {} 个新链接；尚未加载项目，已保存到待下载队列。".format(count)
        else:
            message = "发现 {} 个新链接，正在下载到当前项目。".format(count)
        self.appendLog(message)
        self.showDesktopNotification("Google 表格发现新链接", message)
        self.updateGoogleSheetMonitorButton()

    def onGoogleSheetDownloadResult(self, result):
        self.google_sheet_monitor_pending = int(result.get("pending", self.google_sheet_monitor_pending))
        if result.get("ok"):
            path = result.get("path", "")
            self.appendLog("表格新文件已下载：{}".format(path))
            if self.google_sheet_monitor_pending == 0:
                self.showDesktopNotification("Google 表格下载完成", "新链接文件已经放入当前项目目录。")
        else:
            error = result.get("error", "未知错误")
            self.appendLog("表格链接下载失败，已延后重试：{}".format(error))
            self.showDesktopNotification(
                "Google 表格下载失败",
                "链接仍保留在队列中，稍后自动重试：{}".format(error),
                critical=True,
            )
        self.updateGoogleSheetMonitorButton()

    def updateGoogleSheetMonitorButton(self):
        button = getattr(self, "google_sheet_monitor_btn", None)
        if button is None:
            return
        if not self.google_sheet_monitor_settings.get("enabled"):
            button.setText("表格监视器：未启用")
            button.setStyleSheet("")
        elif str(self.google_sheet_monitor_status).startswith("异常"):
            button.setText("表格监视器：异常")
            button.setStyleSheet("background:#D93025;color:white;font-weight:bold;")
            button.setToolTip(self.google_sheet_monitor_status)
        elif self.google_sheet_monitor_pending:
            button.setText("表格待下载 {}".format(self.google_sheet_monitor_pending))
            button.setStyleSheet("background:#F9AB00;color:#202124;font-weight:bold;")
            button.setToolTip("加载项目后会自动下载；失败任务会延后重试")
        elif self.google_sheet_monitor_unread:
            button.setText("表格新链接 {}".format(self.google_sheet_monitor_unread))
            button.setStyleSheet("background:#8AB4F8;color:#202124;font-weight:bold;")
        else:
            button.setText("表格监视器：运行中")
            button.setStyleSheet("background:#CEEAD6;color:#174EA6;")
            button.setToolTip(self.google_sheet_monitor_status)

    def openGoogleSheetMonitor(self):
        self.google_sheet_monitor_unread = 0
        self.updateGoogleSheetMonitorButton()
        result = GoogleSheetMonitorDialog.get_settings(
            self.google_sheet_monitor_settings,
            self.google_sheet_monitor_status,
            self,
        )
        if result is None:
            return
        settings, reset_baseline = result
        previous_settings = self.google_sheet_monitor_settings
        if not self.stopGoogleSheetMonitor():
            QMessageBox.warning(self, "表格监视器", "后台检查仍在停止，请稍后再保存。")
            return
        self.google_sheet_monitor_settings = settings
        if reset_baseline:
            try:
                reset_monitor_baseline()
            except OSError as error:
                self.google_sheet_monitor_settings = previous_settings
                self.startGoogleSheetMonitor()
                QMessageBox.critical(self, "表格监视器", "无法重置监视记录：{}".format(error))
                return
        if not self.saveCurrentConfig():
            self.google_sheet_monitor_settings = previous_settings
            self.startGoogleSheetMonitor()
            return
        self.startGoogleSheetMonitor()

    def openInventoryManager(self, material_sources=None, source_label="剪贴板"):
        """Compatibility entry point for callers that predate the plugin API."""
        return self.inventory_plugin.open_manager(
            material_sources=material_sources,
            source_label=source_label,
        )

    def refreshInventoryStatus(self):
        """Compatibility entry point for the extracted inventory plugin."""
        return self.inventory_plugin.refresh_status()

    def toggleTaskDirectory(self, expanded):
        self.file_explorer_tree_view.setVisible(expanded)
        self.task_directory_toggle_btn.setArrowType(
            QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow
        )

    def getTodayDir(self):
        task_dir = pathlib.Path(self.task_path_edit.text())
        if not task_dir.is_dir():
            self.Critical("任务路径不是一个目录！！")
            return None

        task_date = self.dateEdit.date().toString("MMdd")
        today_dir = task_dir.joinpath(task_date)
        return today_dir

    def Critical(self,text):
        QMessageBox.critical(self, "错误", text)

    def loadTask(self):
        today_dir = self.getTodayDir()
        if today_dir is None:
            return

        output_dir = today_dir / "result"
        output_dir.mkdir(parents=True, exist_ok=True)

        table_file_name = str(
            self.load_config().get("task_table_file_name") or "tasks.ods"
        )
        doc_path = today_dir / table_file_name
        if not doc_path.exists():
            self.appendLog("没找到登记表，无法继续！！")
            return



        try:
            schema = load_task_table_schema()
            self.task_list, table_report = ReadTaskOds2(
                doc_path,
                schema=schema,
                return_report=True,
            )
        except Exception as error:
            message = (
                "任务表格读取失败：{}\n\n"
                "请打开“程序设置 → 任务表格”检查工作表、表头行和字段别名。"
            ).format(error)
            self.appendLog(message)
            self.Critical(message)
            return
        self.refreshTaskWidget()

        self.file_explorer_tree_view.load_directory(today_dir)

        self.loaded_project_dir = pathlib.Path(today_dir).resolve()
        self.updateGoogleSheetDownloadTarget()
        self.appendLog(format_task_table_report(table_report))
        self.task_status_label.setText("已加载 {} 个任务".format(len(self.task_list)))
        self.appendLog("加载完成！！")
        self.startTaskReferenceDownloads(today_dir)


    def refreshTaskWidget(self):
        self.task_table_widget.setRowCount(len(self.task_list))
        task: TaskData
        for index, task in enumerate(self.task_list):
            data_col = [task.task_id, task.admin, task.task_type, task.task_date, task.task_audio_type]
            for c_i,c_a in enumerate(data_col):
                item = QtWidgets.QTableWidgetItem(str(c_a or ""))
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                self.task_table_widget.setItem(index, c_i, item)

        # 加载完成后立即标记异常语言，不需要再手动点击检测按钮。
        self.markTaskLanguages()

    def openTaskDirectoryForRow(self, row, _column):
        if row < 0 or row >= len(self.task_list):
            return
        today_dir = self.getTodayDir()
        if today_dir is None:
            return

        task = self.task_list[row]
        try:
            task_dir = task_directory_for(
                today_dir, task.task_type, task.task_id
            )
            task_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(task_dir))
        except (OSError, ValueError) as error:
            self.Critical(
                "无法打开任务 {} 的目录：{}".format(task.task_id, error)
            )

    def selectedTaskRows(self):
        selection_model = self.task_table_widget.selectionModel()
        if selection_model is None:
            return []
        return sorted({index.row() for index in selection_model.selectedRows()})

    def showTaskContextMenu(self, position):
        index = self.task_table_widget.indexAt(position)
        if not index.isValid():
            return
        if index.row() not in self.selectedTaskRows():
            self.task_table_widget.clearSelection()
            self.task_table_widget.selectRow(index.row())
        rows = self.selectedTaskRows()
        if not rows:
            return

        menu = QtWidgets.QMenu(self.task_table_widget)
        plugin_actions = self.plugin_host.populate_task_context_menu(menu, rows)
        smart_video_action = menu.addAction("智能剪辑并生成 SRT…")
        smart_video_action.setToolTip(
            "使用任务语音文案核对视频片段、压缩过长气口，并生成对应 SRT"
        )
        menu.addSeparator()
        open_action = menu.addAction(
            "打开任务目录"
            if len(rows) == 1
            else f"打开所选 {len(rows)} 个任务目录"
        )
        copy_path_action = menu.addAction("复制任务目录路径")
        menu.addSeparator()
        copy_id_action = menu.addAction("复制任务编号")
        copy_name_action = menu.addAction("复制任务名称")
        copy_text_action = menu.addAction("复制语音文案")

        selected = menu.exec_(
            self.task_table_widget.viewport().mapToGlobal(position)
        )
        if selected in plugin_actions:
            return
        if selected == smart_video_action:
            self.startSmartVideoEditor(rows)
        elif selected == open_action:
            self.openSelectedTaskDirectories(rows)
        elif selected == copy_path_action:
            self.copySelectedTaskDirectoryPaths(rows)
        elif selected == copy_id_action:
            self.copySelectedTaskValues(rows, "task_id", "任务编号")
        elif selected == copy_name_action:
            self.copySelectedTaskValues(rows, "task_name", "任务名称")
        elif selected == copy_text_action:
            self.copySelectedTaskValues(rows, "task_audio_text", "语音文案")

    def taskTargetsForRows(self, rows, require_loaded=True):
        if require_loaded and self.loaded_project_dir is None:
            QMessageBox.information(
                self,
                "请先加载任务",
                "任务路径或日期可能已经变化，请先点击“加载”确认当前任务目录。",
            )
            return []
        root_dir = (
            pathlib.Path(self.loaded_project_dir)
            if self.loaded_project_dir is not None
            else self.getTodayDir()
        )
        if root_dir is None:
            return []
        targets = []
        errors = []
        for row in rows:
            if row < 0 or row >= len(self.task_list):
                continue
            task = self.task_list[row]
            try:
                target_dir = task_directory_for(
                    root_dir,
                    task.task_type,
                    task.task_id,
                )
            except ValueError as error:
                errors.append(f"第 {row + 1} 行：{error}")
                continue
            task_name = str(getattr(task, "task_name", "") or "").strip()
            label = str(task.task_id)
            if task_name:
                label += f" | {task_name}"
            targets.append({
                "row": row,
                "task": task,
                "label": label,
                "target_dir": str(target_dir),
            })
        if errors:
            QMessageBox.warning(
                self,
                "部分任务目录无效",
                "以下任务无法确定安全的目录，本次操作已取消：\n"
                + "\n".join(errors),
            )
            return []
        return targets

    def startSmartVideoEditor(self, rows=None):
        if (
            self.smart_video_editor_thread is not None
            and self.smart_video_editor_thread.isRunning()
        ):
            QMessageBox.information(
                self,
                "智能剪辑",
                "智能剪辑正在分析或导出，请等待当前任务完成。",
            )
            return
        rows = self.selectedTaskRows() if rows is None else list(rows)
        targets = self.taskTargetsForRows(rows)
        if not targets:
            return

        config = self.load_config()
        settings = normalize_smart_video_editor_settings(
            config.get(SMART_VIDEO_EDITOR_CONFIG_KEY)
        )
        # Smart editing uses the same subtitle controls as the proven main
        # subtitle workflow.  Capture the live values now so the worker cannot
        # drift onto a second, contradictory set of SRT options.
        settings["srt_include_line_breaks"] = (
            self.subtitle_line_break_checkbox.isChecked()
        )
        settings["srt_max_words_per_block"] = (
            self.subtitle_max_words_spinbox.value()
        )
        settings["srt_block_gap_ms"] = self.subtitle_gap_ms_spinbox.value()
        if not settings.get("ffmpeg_path"):
            settings["ffmpeg_path"] = str(
                config.get("shana_ffmpeg_path") or ""
            ).strip()

        jobs = []
        for target in targets:
            task = target["task"]
            task_dir = pathlib.Path(target["target_dir"])
            sources = discover_task_videos(task_dir, settings)
            jobs.append({
                "task_id": str(getattr(task, "task_id", "") or ""),
                "task_name": str(getattr(task, "task_name", "") or ""),
                "label": target["label"],
                "task_dir": str(task_dir),
                "script": str(getattr(task, "task_audio_text", "") or "").strip(),
                "language": self.detectTaskLanguage(task),
                "sources": [str(path) for path in sources],
            })

        self._startSmartVideoAnalysisJobs(jobs, settings)

    def _startSmartVideoAnalysisJobs(self, jobs, settings):
        """Confirm sources and start analysis for normal or persisted tasks."""
        if not jobs:
            QMessageBox.information(self, "智能剪辑", "没有可以分析的任务。")
            return

        selected_jobs = SmartVideoSourceDialog.get_jobs(jobs, self)
        if selected_jobs is None:
            self.appendLog("[智能剪辑] 用户取消了视频片段选择。", end="")
            return
        try:
            whisper_model = globalValue.get_whisper_model()
        except Exception as error:
            self.appendLog(
                f"[智能剪辑错误] Whisper 模型不可用：{type(error).__name__}: {error}",
                end="",
                level=logging.ERROR,
            )
            QMessageBox.critical(
                self,
                "智能剪辑无法启动",
                "Whisper 模型不可用。详细错误已写入程序日志。",
            )
            return
        self._smart_video_editor_settings = settings
        self.appendLog(
            f"[智能剪辑] 开始核对 {len(selected_jobs)} 个任务的视频片段。",
            end="",
        )
        self._startSmartVideoWorker(
            "analyze",
            settings,
            jobs=selected_jobs,
            model=whisper_model,
        )

    def updateSmartVideoPendingAction(self):
        action = getattr(self, "smart_video_pending_action", None)
        if action is None:
            return
        count = len(self.smart_video_pending_reviews)
        action.setText(
            f"待处理智能剪辑（{count}）" if count else "待处理智能剪辑"
        )

    def _rememberSmartVideoPendingReviews(self, bundle):
        self.smart_video_pending_reviews = update_smart_video_pending_reviews(
            self.smart_video_pending_reviews,
            bundle,
        )
        self.updateSmartVideoPendingAction()
        self.saveCurrentConfig()

    def openSmartVideoPendingReviews(self):
        if (
            self.smart_video_editor_thread is not None
            and self.smart_video_editor_thread.isRunning()
        ):
            QMessageBox.information(
                self, "待处理智能剪辑", "智能剪辑正在运行，请稍后再处理。"
            )
            return
        while True:
            if not self.smart_video_pending_reviews:
                QMessageBox.information(
                    self, "待处理智能剪辑", "目前没有待处理的缺段任务。"
                )
                return
            dialog = SmartVideoPendingDialog(
                self.smart_video_pending_reviews, self
            )
            if dialog.exec_() != QtWidgets.QDialog.Accepted:
                return
            selected = list(dialog.selected_records)
            if dialog.action == "remove":
                remove_ids = {
                    str(item.get("record_id") or "") for item in selected
                }
                self.smart_video_pending_reviews = [
                    item for item in self.smart_video_pending_reviews
                    if str(item.get("record_id") or "") not in remove_ids
                ]
                self.updateSmartVideoPendingAction()
                self.saveCurrentConfig()
                continue
            if dialog.action != "reanalyze":
                return

            config = self.load_config()
            settings = normalize_smart_video_editor_settings(
                config.get(SMART_VIDEO_EDITOR_CONFIG_KEY)
            )
            settings["srt_include_line_breaks"] = (
                self.subtitle_line_break_checkbox.isChecked()
            )
            settings["srt_max_words_per_block"] = (
                self.subtitle_max_words_spinbox.value()
            )
            settings["srt_block_gap_ms"] = self.subtitle_gap_ms_spinbox.value()
            if not settings.get("ffmpeg_path"):
                settings["ffmpeg_path"] = str(
                    config.get("shana_ffmpeg_path") or ""
                ).strip()
            jobs = []
            for record in selected:
                task_dir = pathlib.Path(record.get("task_dir") or "")
                sources = []
                seen = set()
                for source in list(record.get("sources", [])) + [
                    str(path) for path in discover_task_videos(task_dir, settings)
                ]:
                    key = os.path.normcase(os.path.abspath(str(source)))
                    if key in seen or not pathlib.Path(source).is_file():
                        continue
                    seen.add(key)
                    sources.append(str(source))
                jobs.append({
                    "task_id": str(record.get("task_id") or ""),
                    "task_name": str(record.get("task_name") or ""),
                    "label": str(
                        record.get("label") or record.get("task_id") or "任务"
                    ),
                    "task_dir": str(task_dir),
                    "script": str(record.get("script") or ""),
                    "language": str(record.get("language") or ""),
                    "sources": sources,
                })
            self._startSmartVideoAnalysisJobs(jobs, settings)
            return

    def _startSmartVideoWorker(
        self,
        phase,
        settings,
        jobs=None,
        model=None,
        bundle=None,
    ):
        thread = SmartVideoEditorThread(
            phase,
            settings,
            jobs=jobs,
            model=model,
            bundle=bundle,
            parent=self,
        )
        thread.log.connect(self.onSmartVideoEditorLog)
        thread.completed.connect(self.onSmartVideoEditorCompleted)
        thread.failed.connect(self.onSmartVideoEditorFailed)
        thread.finished.connect(self.onSmartVideoEditorFinished)
        self.smart_video_editor_thread = thread
        self._smart_video_editor_phase = phase
        self._smart_video_editor_result = None
        self._smart_video_editor_error = None
        thread.start()

    def onSmartVideoEditorLog(self, message):
        self.appendLog(message, end="")

    def onSmartVideoEditorCompleted(self, result):
        self._smart_video_editor_result = result

    def onSmartVideoEditorFailed(self, message, details):
        self._smart_video_editor_error = (message, details)
        self.appendLog(
            f"[智能剪辑错误] {message}\n{details}",
            end="",
            level=logging.ERROR,
        )

    def onSmartVideoEditorFinished(self):
        thread = self.smart_video_editor_thread
        phase = self._smart_video_editor_phase
        result = self._smart_video_editor_result
        error = self._smart_video_editor_error
        self.smart_video_editor_thread = None
        self._smart_video_editor_phase = None
        self._smart_video_editor_result = None
        self._smart_video_editor_error = None
        if thread is not None:
            thread.deleteLater()
        if error is not None:
            QMessageBox.critical(
                self,
                "智能剪辑失败",
                f"{error[0]}\n\n详细堆栈已写入程序日志。",
            )
            return
        if result is None:
            return
        if phase == "analyze":
            QtCore.QTimer.singleShot(
                0, lambda value=result: self._handleSmartVideoAnalysis(value)
            )
        else:
            self._handleSmartVideoExportResult(result)

    def _handleSmartVideoAnalysis(self, bundle):
        summary = bundle.get("summary", {})
        self.appendLog(
            "[智能剪辑] 核对完成：通过 {green}，需核对 {orange}，严重异常 {pink}，"
            "未匹配文案 {missing} 段；共标出 {problems} 个具体问题、"
            "{cuts} 个自动裁切区间。".format(
                green=summary.get("green_count", 0),
                orange=summary.get("orange_count", 0),
                pink=summary.get("pink_count", 0),
                missing=summary.get("missing_count", 0),
                problems=summary.get("problem_count", 0),
                cuts=summary.get("cut_decision_count", 0),
            ),
            end="",
        )
        settings = normalize_smart_video_editor_settings(
            self._smart_video_editor_settings or bundle.get("settings")
        )
        blockers = smart_video_export_blockers(bundle)
        if blockers:
            self.appendLog(
                f"[智能剪辑严重错误] 检测到 {len(blockers)} 个缺段，等待人工决定。",
                end="",
                level=logging.ERROR,
            )
            QMessageBox.warning(
                self,
                "检测到缺段，需要人工决定",
                "任务原文存在没有对应视频的片段。你可以在核对窗口中试听后"
                "标记“没问题”，也可以暂缓这个任务并先导出其余任务。\n\n"
                + format_smart_video_export_blockers(blockers, limit=8),
            )
        if summary.get("needs_review") or not settings.get("auto_export_clean"):
            reviewed = SmartVideoReviewDialog.get_reviewed_bundle(bundle, self)
            if reviewed is None:
                self._rememberSmartVideoPendingReviews(bundle)
                self.appendLog(
                    "[智能剪辑] 已取消导出；分析报告、识别缓存和待处理任务已保留。",
                    end="",
                )
                return
            bundle = reviewed
        self._rememberSmartVideoPendingReviews(bundle)
        self.appendLog("[智能剪辑] 核对已确认，开始生成视频与 SRT。", end="")
        self._startSmartVideoWorker(
            "export",
            settings,
            bundle=bundle,
            model=globalValue.get_whisper_model(),
        )

    def _handleSmartVideoExportResult(self, result):
        completed = result.get("completed", [])
        failed = result.get("failed", [])
        skipped = result.get("skipped", [])
        self.appendLog(
            f"[智能剪辑] 导出结束：成功 {len(completed)}，"
            f"缺段暂缓 {len(skipped)}，失败 {len(failed)}。",
            end="",
            level=logging.ERROR if failed else logging.INFO,
        )
        SmartVideoExportResultDialog(result, self).exec_()

    def assignMaterialImagesToTasks(self, rows=None):
        """Compatibility entry point for the extracted inventory plugin."""
        return self.inventory_plugin.assign_images_to_tasks(rows)

    def openSelectedTaskDirectories(self, rows=None):
        rows = self.selectedTaskRows() if rows is None else list(rows)
        targets = self.taskTargetsForRows(rows)
        if not targets:
            return
        if len(targets) > 5:
            answer = QMessageBox.question(
                self,
                "打开多个目录",
                f"将同时打开 {len(targets)} 个任务目录，继续吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        failures = []
        for target in targets:
            task_dir = pathlib.Path(target["target_dir"])
            try:
                task_dir.mkdir(parents=True, exist_ok=True)
                os.startfile(str(task_dir))
            except OSError as error:
                failures.append(f"{target['label']}：{error}")
        if failures:
            QMessageBox.warning(
                self,
                "部分目录打开失败",
                "\n".join(failures),
            )

    def copySelectedTaskDirectoryPaths(self, rows=None):
        rows = self.selectedTaskRows() if rows is None else list(rows)
        targets = self.taskTargetsForRows(rows)
        if not targets:
            return
        text = "\n".join(target["target_dir"] for target in targets)
        set_internal_clipboard_text(text)
        self.appendLog(f"已复制 {len(targets)} 个任务目录路径。", end="")

    def copySelectedTaskValues(self, rows, attribute, label):
        values = []
        for row in rows:
            if row < 0 or row >= len(self.task_list):
                continue
            value = str(getattr(self.task_list[row], attribute, "") or "").strip()
            if value:
                values.append(value)
        if not values:
            QMessageBox.information(self, f"复制{label}", f"所选任务没有{label}。")
            return
        set_internal_clipboard_text("\n".join(values))
        self.appendLog(f"已复制 {len(values)} 条{label}。", end="")

    def startTaskReferenceDownloads(self, today_dir):
        if (
            self.task_reference_download_thread is not None
            and self.task_reference_download_thread.isRunning()
        ):
            self.appendLog("参考文件仍在后台下载，本次加载不重复启动。")
            return

        jobs, warnings = build_task_reference_jobs(self.task_list, today_dir)
        for warning in warnings:
            self.appendLog(warning)
        if not jobs:
            return

        jobs, cached_jobs, cache_warnings = partition_cached_reference_jobs(jobs)
        for warning in cache_warnings:
            self.appendLog(warning)
        if cached_jobs:
            self.appendLog(
                "参考文件本地索引命中 {} 个，已跳过 Google 网络请求。".format(
                    len(cached_jobs)
                )
            )
        if not jobs:
            return

        thread = TaskReferenceDownloadThread(jobs, self)
        thread.log.connect(self.onTaskReferenceDownloadLog)
        thread.completed.connect(self.onTaskReferenceDownloadCompleted)
        thread.finished.connect(self.onTaskReferenceDownloadFinished)
        self.task_reference_download_thread = thread
        self.task_reference_download_root = pathlib.Path(today_dir)
        self.appendLog(
            "发现 {} 个未缓存的 Google Drive 参考文件，已在后台开始下载。".format(
                len(jobs)
            )
        )
        thread.start()

    def onTaskReferenceDownloadLog(self, text):
        self.appendLog(text)

    def onTaskReferenceDownloadCompleted(self, result):
        self.appendLog(
            "参考文件处理完成：下载 {downloaded}，修正乱码名 {repaired}，"
            "本地索引 {cached}，联网确认已有 {skipped}，失败 {failed}。".format(
                **result
            )
        )
        if self.task_reference_download_root is not None:
            self.file_explorer_tree_view.load_directory(
                self.task_reference_download_root
            )

    def onTaskReferenceDownloadFinished(self):
        thread = self.task_reference_download_thread
        self.task_reference_download_thread = None
        self.task_reference_download_root = None
        if thread is not None:
            thread.deleteLater()

    def detectTaskLanguage(self, task):
        try:
            return get_text_language(task.task_audio_text or "") or "unknown"
        except Exception as error:
            print(f"语言检测失败（任务 {task.task_id}）: {error}")
            return "unknown"

    def getTaskLanguageColor(self, language_code):
        if language_code not in self._task_language_color_by_code:
            color_index = len(self._task_language_color_by_code) % len(self.task_language_colors)
            self._task_language_color_by_code[language_code] = self.task_language_colors[color_index]
        return self._task_language_color_by_code[language_code]

    def getTaskLanguageName(self, language_code):
        return self.task_language_names.get(language_code, language_code)

    def markTaskLanguages(self):
        """检测任务语音语言并给非斯洛伐克语任务整行着色。"""
        self._task_language_by_row = {}
        self._task_language_color_by_code = {}
        language_counts = {}

        for row, task in enumerate(self.task_list):
            language_code = self.detectTaskLanguage(task)
            self._task_language_by_row[row] = language_code
            language_counts[language_code] = language_counts.get(language_code, 0) + 1

            language_name = self.getTaskLanguageName(language_code)
            tooltip = f"检测语言：{language_name} ({language_code})"
            background = None
            if language_code != "sk":
                background = QtGui.QBrush(QtGui.QColor(
                    self.getTaskLanguageColor(language_code)
                ))

            for column in range(self.task_table_widget.columnCount()):
                item = self.task_table_widget.item(row, column)
                if item is None:
                    continue
                item.setToolTip(tooltip)
                if background is None:
                    item.setBackground(QtGui.QBrush())
                else:
                    item.setBackground(background)
                    item.setForeground(QtGui.QBrush(QtGui.QColor("#202124")))

        return language_counts


    def appendLog(self, text, end="", level=logging.INFO):
        content = f"{text}{end}".rstrip("\r\n")
        if not content:
            return
        self.app_logger.log(level, content)
        cursor = self.log_text_edit.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        if not self.log_text_edit.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(content)
        self.log_text_edit.setTextCursor(cursor)
        self.log_text_edit.ensureCursorVisible()

    def showLogContextMenu(self, position):
        menu = self.log_text_edit.createStandardContextMenu()
        if self.app_log_file is not None:
            menu.addSeparator()
            open_logs_action = menu.addAction("打开本地日志目录")
            open_logs_action.triggered.connect(self.openLocalLogDirectory)
        menu.exec_(self.log_text_edit.mapToGlobal(position))

    def openLocalLogDirectory(self):
        if self.app_log_file is None:
            return
        log_dir = pathlib.Path(self.app_log_file).parent
        opened = QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(log_dir.resolve()))
        )
        if not opened:
            QMessageBox.warning(self, "打开日志目录", f"无法打开：\n{log_dir}")

    def _audioProgress(self, message, level=logging.INFO):
        self.appendLog(f"[音频] {message}", end="", level=level)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)


    def tidyTaskResult(self):
        if self.task_result_thread is not None and self.task_result_thread.isRunning():
            QMessageBox.information(self, '整理任务结果', '整理或上传正在进行，请等待当前任务完成。')
            return

        base_dir = pathlib.Path(self.task_path_edit.text().strip())
        if not base_dir.is_dir():
            self.Critical('任务路径不是一个目录！！')
            return

        config = load_task_result_config(self.load_config())
        selected_date = self.dateEdit.date()
        task_date = date(selected_date.year(), selected_date.month(), selected_date.day())
        thread = TaskResultOrganizerThread(
            [task_date],
            base_dir,
            config,
            self,
            interactive_detection_choice=True,
        )
        thread.log.connect(self.onTaskResultLog)
        thread.detection_choice_requested.connect(
            self.onTaskResultDetectionChoiceRequested
        )
        thread.completed.connect(self.onTaskResultCompleted)
        thread.failed.connect(self.onTaskResultFailed)
        thread.finished.connect(self.onTaskResultFinished)
        self.task_result_thread = thread
        self.tidy_task_result_btn.setEnabled(False)
        self.tidy_task_result_btn.setText('正在整理/上传…')
        self.appendLog(f'开始整理任务结果：{task_date:%Y-%m-%d}', end='')
        thread.start()

    def onTaskResultLog(self, text):
        self.appendLog(text, end='')

    def onTaskResultDetectionChoiceRequested(self, updated_files):
        selected_mode = 'cancel'
        try:
            dialog = UpdatedFilesDetectionDialog(updated_files, self)
            if dialog.exec_() == QtWidgets.QDialog.Accepted:
                selected_mode = dialog.selected_mode
        except BaseException as error:
            self.appendLog(
                f'显示更新文件列表失败，已取消本次操作：{error}',
                end='',
            )
        finally:
            thread = self.task_result_thread
            if thread is not None:
                thread.set_detection_choice(selected_mode)

        mode_names = {
            'ai': '使用 AI 检测',
            'skip': '无需检测',
            'manual': '逐个手动审核',
            'cancel': '取消本次操作，保留待处理列表',
        }
        self.appendLog(
            f'本轮视频处理方式：{mode_names.get(selected_mode, selected_mode)}',
            end='',
        )

    def onTaskResultCompleted(self, result):
        if result.get('cancelled'):
            QMessageBox.information(
                self,
                '已取消整理任务结果',
                str(result.get('message') or '本次操作已取消。'),
            )
            return

        if bool(load_task_result_config(self.load_config()).get('open_result_dir', True)):
            for directory in result.get('result_dirs', []):
                path = pathlib.Path(directory)
                if path.exists():
                    try:
                        os.startfile(str(path))
                    except OSError as error:
                        self.appendLog(f'打开结果目录失败：{error}', end='')

        saved_link_count, failed_file_count = self.recordTaskResultHistory(result)
        # The organizer records newly submitted review links after the sheet
        # write succeeds.  Wake the monitor now instead of waiting for its
        # regular interval.
        self.requestReviewStatusCheck()
        message = str(result.get('message') or '整理完成')
        changed_count = result.get('changed_file_count', 0)
        uploaded_count = result.get('uploaded_file_count', 0)
        details = f'{message}\n本次新增/更新：{changed_count} 个文件'
        if result.get('upload_batch'):
            details += f"\n上传批次：{result['upload_batch']}"
            details += f'\n成功同步：{uploaded_count} 个文件'
        if saved_link_count:
            details += f'\n每日链接汇总：已保存 {saved_link_count} 人（保留 7 天）'
        if failed_file_count:
            details += f'\n任务提交表待核对：{failed_file_count} 个视频'
        QMessageBox.information(self, '整理任务结果', details)

    def onTaskResultFailed(self, message):
        QMessageBox.critical(
            self,
            '整理任务结果失败',
            f'{message}\n\n详细过程已写入主界面日志。',
        )

    def onTaskResultFinished(self):
        thread = self.task_result_thread
        self.task_result_thread = None
        self.tidy_task_result_btn.setEnabled(True)
        self.tidy_task_result_btn.setText(self._task_result_button_text or '整理任务结果')
        if thread is not None:
            thread.deleteLater()


    def assignVideo(self):
        today_dir = self.getTodayDir()
        VIDEO_ROOT_DIR = globalValue.videoSortingStationPath()

        # 匹配阈值 (0.0 ~ 1.0)
        # 这个值代表：图片中的特征点，有多少比例在视频帧中找到了？
        # 0.2 表示图片中 20% 的特征在视频里找到了。
        # 对于裁剪严重的图片，建议设置在 0.15 ~ 0.3 之间。
        try:
            MATCH_RATIO_THRESHOLD = float(
                self.load_config().get("video_match_ratio_threshold", 0.03)
            )
        except (TypeError, ValueError):
            MATCH_RATIO_THRESHOLD = 0.03
        MATCH_RATIO_THRESHOLD = min(1.0, max(0.0, MATCH_RATIO_THRESHOLD))

        # 视频后缀
        VIDEO_EXTS = ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.ts']

        if not os.path.exists(VIDEO_ROOT_DIR) or not os.path.exists(today_dir):
            self.Critical("视频来源目录或当前任务目录配置错误。")
            return

        # 初始化匹配器
        matcher = FeatureMatcher()

        # 1. 加载图片库 (计算特征)
        img_db = FeatureMatcher.scan_images_recursively(today_dir, matcher)
        if not img_db: return

        self.appendLog(f"开始扫描视频: {VIDEO_ROOT_DIR} ...")

        processed = 0
        moved = 0

        for root, dirs, files in os.walk(VIDEO_ROOT_DIR):
            for file in files:
                file_path = os.path.join(root, file)
                ext = os.path.splitext(file)[1].lower()

                if ext in VIDEO_EXTS:
                    self.appendLog(f"正在分析: {file} ...", end='\r')

                    # 获取视频帧
                    frame = FeatureMatcher.get_video_frame_clean(file_path)
                    if frame is None: continue

                    # 计算视频帧的特征
                    _, video_desc = matcher.get_features(frame)
                    if video_desc is None: continue

                    best_score = 0.0
                    best_match = None

                    # 2. 与图片库逐一进行特征匹配
                    for img_data in img_db:
                        score = matcher.match(img_data['desc'], video_desc)

                        if score > best_score:
                            best_score = score
                            best_match = img_data

                    # 3. 判定匹配
                    if best_score >= MATCH_RATIO_THRESHOLD:
                        target_dir = best_match['folder']

                        if os.path.abspath(root) == os.path.abspath(target_dir):
                            continue

                        target_path = os.path.join(target_dir, file)
                        if os.path.exists(target_path):
                            base, ex = os.path.splitext(file)
                            target_path = os.path.join(target_dir, f"{base}_match{ex}")

                        try:
                            self.appendLog(f"\n[匹配成功] {file}")
                            self.appendLog(f"         目标图片: {best_match['name']}")
                            self.appendLog(f"         特征重合度: {best_score:.2%}")  # 显示百分比

                            shutil.move(file_path, target_path)
                            moved += 1
                        except Exception as e:
                            self.appendLog(f"\n[错误] {e}")

                    processed += 1

        self.appendLog("\n" + "=" * 30)
        self.appendLog(f"处理完成。归类: {moved}/{processed}")

    def chromeRunner(self):
        """Compatibility entry point; Chrome lifecycle belongs to its plugin."""
        return self.chrome_plugin.open_launcher()

    def launchNextChromeProfile(self):
        """Compatibility entry point for the retained main-window button."""
        return self.chrome_plugin.launch_next_profile()

    def registerGlobalHotkey(
        self,
        manager,
        shortcut,
        label,
        setting_attr,
        show_error=True,
    ):
        try:
            shortcut = normalize_hotkey_sequence(shortcut)
        except ValueError as error:
            self.appendLog(f'[{label}] 全局快捷键无效：{error}', end='')
            if show_error:
                QMessageBox.warning(self, '全局快捷键无效', f'{label}：{error}')
            return False

        if manager.register(shortcut):
            setattr(self, setting_attr, shortcut)
            self.appendLog(
                f'[{label}] 全局快捷键已启用：{shortcut}',
                end='',
            )
            return True

        message = (
            f'{shortcut} 无法注册，可能已被其他程序占用。'
            f' {manager.last_error}'
        )
        self.appendLog(f'[{label}] 全局快捷键未启用：{message}', end='')
        if show_error:
            QMessageBox.warning(
                self,
                '全局快捷键注册失败',
                message,
            )
        return False

    def activateForGlobalAction(self):
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def triggerTaskResultFromHotkey(self):
        self.activateForGlobalAction()
        self.tidyTaskResult()

    def triggerLoadTaskFromHotkey(self):
        self.activateForGlobalAction()
        self.loadTask()

    def updateActionShortcutTooltips(self):
        self.tidy_task_result_btn.setToolTip(
            f'整理并上传当前任务结果。系统全局快捷键：'
            f'{self.task_result_global_hotkey}'
        )
        self.load_btn.setToolTip(
            f'按当前任务路径和目标日期加载任务。系统全局快捷键：'
            f'{self.load_task_global_hotkey}'
        )

    def registerTaskResultGlobalHotkey(self, shortcut, show_error=True):
        return self.registerGlobalHotkey(
            self.task_result_hotkey_manager,
            shortcut,
            '整理任务结果',
            'task_result_global_hotkey',
            show_error=show_error,
        )

    def registerLoadTaskGlobalHotkey(self, shortcut, show_error=True):
        return self.registerGlobalHotkey(
            self.load_task_hotkey_manager,
            shortcut,
            '加载当前任务',
            'load_task_global_hotkey',
            show_error=show_error,
        )

    def toggleMusicDucker(self, enabled):
        thread = self.music_ducker_thread
        if not enabled:
            if thread is not None and thread.isRunning():
                self.music_ducker_checkbox.setEnabled(False)
                self.music_ducker_checkbox.setText('正在停止音乐压制...')
                thread.stop()
            return

        if thread is not None and thread.isRunning():
            return

        thread = MusicDuckerThread(self.music_ducker_settings, self)
        thread.message.connect(self.onMusicDuckerMessage)
        thread.error.connect(self.onMusicDuckerError)
        thread.finished.connect(self.onMusicDuckerFinished)
        self.music_ducker_thread = thread
        self.music_ducker_checkbox.setText('音乐压制运行中')
        self.music_ducker_checkbox.setStyleSheet(
            'QCheckBox { color: #1B5E20; font-weight: bold; }'
        )
        self.music_ducker_settings_btn.setEnabled(False)
        thread.start()

    def editMusicDuckerSettings(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('音乐压制参数')
        form = QtWidgets.QFormLayout(dialog)

        trigger_apps_edit = QtWidgets.QLineEdit(dialog)
        trigger_apps_edit.setText(', '.join(
            self.music_ducker_settings['trigger_apps']
        ))
        trigger_apps_edit.setToolTip('用逗号分隔；不写 .exe 时会自动补全。')

        music_apps_edit = QtWidgets.QPlainTextEdit(dialog)
        music_apps_edit.setPlainText('\n'.join(
            self.music_ducker_settings['music_apps']
        ))
        music_apps_edit.setMaximumHeight(100)
        music_apps_edit.setToolTip('每行一个进程名，也可以用逗号分隔。')

        duck_to_spin = QtWidgets.QSpinBox(dialog)
        duck_to_spin.setRange(0, 100)
        duck_to_spin.setSuffix(' %')
        duck_to_spin.setValue(self.music_ducker_settings['duck_to_percent'])
        duck_to_spin.setToolTip('检测到达芬奇出声后，音乐最终降低到的音量。')

        threshold_spin = QtWidgets.QDoubleSpinBox(dialog)
        threshold_spin.setRange(0, 1)
        threshold_spin.setDecimals(4)
        threshold_spin.setSingleStep(0.001)
        threshold_spin.setValue(self.music_ducker_settings['peak_threshold'])
        threshold_spin.setToolTip('数值越低越灵敏；太低可能把底噪也当成出声。')

        release_spin = QtWidgets.QDoubleSpinBox(dialog)
        release_spin.setRange(0, 600)
        release_spin.setDecimals(1)
        release_spin.setSuffix(' 秒')
        release_spin.setValue(self.music_ducker_settings['release_seconds'])
        release_spin.setToolTip('达芬奇安静多久后，开始恢复音乐音量。')

        fade_down_spin = QtWidgets.QDoubleSpinBox(dialog)
        fade_down_spin.setRange(0, 600)
        fade_down_spin.setDecimals(1)
        fade_down_spin.setSuffix(' 秒')
        fade_down_spin.setValue(self.music_ducker_settings['fade_down_seconds'])

        fade_up_spin = QtWidgets.QDoubleSpinBox(dialog)
        fade_up_spin.setRange(0, 600)
        fade_up_spin.setDecimals(1)
        fade_up_spin.setSuffix(' 秒')
        fade_up_spin.setValue(self.music_ducker_settings['fade_up_seconds'])

        interval_spin = QtWidgets.QSpinBox(dialog)
        interval_spin.setRange(50, 5000)
        interval_spin.setSingleStep(50)
        interval_spin.setSuffix(' ms')
        interval_spin.setValue(self.music_ducker_settings['check_interval_ms'])
        interval_spin.setToolTip('越小响应越快，但检测频率和 CPU 占用也越高。')

        form.addRow('触发程序：', trigger_apps_edit)
        form.addRow('压低的程序：', music_apps_edit)
        form.addRow('压低后的音量：', duck_to_spin)
        form.addRow('触发峰值阈值：', threshold_spin)
        form.addRow('安静等待时间：', release_spin)
        form.addRow('压低渐变时间：', fade_down_spin)
        form.addRow('恢复渐变时间：', fade_up_spin)
        form.addRow('检测间隔：', interval_spin)

        hint_label = QtWidgets.QLabel(
            '参数会保存到 config.json，并在下一次启用音乐压制时生效。',
            dialog,
        )
        hint_label.setWordWrap(True)
        form.addRow(hint_label)

        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel,
            dialog,
        )
        button_box.button(QtWidgets.QDialogButtonBox.Save).setText('保存')
        button_box.button(QtWidgets.QDialogButtonBox.Cancel).setText('取消')
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        form.addRow(button_box)

        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return

        previous_settings = dict(self.music_ducker_settings)
        self.music_ducker_settings = normalize_music_ducker_settings({
            'trigger_apps': trigger_apps_edit.text(),
            'music_apps': music_apps_edit.toPlainText(),
            'duck_to_percent': duck_to_spin.value(),
            'peak_threshold': threshold_spin.value(),
            'release_seconds': release_spin.value(),
            'fade_down_seconds': fade_down_spin.value(),
            'fade_up_seconds': fade_up_spin.value(),
            'check_interval_ms': interval_spin.value(),
        })
        if self.saveCurrentConfig():
            self.appendLog('[音乐压制] 参数已保存。', end='')
        else:
            self.music_ducker_settings = previous_settings

    def onMusicDuckerMessage(self, message):
        self.appendLog(f'[音乐压制] {message}', end='')

    def onMusicDuckerError(self, message):
        self.appendLog(f'[音乐压制错误] {message}', end='')
        QMessageBox.critical(self, '音乐压制启动失败', message)

    def onMusicDuckerFinished(self):
        thread = self.music_ducker_thread
        self.music_ducker_thread = None
        self.music_ducker_checkbox.blockSignals(True)
        self.music_ducker_checkbox.setChecked(False)
        self.music_ducker_checkbox.blockSignals(False)
        self.music_ducker_checkbox.setEnabled(True)
        self.music_ducker_checkbox.setText('启用音乐压制')
        self.music_ducker_checkbox.setStyleSheet('')
        self.music_ducker_settings_btn.setEnabled(True)
        if thread is not None:
            thread.deleteLater()

    def openSettings(self):
        """打开设置对话框"""
        settings = MainSettingDialog.get_settings(self, plugin_host=self.plugin_host)
        if settings:
            self.audio_settings = self._load_audio_settings()
            saved_config = self.load_config()
            self.review_status_settings = normalize_review_status_settings(
                saved_config
            )
            self.review_status_config = load_task_result_config(saved_config)
            previous_flow_guard_settings = dict(self.flow_guard_settings)
            self.flow_guard_settings = normalize_flow_guard_settings(
                settings.get(FLOW_GUARD_CONFIG_KEY)
            )
            self._set_flow_guard_action_checked(
                self.flow_guard_settings["enabled"]
            )
            flow_guard_restarted = self.restartFlowParameterGuard()
            if not flow_guard_restarted:
                self.flow_guard_settings = previous_flow_guard_settings
                self._set_flow_guard_action_checked(
                    previous_flow_guard_settings["enabled"]
                )
            review_monitor_restarted = self.restartReviewStatusMonitor()
            registration_results = (
                self.registerTaskResultGlobalHotkey(
                    settings.get(
                        TASK_RESULT_HOTKEY_CONFIG_KEY,
                        self.task_result_global_hotkey,
                    )
                ),
                self.registerLoadTaskGlobalHotkey(
                    settings.get(
                        LOAD_TASK_HOTKEY_CONFIG_KEY,
                        self.load_task_global_hotkey,
                    )
                ),
            )
            plugin_registration_results = self.plugin_host.apply_settings(settings)
            registration_results += tuple(
                result for _plugin_id, result in plugin_registration_results
            )
            self.updateActionShortcutTooltips()
            if (
                all(registration_results)
                and flow_guard_restarted
                and review_monitor_restarted
            ):
                QMessageBox.information(
                    self,
                    '设置',
                    '设置已保存。\n'
                    f'启动下一个浏览器：{self.chrome_plugin.global_hotkey}\n'
                    f'整理任务结果：{self.task_result_global_hotkey}\n'
                    f'加载当前任务：{self.load_task_global_hotkey}\n'
                    f'库存与素材管理器：{self.inventory_plugin.global_hotkey}\n'
                    'Flow 参数守卫：{}（{}）'.format(
                        '已启用' if self.flow_guard_settings['enabled'] else '已关闭',
                        format_flow_guard_targets(self.flow_guard_settings),
                    ),
                )
            else:
                # 设置窗口先写入配置；注册失败时用仍然生效的旧值覆盖回来。
                self.saveCurrentConfig()

    def closeEvent(self, event):
        plugins_can_close, plugin_message = self.plugin_host.can_close_all()
        if not plugins_can_close:
            event.ignore()
            QMessageBox.warning(
                self,
                '插件仍在处理任务',
                plugin_message,
            )
            return
        oral_thread = self.oral_duration_check_thread
        if oral_thread is not None and oral_thread.isRunning():
            event.ignore()
            QMessageBox.warning(
                self,
                '口播长度检测仍在进行',
                '正在读取并更新任务提交表格，请等待完成后再关闭程序。',
            )
            return
        audit_thread = self.task_submission_audit_thread
        if audit_thread is not None and audit_thread.isRunning():
            event.ignore()
            QMessageBox.warning(
                self,
                '任务提交表自查仍在进行',
                '正在读取任务提交表格，请等待完成后再关闭程序。',
            )
            return
        smart_video_thread = self.smart_video_editor_thread
        if smart_video_thread is not None and smart_video_thread.isRunning():
            smart_video_thread.requestInterruption()
            event.ignore()
            QMessageBox.warning(
                self,
                "智能剪辑仍在进行",
                "已请求停止智能剪辑。正在处理的识别或编码步骤结束后即可关闭程序；"
                "原视频不会被改动。",
            )
            return
        reference_thread = self.task_reference_download_thread
        if reference_thread is not None and reference_thread.isRunning():
            reference_thread.requestInterruption()
            event.ignore()
            QMessageBox.warning(
                self,
                '参考文件仍在下载',
                '已请求停止后台下载。当前文件处理结束后即可关闭程序。',
            )
            return
        if self.task_result_thread is not None and self.task_result_thread.isRunning():
            event.ignore()
            QMessageBox.warning(
                self,
                '整理任务结果仍在运行',
                '正在整理、检测或上传文件。为避免中断上传，请等待完成后再关闭程序。',
            )
            return
        thread = self.music_ducker_thread
        if thread is not None and thread.isRunning():
            thread.stop()
            if not thread.wait(5000):
                event.ignore()
                QMessageBox.warning(
                    self,
                    '音乐压制仍在停止',
                    '正在恢复音乐音量，请稍后再关闭程序。',
                )
                return

        if not self.stopGoogleSheetMonitor():
            event.ignore()
            QMessageBox.warning(
                self,
                '表格监视器仍在停止',
                '后台网络请求尚未结束，请稍后再关闭程序。',
            )
            return
        if not self.stopFlowParameterGuard():
            self.startGoogleSheetMonitor()
            event.ignore()
            QMessageBox.warning(
                self,
                'Flow 参数守卫仍在停止',
                '后台浏览器检查尚未结束，请稍后再关闭程序。',
            )
            return
        if not self.stopReviewStatusMonitor():
            self.startGoogleSheetMonitor()
            self.startFlowParameterGuard()
            event.ignore()
            QMessageBox.warning(
                self,
                '审核提醒仍在停止',
                '后台表格请求尚未结束，请稍后再关闭程序。',
            )
            return
        if not self.saveCurrentConfig():
            self.startGoogleSheetMonitor()
            self.startFlowParameterGuard()
            self.startReviewStatusMonitor()
            event.ignore()
            return

        if self.task_result_hotkey_manager is not None:
            self.task_result_hotkey_manager.close()
        if self.load_task_hotkey_manager is not None:
            self.load_task_hotkey_manager.close()
        self.plugin_host.stop_all()
        if self.notification_tray_icon is not None:
            self.notification_tray_icon.hide()

        super().closeEvent(event)

    def saveCurrentConfig(self):
        temp_path = f'{self.config_name}.main.tmp'
        try:
            config = self.dump()
            with open(temp_path, 'w', encoding='utf-8') as config_file:
                json.dump(config, config_file, indent=4, ensure_ascii=False)
            os.replace(temp_path, self.config_name)
            return True
        except (OSError, ValueError, TypeError) as error:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            QMessageBox.critical(self, '配置保存失败', str(error))
            return False

    def dump(self):
        # 先读取现有配置
        if os.path.exists(self.config_name):
            with open(self.config_name, "r", encoding="utf-8") as rf:
                existing_config = json.load(rf)
        else:
            existing_config = {}

        # 更新任务路径，保留其他配置（如API key）
        existing_config["task_path"] = self.task_path_edit.text()
        existing_config["audio_use_task_name"] = self.audio_use_task_name_checkbox.isChecked()
        existing_config["subtitle_include_line_breaks"] = self.subtitle_line_break_checkbox.isChecked()
        existing_config["subtitle_max_words_per_block"] = self.subtitle_max_words_spinbox.value()
        existing_config["subtitle_block_gap_ms"] = self.subtitle_gap_ms_spinbox.value()
        existing_config[self.music_ducker_settings_key] = dict(
            self.music_ducker_settings
        )
        existing_config[TASK_RESULT_HOTKEY_CONFIG_KEY] = self.task_result_global_hotkey
        existing_config[LOAD_TASK_HOTKEY_CONFIG_KEY] = self.load_task_global_hotkey
        existing_config[self.google_sheet_monitor_settings_key] = dict(
            self.google_sheet_monitor_settings
        )
        existing_config[FLOW_GUARD_CONFIG_KEY] = normalize_flow_guard_settings(
            self.flow_guard_settings
        )
        existing_config[DAILY_LINK_HISTORY_CONFIG_KEY] = normalize_daily_link_history(
            self.daily_link_history
        )
        existing_config[SMART_VIDEO_PENDING_CONFIG_KEY] = (
            normalize_smart_video_pending_reviews(
                self.smart_video_pending_reviews
            )
        )
        self.plugin_host.update_runtime_config(existing_config)

        return existing_config

    def load(self,dic):
        task_path =dic.get("task_path")
        if task_path is not None:
            self.task_path_edit.setText(task_path)
        self.audio_use_task_name_checkbox.setChecked(
            bool(dic.get("audio_use_task_name", False))
        )
        self.subtitle_line_break_checkbox.setChecked(
            bool(dic.get("subtitle_include_line_breaks", False))
        )
        try:
            max_words_per_block = int(dic.get("subtitle_max_words_per_block", 0))
        except (TypeError, ValueError):
            max_words_per_block = 0
        try:
            block_gap_ms = int(dic.get("subtitle_block_gap_ms", -1))
        except (TypeError, ValueError):
            block_gap_ms = -1
        self.subtitle_max_words_spinbox.setValue(max_words_per_block)
        self.subtitle_gap_ms_spinbox.setValue(block_gap_ms)
        self.music_ducker_settings = normalize_music_ducker_settings(
            dic.get(self.music_ducker_settings_key)
        )
        self.google_sheet_monitor_settings = normalize_monitor_settings(
            dic.get(self.google_sheet_monitor_settings_key)
        )
        self.flow_guard_settings = normalize_flow_guard_settings(
            dic.get(FLOW_GUARD_CONFIG_KEY)
        )
        self.review_status_settings = normalize_review_status_settings(dic)
        self.review_status_config = load_task_result_config(dic)
        self.daily_link_history = normalize_daily_link_history(
            dic.get(DAILY_LINK_HISTORY_CONFIG_KEY)
        )
        self.smart_video_pending_reviews = normalize_smart_video_pending_reviews(
            dic.get(SMART_VIDEO_PENDING_CONFIG_KEY)
        )
        try:
            self.task_result_global_hotkey = normalize_hotkey_sequence(
                dic.get(
                    TASK_RESULT_HOTKEY_CONFIG_KEY,
                    DEFAULT_TASK_RESULT_HOTKEY,
                )
            )
        except ValueError:
            self.task_result_global_hotkey = DEFAULT_TASK_RESULT_HOTKEY
        try:
            self.load_task_global_hotkey = normalize_hotkey_sequence(
                dic.get(
                    LOAD_TASK_HOTKEY_CONFIG_KEY,
                    DEFAULT_LOAD_TASK_HOTKEY,
                )
            )
        except ValueError:
            self.load_task_global_hotkey = DEFAULT_LOAD_TASK_HOTKEY

    def load_config(self):
        """加载配置文件"""
        if os.path.exists(self.config_name):
            with open(self.config_name, "r", encoding="utf-8") as rf:
                return json.load(rf)
        return {}

    def _load_audio_settings(self):
        settings = self.load_config().get("audio_settings", {})
        if not isinstance(settings, dict):
            print("config.json 中的 audio_settings 必须是对象，已忽略。")
            return {}
        return {
            str(name): dict(value)
            for name, value in settings.items()
            if str(name).strip() and isinstance(value, dict)
        }

    def get_elevenlabs_api_keys(self, progress_callback=None):
        """按持久化状态筛选 Key，并轮换可用 Key。"""
        def emit(message):
            if progress_callback is None:
                print(message)
            else:
                progress_callback(message)

        config = self.load_config()
        api_keys = config.get('elevenlabs_api_keys')
        if api_keys is None:
            # 兼容升级前的单 Key 配置。
            api_keys = config.get('elevenlabs_api_key', '')

        api_keys = normalize_api_keys(api_keys)
        if not api_keys:
            return []

        selected_keys, next_index, selection = select_api_keys_for_attempt(
            api_keys,
            config.get(API_KEY_STATUSES_CONFIG_KEY, {}),
            self._elevenlabs_api_key_index,
        )
        self._elevenlabs_api_key_index = next_index

        if selected_keys:
            return selected_keys

        mode = selection.get('mode')
        if mode == 'waiting_exhausted':
            next_key = selection.get('next_key', '')
            emit(
                f"所有 ElevenLabs API Key 的额度都已用完。"
                f"距离刷新最近的 Key (...{next_key[-4:]}) 将在 "
                f"{format_unix_time(selection.get('next_retry_unix'))} 后测试。"
            )
        elif mode == 'waiting_cooldown':
            emit(
                f"所有 ElevenLabs API Key 都在冷却中，最早将在 "
                f"{format_unix_time(selection.get('next_retry_unix'))} 后重试。"
            )
        elif mode == 'no_usable_keys':
            emit("所有 ElevenLabs API Key 均已标记为无效，请在程序设置中处理。")
        return []

    def genAllTaskVtt(self):
        root_dir = self.getTodayDir()
        for task in self.task_list:
            result_dir = root_dir.joinpath(task.task_type).joinpath(task.task_id)
            if not result_dir.exists():
                result_dir.mkdir(parents=True)
            result_file = result_dir / f"task_audio.wav"
            if not result_file.exists():
                result_file = result_dir / f"task_audio.mp3"
            if not result_file.exists():
                result_file = result_dir / f"task_audio.m4a"

            if result_file.exists():
                self.genSrt(result_file, task)

    def genSrt(self, result_file: pathlib.Path, task: TaskData):
        # 生成ASS字幕（支持卡拉OK逐字高亮）
        subtitle_file = (str(result_file).replace('.mp3', '.srt')
                         .replace('.wav', '.srt')
                         .replace('.m4a', '.srt'))

        if not pathlib.Path(subtitle_file).exists():
            try:
                block_gap_ms = self.subtitle_gap_ms_spinbox.value()
                if block_gap_ms < 0:
                    block_gap_ms = None
                generate_srt_whisper_only(
                    str(result_file),
                    task.task_audio_text,
                    subtitle_file,
                    include_line_breaks=self.subtitle_line_break_checkbox.isChecked(),
                    max_words_per_block=self.subtitle_max_words_spinbox.value(),
                    block_gap_ms=block_gap_ms,
                    model=globalValue.get_whisper_model(),
                )
                self.appendLog(f"[字幕] 卡拉OK字幕已生成：{subtitle_file}", end="")
            except Exception as e:
                self.app_logger.exception("生成字幕失败：%s", subtitle_file)
                self.appendLog(
                    f"[字幕错误] {type(e).__name__}: {e}（完整错误已写入本地日志）",
                    end="",
                    level=logging.ERROR,
                )

    def _createTaskAudioFile(self, task, result_file, progress_callback=None):
        settings = self.audio_settings.get(task.task_audio_type)
        if settings is None:
            return False

        model = settings.get("model")
        if model == "edge":
            speed = float(settings.get("speed", 1.0))
            rate_percent = round((speed - 1.0) * 100)
            CreateTTSAudio(
                task.task_audio_text,
                settings["sex"],
                str(result_file),
                rate="{:+d}%".format(rate_percent),
                pitch=settings.get("pitch", "+0Hz"),
                progress_callback=progress_callback,
            )
            return result_file.exists()

        if model == "elevenlabs":
            voices = settings.get("voices") or []
            voice_ids = settings.get("voice_ids") or []
            speed = float(settings.get("speed", 1.0))
            if voices:
                voice = random.choice(voices)
                voice_id = voice.get("id")
                speed = float(voice.get("speed", speed))
            elif voice_ids:
                voice_id = random.choice(voice_ids)
            else:
                raise ValueError("ElevenLabs 语音配置中没有可用的 voice id")

            created = CreateAudio(
                task.task_audio_text,
                str(result_file),
                voice_id=voice_id,
                speed=speed,
                api_keys=self.get_elevenlabs_api_keys(progress_callback),
                api_key_status_config=self.config_name,
                progress_callback=progress_callback,
            )
            return created is not False and result_file.exists()

        if model == "elevenlabs3":
            voice_ids = settings.get("voice_ids") or []
            if not voice_ids:
                raise ValueError("ElevenLabs v3 语音配置中没有可用的 voice id")
            created = CreateAudio3(
                task.task_audio_text,
                str(result_file),
                voice_id=random.choice(voice_ids),
                stability=float(settings.get("stability", 0.35)),
                api_keys=self.get_elevenlabs_api_keys(progress_callback),
                api_key_status_config=self.config_name,
                progress_callback=progress_callback,
            )
            return created is not False and result_file.exists()

        raise ValueError(f"不支持的音频模型：{model or '空'}")

    def _generateTaskAudio(self, use_task_name=False):
        root_dir = self.getTodayDir()
        if root_dir is None:
            return

        task_count = len(self.task_list)
        self._audioProgress(
            f"开始处理 {task_count} 个任务；输出目录：{root_dir}"
        )

        generated = 0
        existing = 0
        skipped_incomplete = 0
        skipped_audio_type = 0
        failed = 0

        for task_index, task in enumerate(self.task_list, 1):
            task_type = str(task.task_type or "").strip()
            task_id = str(task.task_id or "").strip()
            if not task_type or not task_id:
                skipped_incomplete += 1
                continue

            audio_type = str(task.task_audio_type or "").strip()
            if not audio_type or audio_type not in self.audio_settings:
                skipped_audio_type += 1
                continue

            try:
                self._audioProgress(
                    f"[{task_index}/{task_count}] 任务 {task_id}，语音配置：{audio_type}"
                )
                result_dir = root_dir.joinpath(task_type, task_id)
                result_dir.mkdir(parents=True, exist_ok=True)
                if use_task_name:
                    result_name = str(task.task_name or "task_audio").strip() or "task_audio"
                else:
                    result_name = "task_audio"
                result_file = result_dir / f"{result_name}.mp3"

                if result_file.exists():
                    existing += 1
                    self._audioProgress(
                        f"任务 {task_id} 已有音频，跳过：{result_file.name}"
                    )
                elif self._createTaskAudioFile(
                    task,
                    result_file,
                    progress_callback=self._audioProgress,
                ):
                    generated += 1
                    self._audioProgress(f"任务 {task_id} 生成完成")
                else:
                    failed += 1
                    self._audioProgress(
                        f"任务 {task_id} 生成失败，请查看上方信息和本地错误日志。",
                        level=logging.ERROR,
                    )
                    continue

                if not use_task_name:
                    self.genSrt(result_file, task)
            except Exception as error:
                failed += 1
                self.app_logger.exception("生成音频失败：任务 %s", task_id)
                self._audioProgress(
                    f"任务 {task_id} 失败：{type(error).__name__}: {error}"
                    "（完整 traceback 已写入本地日志）",
                    level=logging.ERROR,
                )

        summary = (
            f"音频处理完成：新生成 {generated}，已有 {existing}，失败 {failed}"
        )
        if skipped_incomplete:
            summary += f"，跳过路径信息不完整的记录 {skipped_incomplete}"
        if skipped_audio_type:
            summary += f"，跳过未配置语音类型的记录 {skipped_audio_type}"
        self._audioProgress(summary)

    def genTaskAudio(self):
        if getattr(self, "_audio_generation_active", False):
            self._audioProgress("音频任务仍在运行，请等待完成。")
            return

        self._audio_generation_active = True
        original_text = self.gen_audio_btn.text()
        self.gen_audio_btn.setEnabled(False)
        self.gen_audio_btn.setText("正在生成音频…")
        try:
            self._generateTaskAudio(
                use_task_name=self.audio_use_task_name_checkbox.isChecked()
            )
        except Exception as error:
            self.app_logger.exception("音频批量处理意外中断")
            self._audioProgress(
                f"批量处理意外中断：{type(error).__name__}: {error}"
                "（完整 traceback 已写入本地日志）",
                level=logging.ERROR,
            )
            QMessageBox.critical(
                self,
                "生成音频失败",
                f"音频处理意外中断：{error}\n\n完整错误已写入本地日志。",
            )
        finally:
            self._audio_generation_active = False
            self.gen_audio_btn.setText(original_text)
            self.gen_audio_btn.setEnabled(True)

    def print(self,text,end='\n'):
        self.appendLog(text, end)

    def printEmit(self,text,end='\n'):
        self.printSignal.emit(text,end)
