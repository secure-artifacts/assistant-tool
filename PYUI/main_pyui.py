import json
import os
import pathlib
import random
import shutil
from datetime import date

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import QDate, QThreadPool, pyqtSignal
from PyQt5.QtWidgets import QMessageBox, QHeaderView

from PYUI.chrome_runner_pyui import ChromeRunnerDialog
from PYUI.main_setting_pyui import MainSettingDialog
from PYUI.utility_managers_pyui import (
    GoogleSheetMonitorDialog,
    InventoryManagerDialog,
)
from QTUI.main_ui import Ui_MainDialog
from globalValue import globalValue
from model.AudioHelper import CreateTTSAudio, CreateAudio, splitAudio, CreateAudio3
from model.ApiKeyHelper import (
    API_KEY_STATUSES_CONFIG_KEY,
    format_unix_time,
    normalize_api_keys,
    select_api_keys_for_attempt,
)
from model.GlobalHotkey import (
    CHROME_NEXT_HOTKEY_CONFIG_KEY,
    DEFAULT_CHROME_NEXT_HOTKEY,
    GlobalHotkeyManager,
    normalize_hotkey_sequence,
)
from model.GoogleSheetMonitor import (
    GoogleSheetMonitorThread,
    normalize_monitor_settings,
    reset_monitor_baseline,
)
from model.InventoryManager import (
    InventoryStore,
    STATUS_CRITICAL,
    STATUS_INITIAL,
    STATUS_LABELS,
    STATUS_MODERATE,
    STATUS_NORMAL,
)
from model.MusicDucker import (
    DEFAULT_MUSIC_DUCKER_SETTINGS,
    MusicDuckerThread,
    normalize_music_ducker_settings,
)
from model.OdsHelper import ReadTaskOds2, TaskData, format_task_table_report
from model.SubtitleHelper import generate_srt_whisper_only, get_text_language
from model.TaskResultOrganizer import (
    TaskResultOrganizerThread,
    load_effective_config as load_task_result_config,
    migrate_legacy_task_result_config,
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
from model.VideoHelper import FeatureMatcher


class UpdatedFilesDetectionDialog(QtWidgets.QDialog):
    """Show the files exported in this run before choosing review behavior."""

    video_suffixes = {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm'}

    def __init__(self, updated_files, parent=None):
        super().__init__(parent)
        self.updated_files = [pathlib.Path(path) for path in updated_files]
        self.selected_mode = 'manual'
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
        self.setupUi(self)
        self.log_text_edit.setReadOnly(True)
        self.log_text_edit.setUndoRedoEnabled(False)
        self.log_text_edit.document().setMaximumBlockCount(self.log_max_blocks)
        self._elevenlabs_api_key_index = 0
        self._task_language_by_row = {}
        self._task_language_color_by_code = {}
        self.music_ducker_thread = None
        self.music_ducker_settings = dict(DEFAULT_MUSIC_DUCKER_SETTINGS)
        self.chrome_global_hotkey = DEFAULT_CHROME_NEXT_HOTKEY
        self.chrome_hotkey_manager = None
        self.task_result_thread = None
        self.task_reference_download_thread = None
        self.task_reference_download_root = None
        self._task_result_button_text = ''
        self.loaded_project_dir = None
        self.google_sheet_monitor_settings = normalize_monitor_settings({})
        self.google_sheet_monitor_thread = None
        self.google_sheet_monitor_status = "未启动"
        self.google_sheet_monitor_pending = 0
        self.google_sheet_monitor_unread = 0
        self.inventory_store = InventoryStore()
        self._inventory_alert_signature = None
        self._inventory_error = ""
        self.notification_tray_icon = None

        self.init()
        self.setupUtilityManagerButtons()
        self.setupNotificationTray()

        self.chrome_runner = ChromeRunnerDialog(
            self,
            config_path=self.config_name,
        )
        self.chrome_hotkey_manager = GlobalHotkeyManager(self)
        self.chrome_hotkey_manager.activated.connect(
            self.launchNextChromeProfile
        )

        self.load_btn.clicked.connect(lambda clicked:self.loadTask())
        self.task_table_widget.cellDoubleClicked.connect(
            self.openTaskDirectoryForRow
        )
        self.assign_video_btn.clicked.connect(lambda clicked:self.assignVideo())
        self.split_audio_btn.clicked.connect(lambda clicked:self.splitAudio())
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
        self.gen_audio_btn2.clicked.connect(lambda clicked:self.genTaskAudio2())
        self.gen_vtt_btn.clicked.connect(lambda clicked:self.genAllTaskVtt())
        self.task_directory_toggle_btn.toggled.connect(
            self.toggleTaskDirectory
        )
        self.toggleTaskDirectory(False)
        self.tidy_task_result_btn.clicked.connect(lambda clicked:self.tidyTaskResult())
        self._task_result_button_text = self.tidy_task_result_btn.text()
        self.google_sheet_monitor_btn.clicked.connect(
            lambda clicked: self.openGoogleSheetMonitor()
        )
        self.inventory_manager_btn.clicked.connect(
            lambda clicked: self.openInventoryManager()
        )
        self.setting_btn.clicked.connect(lambda clicked:self.openSettings())
        self.printSignal.connect(self.print)
        # 快捷键冲突不能阻止主窗口启动；启动阶段只记录日志。
        self.registerChromeGlobalHotkey(
            self.chrome_global_hotkey,
            show_error=False,
        )

        self.task_list = [] #type:list[TaskData]

        self.task_table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

        self.task_path_edit.textChanged.connect(self.clearLoadedProject)
        self.dateEdit.dateChanged.connect(self.clearLoadedProject)

        self.inventory_status_timer = QtCore.QTimer(self)
        self.inventory_status_timer.setInterval(60_000)
        self.inventory_status_timer.timeout.connect(self.refreshInventoryStatus)
        self.inventory_status_timer.start()
        QtCore.QTimer.singleShot(500, self.refreshInventoryStatus)
        self.startGoogleSheetMonitor()

        self.audio_settings = self._load_audio_settings()

        self.file_explorer_tree_view.add_context_menu("分割音频",self.splitAudio)


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
        self.manager_buttons_layout = QtWidgets.QHBoxLayout()
        self.manager_buttons_layout.setObjectName("manager_buttons_layout")
        self.google_sheet_monitor_btn = QtWidgets.QPushButton(self.common_tools)
        self.google_sheet_monitor_btn.setObjectName("google_sheet_monitor_btn")
        self.inventory_manager_btn = QtWidgets.QPushButton(self.common_tools)
        self.inventory_manager_btn.setObjectName("inventory_manager_btn")
        self.manager_buttons_layout.addWidget(self.google_sheet_monitor_btn)
        self.manager_buttons_layout.addWidget(self.inventory_manager_btn)
        insert_index = self.verticalLayout.indexOf(self.tidy_task_result_btn) + 1
        self.verticalLayout.insertLayout(insert_index, self.manager_buttons_layout)
        self.updateGoogleSheetMonitorButton()
        self.inventory_manager_btn.setText("库存管理器")

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

    def openInventoryManager(self):
        dialog = InventoryManagerDialog(self.inventory_store, self)
        dialog.changed.connect(self.refreshInventoryStatus)
        dialog.exec_()
        self.refreshInventoryStatus()

    def refreshInventoryStatus(self):
        try:
            summary = self.inventory_store.summary()
        except (OSError, ValueError) as error:
            message = str(error)
            self.inventory_manager_btn.setText("库存管理器：异常")
            self.inventory_manager_btn.setStyleSheet("background:#D93025;color:white;font-weight:bold;")
            self.inventory_manager_btn.setToolTip(message)
            if message != self._inventory_error:
                self.appendLog(message)
                self.showDesktopNotification("库存记录异常", message, critical=True)
                self._inventory_error = message
            return

        self._inventory_error = ""
        alerts = [item for item in summary["items"] if item["status"] != STATUS_NORMAL]
        signature = tuple(sorted((item["id"], item["status"]) for item in alerts))
        worst = summary["worst"]
        if worst == STATUS_CRITICAL:
            count = summary["counts"][STATUS_CRITICAL]
            self.inventory_manager_btn.setText("库存高危 {}".format(count))
            self.inventory_manager_btn.setStyleSheet("background:#D93025;color:white;font-weight:bold;")
        elif worst == STATUS_MODERATE:
            count = summary["counts"][STATUS_MODERATE]
            self.inventory_manager_btn.setText("库存中度报警 {}".format(count))
            self.inventory_manager_btn.setStyleSheet("background:#F29900;color:#202124;font-weight:bold;")
        elif worst == STATUS_INITIAL:
            count = summary["counts"][STATUS_INITIAL]
            self.inventory_manager_btn.setText("库存初步报警 {}".format(count))
            self.inventory_manager_btn.setStyleSheet("background:#FFF3A8;color:#202124;font-weight:bold;")
        else:
            self.inventory_manager_btn.setText("库存管理器")
            self.inventory_manager_btn.setStyleSheet("")
        self.inventory_manager_btn.setToolTip(
            "黄色不足2天，橙色不足1天，红色已经耗尽"
        )

        if alerts and signature != self._inventory_alert_signature:
            names = "、".join(
                "{}（{}）".format(item["name"], STATUS_LABELS[item["status"]])
                for item in alerts[:5]
            )
            if len(alerts) > 5:
                names += "等 {} 项".format(len(alerts))
            self.showDesktopNotification(
                "库存报警",
                names,
                critical=worst == STATUS_CRITICAL,
            )
        self._inventory_alert_signature = signature

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


    def appendLog(self,text,end=""):
        content = f"{text}{end}".rstrip("\r\n")
        if not content:
            return
        cursor = self.log_text_edit.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        if not self.log_text_edit.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(content)
        self.log_text_edit.setTextCursor(cursor)
        self.log_text_edit.ensureCursorVisible()


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
        selected_mode = 'manual'
        try:
            dialog = UpdatedFilesDetectionDialog(updated_files, self)
            dialog.exec_()
            selected_mode = dialog.selected_mode
        except BaseException as error:
            self.appendLog(
                f'显示更新文件列表失败，已切换为手动审核：{error}',
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
        }
        self.appendLog(
            f'本轮视频处理方式：{mode_names.get(selected_mode, selected_mode)}',
            end='',
        )

    def onTaskResultCompleted(self, result):
        if bool(load_task_result_config(self.load_config()).get('open_result_dir', True)):
            for directory in result.get('result_dirs', []):
                path = pathlib.Path(directory)
                if path.exists():
                    try:
                        os.startfile(str(path))
                    except OSError as error:
                        self.appendLog(f'打开结果目录失败：{error}', end='')

        message = str(result.get('message') or '整理完成')
        changed_count = result.get('changed_file_count', 0)
        uploaded_count = result.get('uploaded_file_count', 0)
        details = f'{message}\n本次新增/更新：{changed_count} 个文件'
        if result.get('upload_batch'):
            details += f"\n上传批次：{result['upload_batch']}"
            details += f'\n成功同步：{uploaded_count} 个文件'
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
        self.chrome_runner.exec_()

    def launchNextChromeProfile(self):
        profile = self.chrome_runner.launch_next_profile()
        if profile:
            self.appendLog(
                f"已按顺序启动 Chrome：{profile['name']} "
                f"({profile['directory']})"
            )

    def registerChromeGlobalHotkey(self, shortcut, show_error=True):
        try:
            shortcut = normalize_hotkey_sequence(shortcut)
        except ValueError as error:
            QMessageBox.warning(self, '全局快捷键无效', str(error))
            return False

        if self.chrome_hotkey_manager.register(shortcut):
            self.chrome_global_hotkey = shortcut
            self.appendLog(
                f'[Chrome] 全局快捷键已启用：{shortcut}',
                end='',
            )
            return True

        message = (
            f'{shortcut} 无法注册，可能已被其他程序占用。'
            f' {self.chrome_hotkey_manager.last_error}'
        )
        self.appendLog(f'[Chrome] 全局快捷键未启用：{message}', end='')
        if show_error:
            QMessageBox.warning(
                self,
                '全局快捷键注册失败',
                message,
            )
        return False

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
        settings = MainSettingDialog.get_settings(self)
        if settings:
            previous_hotkey = self.chrome_global_hotkey
            new_hotkey = settings.get(
                CHROME_NEXT_HOTKEY_CONFIG_KEY,
                previous_hotkey,
            )
            if self.registerChromeGlobalHotkey(new_hotkey):
                QMessageBox.information(
                    self,
                    '设置',
                    f'设置已保存。\n启动下一个浏览器：{self.chrome_global_hotkey}',
                )
            else:
                self.chrome_global_hotkey = previous_hotkey
                self.saveCurrentConfig()

    def closeEvent(self, event):
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
        if not self.saveCurrentConfig():
            self.startGoogleSheetMonitor()
            event.ignore()
            return

        if self.chrome_hotkey_manager is not None:
            self.chrome_hotkey_manager.close()
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
        existing_config["subtitle_include_line_breaks"] = self.subtitle_line_break_checkbox.isChecked()
        existing_config["subtitle_max_words_per_block"] = self.subtitle_max_words_spinbox.value()
        existing_config["subtitle_block_gap_ms"] = self.subtitle_gap_ms_spinbox.value()
        existing_config[self.music_ducker_settings_key] = dict(
            self.music_ducker_settings
        )
        existing_config[CHROME_NEXT_HOTKEY_CONFIG_KEY] = self.chrome_global_hotkey
        existing_config[self.google_sheet_monitor_settings_key] = dict(
            self.google_sheet_monitor_settings
        )
        
        return existing_config

    def load(self,dic):
        task_path =dic.get("task_path")
        if task_path is not None:
            self.task_path_edit.setText(task_path)
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
        try:
            self.chrome_global_hotkey = normalize_hotkey_sequence(
                dic.get(
                    CHROME_NEXT_HOTKEY_CONFIG_KEY,
                    DEFAULT_CHROME_NEXT_HOTKEY,
                )
            )
        except ValueError:
            self.chrome_global_hotkey = DEFAULT_CHROME_NEXT_HOTKEY
    
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
    
    def get_elevenlabs_api_keys(self):
        """按持久化状态筛选 Key，并轮换可用 Key。"""
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
            print(
                f"所有 ElevenLabs API Key 的额度都已用完。"
                f"距离刷新最近的 Key (...{next_key[-4:]}) 将在 "
                f"{format_unix_time(selection.get('next_retry_unix'))} 后测试。"
            )
        elif mode == 'waiting_cooldown':
            print(
                f"所有 ElevenLabs API Key 都在冷却中，最早将在 "
                f"{format_unix_time(selection.get('next_retry_unix'))} 后重试。"
            )
        elif mode == 'no_usable_keys':
            print("所有 ElevenLabs API Key 均已标记为无效，请在程序设置中处理。")
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
                )
                print(f"卡拉OK字幕已生成: {subtitle_file}")
            except Exception as e:
                print(f"生成字幕失败: {e}")

    def _createTaskAudioFile(self, task, result_file):
        settings = self.audio_settings.get(task.task_audio_type)
        if settings is None:
            return False

        model = settings.get("model")
        if model == "edge":
            CreateTTSAudio(
                task.task_audio_text,
                settings["sex"],
                str(result_file),
                pitch=settings.get("pitch", "+0Hz"),
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
                api_keys=self.get_elevenlabs_api_keys(),
                api_key_status_config=self.config_name,
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
                api_keys=self.get_elevenlabs_api_keys(),
                api_key_status_config=self.config_name,
            )
            return created is not False and result_file.exists()

        raise ValueError(f"不支持的音频模型：{model or '空'}")

    def _generateTaskAudio(self, use_task_name=False):
        root_dir = self.getTodayDir()
        if root_dir is None:
            return

        generated = 0
        existing = 0
        skipped_incomplete = 0
        skipped_audio_type = 0
        failed = 0

        for task in self.task_list:
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
                result_dir = root_dir.joinpath(task_type, task_id)
                result_dir.mkdir(parents=True, exist_ok=True)
                if use_task_name:
                    result_name = str(task.task_name or "task_audio").strip() or "task_audio"
                else:
                    result_name = "task_audio"
                result_file = result_dir / f"{result_name}.mp3"

                if result_file.exists():
                    existing += 1
                elif self._createTaskAudioFile(task, result_file):
                    generated += 1
                else:
                    failed += 1
                    self.appendLog(
                        f"生成音频失败：任务 {task_id}，请查看上方 API 日志。",
                        end="",
                    )
                    continue

                if not use_task_name:
                    self.genSrt(result_file, task)
            except Exception as error:
                failed += 1
                self.appendLog(
                    f"生成音频失败：任务 {task_id}："
                    f"{type(error).__name__}: {error}",
                    end="",
                )

        summary = (
            f"音频处理完成：新生成 {generated}，已有 {existing}，失败 {failed}"
        )
        if skipped_incomplete:
            summary += f"，跳过路径信息不完整的记录 {skipped_incomplete}"
        if skipped_audio_type:
            summary += f"，跳过未配置语音类型的记录 {skipped_audio_type}"
        self.appendLog(summary, end="")

    def genTaskAudio(self):
        self._generateTaskAudio(use_task_name=False)

    def genTaskAudio2(self):
        self._generateTaskAudio(use_task_name=True)

    def splitAudio(self,target_dir=None):

        QThreadPool.globalInstance().start(lambda: self._splitAudio(target_dir))

    def _splitAudio(self,target_dir=None):
        root_dir = target_dir
        if target_dir is None:
            root_dir = self.getTodayDir()
        max_length = self.split_len_sbox.value() * 1000  # 23 seconds in ms
        ultra_tolerance = 30 * 1000  # 23 seconds in ms

        for ext in ("*.mp3", "*.m4a"):
            for f in list(root_dir.rglob(ext)):
                if f.name.startswith("片段"):
                    continue
                print(f)
                splitAudio(f, max_length, ultra_tolerance)


    def print(self,text,end='\n'):
        self.appendLog(text, end)

    def printEmit(self,text,end='\n'):
        self.printSignal.emit(text,end)


