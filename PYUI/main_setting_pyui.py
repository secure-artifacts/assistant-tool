import copy
import json
import os
from pathlib import Path
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtWidgets import QMessageBox

from QTUI.main_setting_ui import Ui_MainSettingDialog
from PYUI.task_schema_pyui import TaskTableSchemaDialog
from model.GlobalHotkey import (
    CHROME_NEXT_HOTKEY_CONFIG_KEY,
    DEFAULT_CHROME_NEXT_HOTKEY,
    DEFAULT_LOAD_TASK_HOTKEY,
    DEFAULT_TASK_RESULT_HOTKEY,
    LOAD_TASK_HOTKEY_CONFIG_KEY,
    TASK_RESULT_HOTKEY_CONFIG_KEY,
    normalize_hotkey_sequence,
)
from model.ApiKeyHelper import (
    API_KEY_STATUSES_CONFIG_KEY,
    api_key_id,
    describe_api_key_status,
    normalize_api_keys,
    prune_api_key_statuses,
)
from model.AudioSettings import (
    AudioSettingsError,
    audio_profile_voice_count,
    build_audio_profile,
    extra_settings_json,
    normalize_audio_settings,
    voice_lines_from_profile,
)
from model.FlowParameterGuard import (
    FLOW_ASPECT_RATIOS,
    FLOW_DURATIONS,
    FLOW_GENERATION_MODES,
    FLOW_GUARD_CONFIG_KEY,
    FLOW_KEEP_ORIGINAL,
    FLOW_MODELS,
    FLOW_OUTPUT_COUNTS,
    FLOW_RESOLUTIONS,
    FLOW_VIDEO_TYPES,
    normalize_flow_guard_settings,
)
from model.TaskResultOrganizer import (
    config_bool as task_result_config_bool,
    config_list as task_result_config_list,
    config_str as task_result_config_str,
    load_effective_config as load_task_result_config,
    migrate_legacy_task_result_config,
)
from model.TaskResultExporter import DEFAULT_OUTPUT_FILENAME_MAX_LENGTH
from model.TaskTableSchema import (
    FIELD_ORDER,
    SUBMISSION_FIELD_ORDER,
    TaskTableSchemaError,
    load_task_table_schema,
    save_task_table_schema,
)
from model.SmartVideoEditor import (
    SMART_VIDEO_EDITOR_CONFIG_KEY,
    normalize_smart_video_editor_settings,
)


class AudioProfileDialog(QtWidgets.QDialog):
    def __init__(
        self,
        existing_names=(),
        original_name="",
        profile=None,
        suggested_name="",
        parent=None,
    ):
        super().__init__(parent)
        self.existing_names = {
            str(name).strip().casefold()
            for name in existing_names
            if str(name).strip()
        }
        self.original_name = str(original_name or "").strip()
        self.result_name = ""
        self.result_profile = None
        profile = profile if isinstance(profile, dict) else {}
        self.original_profile = copy.deepcopy(profile)

        self.setWindowTitle("AI 语音生成配置")
        self.resize(620, 620)
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)

        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText("本地任务表格中的语音参数名称")
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.addItem("Microsoft Edge TTS", "edge")
        self.model_combo.addItem("ElevenLabs", "elevenlabs")
        self.model_combo.addItem("ElevenLabs v3", "elevenlabs3")
        self.sex_combo = QtWidgets.QComboBox()
        self.sex_combo.setEditable(True)
        self.sex_combo.addItems(["女声", "男声"])
        self.speed_spinbox = QtWidgets.QDoubleSpinBox()
        self.speed_spinbox.setRange(0.10, 4.00)
        self.speed_spinbox.setDecimals(2)
        self.speed_spinbox.setSingleStep(0.05)
        self.speed_spinbox.setValue(1.00)
        self.pitch_edit = QtWidgets.QLineEdit("+0Hz")
        self.pitch_edit.setPlaceholderText("例如 +0Hz 或 -20Hz")
        self.stability_spinbox = QtWidgets.QDoubleSpinBox()
        self.stability_spinbox.setRange(0.00, 1.00)
        self.stability_spinbox.setDecimals(2)
        self.stability_spinbox.setSingleStep(0.05)
        self.stability_spinbox.setValue(0.35)
        self.voices_edit = QtWidgets.QPlainTextEdit()
        self.voices_edit.setMinimumHeight(130)
        self.voice_help_label = QtWidgets.QLabel()
        self.voice_help_label.setWordWrap(True)
        self.voice_help_label.setStyleSheet("color:#666;")
        self.extra_edit = QtWidgets.QPlainTextEdit()
        self.extra_edit.setMaximumHeight(110)
        self.extra_edit.setPlaceholderText("{}")

        form.addRow("配置名称：", self.name_edit)
        form.addRow("生成模型：", self.model_combo)
        form.addRow("性别/标签：", self.sex_combo)
        form.addRow("语速倍率：", self.speed_spinbox)
        form.addRow("音调：", self.pitch_edit)
        form.addRow("稳定性：", self.stability_spinbox)
        form.addRow("Voice ID 列表：", self.voices_edit)
        form.addRow("", self.voice_help_label)
        form.addRow("其他参数（JSON）：", self.extra_edit)
        layout.addLayout(form)

        note = QtWidgets.QLabel(
            "常用参数使用上面的输入框维护；无法识别的新参数会保存在“其他参数”中，"
            "因此以后扩展 JSON 字段也不会在编辑时丢失。",
            self,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        layout.addWidget(note)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.model_combo.currentIndexChanged.connect(self._update_model_fields)
        self._load_profile(suggested_name or self.original_name, profile)

    @staticmethod
    def _number(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _load_profile(self, name, profile):
        self.name_edit.setText(str(name or ""))
        model = str(profile.get("model") or "edge").strip()
        index = self.model_combo.findData(model)
        self.model_combo.setCurrentIndex(index if index >= 0 else 0)
        self.sex_combo.setEditText(str(profile.get("sex") or "女声"))
        self.speed_spinbox.setValue(self._number(profile.get("speed"), 1.0))
        self.pitch_edit.setText(str(profile.get("pitch") or "+0Hz"))
        self.stability_spinbox.setValue(
            self._number(profile.get("stability"), 0.35)
        )
        self.voices_edit.setPlainText(voice_lines_from_profile(profile))
        self.extra_edit.setPlainText(extra_settings_json(profile))
        self._update_model_fields()
        self.name_edit.setFocus()

    def _update_model_fields(self):
        model = self.model_combo.currentData()
        is_edge = model == "edge"
        is_elevenlabs = model == "elevenlabs"
        self.speed_spinbox.setEnabled(is_edge or is_elevenlabs)
        self.pitch_edit.setEnabled(is_edge)
        self.stability_spinbox.setEnabled(model == "elevenlabs3")
        self.voices_edit.setEnabled(not is_edge)
        if is_edge:
            self.voice_help_label.setText(
                "Edge TTS 目前根据“女声/男声”自动选择内置 Voice，不需要填写 Voice ID。"
            )
        elif is_elevenlabs:
            self.voice_help_label.setText(
                "每行一个 Voice ID；需要为某个 Voice 单独设置语速时，写成：Voice ID | 0.90"
            )
        else:
            self.voice_help_label.setText("每行填写一个 ElevenLabs v3 Voice ID。")

    def accept(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少名称", "请填写配置名称。")
            self.name_edit.setFocus()
            return
        folded_name = name.casefold()
        if (
            folded_name in self.existing_names
            and folded_name != self.original_name.casefold()
        ):
            QMessageBox.warning(self, "名称重复", "已经存在同名语音配置。")
            self.name_edit.setFocus()
            return
        try:
            profile = build_audio_profile(
                self.model_combo.currentData(),
                sex=self.sex_combo.currentText(),
                speed=self.speed_spinbox.value(),
                pitch=self.pitch_edit.text(),
                stability=self.stability_spinbox.value(),
                voice_lines=self.voices_edit.toPlainText(),
                extra_json=self.extra_edit.toPlainText(),
                original_profile=self.original_profile,
            )
        except (AudioSettingsError, TypeError, ValueError) as error:
            QMessageBox.warning(self, "配置无效", str(error))
            return
        self.result_name = name
        self.result_profile = profile
        super().accept()


class MainSettingDialog(QtWidgets.QDialog, Ui_MainSettingDialog):
    def __init__(self, parent=None, plugin_host=None):
        super(MainSettingDialog, self).__init__(parent)
        self.plugin_host = plugin_host
        self._chrome_managed_by_plugin = bool(
            plugin_host is not None
            and plugin_host.plugin("chrome_launcher") is not None
        )
        self.setupUi(self)
        if self._chrome_managed_by_plugin:
            # The old widgets remain as a source-run compatibility fallback,
            # but the installed application exposes this setting only through
            # the Chrome plugin page.
            self.chrome_hotkey_label.hide()
            self.chrome_hotkey_edit.hide()
            self.chrome_hotkey_help_label.hide()
        self._build_additional_hotkey_editors()
        self._build_flow_guard_tab()
        self._build_smart_video_editor_tab()
        self.audio_settings = {}
        self._build_audio_settings_editor()
        self._build_task_result_tab()
        self._build_task_schema_tab()
        self.plugin_settings_pages = []
        if self.plugin_host is not None:
            self.plugin_settings_pages = self.plugin_host.create_settings_pages(
                self,
                self.settingTabWidget,
            )
        self.api_key_statuses = {}

        # 连接按钮信号
        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        self.add_api_key_btn.clicked.connect(self.add_api_key)
        self.remove_api_key_btn.clicked.connect(self.remove_selected_api_keys)
        self.reset_api_key_status_btn.clicked.connect(self.reset_selected_api_key_statuses)
        self.elevenlabs_api_key_edit.returnPressed.connect(self.add_api_key)

        # 加载当前配置
        self.load_config()

    def load_config(self):
        """加载配置到界面"""
        hotkey = DEFAULT_CHROME_NEXT_HOTKEY
        task_result_hotkey = DEFAULT_TASK_RESULT_HOTKEY
        load_task_hotkey = DEFAULT_LOAD_TASK_HOTKEY
        flow_guard_settings = normalize_flow_guard_settings({})
        smart_video_editor_settings = normalize_smart_video_editor_settings({})
        config = load_task_result_config()
        try:
            migrate_legacy_task_result_config("config.json")
            if os.path.exists("config.json"):
                with open("config.json", "r", encoding="utf-8") as f:
                    config = load_task_result_config(json.load(f))
                    self.audio_settings = normalize_audio_settings(
                        config.get("audio_settings", {})
                    )
                    api_keys = config.get('elevenlabs_api_keys')
                    if api_keys is None:
                        api_keys = config.get('elevenlabs_api_key', '')
                    self.api_key_statuses = config.get(
                        API_KEY_STATUSES_CONFIG_KEY, {}
                    )
                    if not isinstance(self.api_key_statuses, dict):
                        self.api_key_statuses = {}
                    for api_key in normalize_api_keys(api_keys):
                        self._append_api_key(api_key)
                    hotkey = config.get(
                        CHROME_NEXT_HOTKEY_CONFIG_KEY,
                        DEFAULT_CHROME_NEXT_HOTKEY,
                    )
                    task_result_hotkey = config.get(
                        TASK_RESULT_HOTKEY_CONFIG_KEY,
                        DEFAULT_TASK_RESULT_HOTKEY,
                    )
                    load_task_hotkey = config.get(
                        LOAD_TASK_HOTKEY_CONFIG_KEY,
                        DEFAULT_LOAD_TASK_HOTKEY,
                    )
                    flow_guard_settings = normalize_flow_guard_settings(
                        config.get(FLOW_GUARD_CONFIG_KEY)
                    )
                    smart_video_editor_settings = normalize_smart_video_editor_settings(
                        config.get(SMART_VIDEO_EDITOR_CONFIG_KEY)
                    )
            self._load_task_result_config(config)
        except Exception as e:
            print(f"加载配置失败: {e}")
            self._load_task_result_config(config)
        self._refresh_audio_settings_table()
        try:
            hotkey = normalize_hotkey_sequence(hotkey)
        except ValueError:
            hotkey = DEFAULT_CHROME_NEXT_HOTKEY
        self.chrome_hotkey_edit.setKeySequence(QtGui.QKeySequence(hotkey))
        try:
            task_result_hotkey = normalize_hotkey_sequence(task_result_hotkey)
        except ValueError:
            task_result_hotkey = DEFAULT_TASK_RESULT_HOTKEY
        self.task_result_hotkey_edit.setKeySequence(
            QtGui.QKeySequence(task_result_hotkey)
        )
        try:
            load_task_hotkey = normalize_hotkey_sequence(load_task_hotkey)
        except ValueError:
            load_task_hotkey = DEFAULT_LOAD_TASK_HOTKEY
        self.load_task_hotkey_edit.setKeySequence(QtGui.QKeySequence(load_task_hotkey))
        self._load_flow_guard_settings(flow_guard_settings)
        self._load_smart_video_editor_settings(smart_video_editor_settings)
        if self.plugin_host is not None:
            self.plugin_host.load_settings_pages(config)

    def _build_additional_hotkey_editors(self):
        insert_index = max(0, self.hotkey_layout.count() - 1)

        self.task_result_hotkey_label = QtWidgets.QLabel(
            '整理任务结果的全局快捷键：',
            self.hotkey_tab,
        )
        self.task_result_hotkey_edit = QtWidgets.QKeySequenceEdit(self.hotkey_tab)
        self.task_result_hotkey_help_label = QtWidgets.QLabel(
            '在其他软件中也可直接开始整理与上传；任务正在运行时不会重复启动。',
            self.hotkey_tab,
        )
        self.task_result_hotkey_help_label.setWordWrap(True)

        self.load_task_hotkey_label = QtWidgets.QLabel(
            '加载当前任务的全局快捷键：',
            self.hotkey_tab,
        )
        self.load_task_hotkey_edit = QtWidgets.QKeySequenceEdit(self.hotkey_tab)
        self.load_task_hotkey_help_label = QtWidgets.QLabel(
            '按主界面当前的任务路径和目标日期重新加载任务列表。',
            self.hotkey_tab,
        )
        self.load_task_hotkey_help_label.setWordWrap(True)

        for widget in (
            self.task_result_hotkey_label,
            self.task_result_hotkey_edit,
            self.task_result_hotkey_help_label,
            self.load_task_hotkey_label,
            self.load_task_hotkey_edit,
            self.load_task_hotkey_help_label,
        ):
            self.hotkey_layout.insertWidget(insert_index, widget)
            insert_index += 1

    def _build_flow_guard_tab(self):
        self.flow_guard_tab = QtWidgets.QWidget(self.settingTabWidget)
        layout = QtWidgets.QVBoxLayout(self.flow_guard_tab)

        self.flow_guard_enabled_checkbox = QtWidgets.QCheckBox(
            "启用 Flow 参数守卫",
            self.flow_guard_tab,
        )
        self.flow_guard_enabled_checkbox.setToolTip(
            "由本程序统一检查所有普通 Chrome 窗口，不需要为每个浏览器配置安装插件"
        )
        layout.addWidget(self.flow_guard_enabled_checkbox)

        form = QtWidgets.QFormLayout()

        def create_target_combo(options):
            combo = QtWidgets.QComboBox(self.flow_guard_tab)
            combo.addItems([FLOW_KEEP_ORIGINAL, *options])
            combo.setToolTip("选择“保持原样”时，守卫不会修改这一项")
            return combo

        self.flow_guard_generation_mode_combo = create_target_combo(
            FLOW_GENERATION_MODES
        )
        form.addRow("模式：", self.flow_guard_generation_mode_combo)

        self.flow_guard_video_type_combo = create_target_combo(FLOW_VIDEO_TYPES)
        form.addRow("视频类型：", self.flow_guard_video_type_combo)

        self.flow_guard_aspect_ratio_combo = create_target_combo(
            FLOW_ASPECT_RATIOS
        )
        form.addRow("宽高比：", self.flow_guard_aspect_ratio_combo)

        self.flow_guard_model_combo = create_target_combo(FLOW_MODELS)
        form.addRow("模型：", self.flow_guard_model_combo)

        self.flow_guard_resolution_combo = create_target_combo(FLOW_RESOLUTIONS)
        form.addRow("视频分辨率：", self.flow_guard_resolution_combo)

        self.flow_guard_duration_combo = create_target_combo(FLOW_DURATIONS)
        form.addRow("视频时长：", self.flow_guard_duration_combo)

        self.flow_guard_output_count_combo = create_target_combo(
            FLOW_OUTPUT_COUNTS
        )
        form.addRow("输出数量：", self.flow_guard_output_count_combo)

        self.flow_guard_poll_spinbox = QtWidgets.QDoubleSpinBox(self.flow_guard_tab)
        self.flow_guard_poll_spinbox.setRange(0.5, 30.0)
        self.flow_guard_poll_spinbox.setDecimals(1)
        self.flow_guard_poll_spinbox.setSingleStep(0.5)
        self.flow_guard_poll_spinbox.setSuffix(" 秒")
        form.addRow("检查间隔：", self.flow_guard_poll_spinbox)
        layout.addLayout(form)

        self.flow_guard_new_page_checkbox = QtWidgets.QCheckBox(
            "主动展开设置检查（不推荐）",
            self.flow_guard_tab,
        )
        self.flow_guard_new_page_checkbox.setToolTip(
            "默认关闭。开启后会在进入 Flow 项目时短暂展开设置并立即收起；"
            "为避免干扰网页生成，建议保持关闭，手动展开设置时仍会自动纠正"
        )
        layout.addWidget(self.flow_guard_new_page_checkbox)

        help_label = QtWidgets.QLabel(
            "仅处理标题中包含“Google Flow”的 Chrome 窗口。每一项都可以设为"
            "“保持原样”；Flow 会根据模式和模型只显示兼容选项，当前不可用的目标"
            "会跳过。发生自动修正时，会在主程序日志和系统通知中留下记录。",
            self.flow_guard_tab,
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color:#666;")
        layout.addWidget(help_label)
        layout.addStretch(1)
        self.settingTabWidget.addTab(self.flow_guard_tab, "Flow 守卫")

        self.flow_guard_enabled_checkbox.toggled.connect(
            self._update_flow_guard_enabled_state
        )

    def _update_flow_guard_enabled_state(self):
        enabled = self.flow_guard_enabled_checkbox.isChecked()
        for widget in (
            self.flow_guard_generation_mode_combo,
            self.flow_guard_video_type_combo,
            self.flow_guard_aspect_ratio_combo,
            self.flow_guard_model_combo,
            self.flow_guard_resolution_combo,
            self.flow_guard_duration_combo,
            self.flow_guard_output_count_combo,
            self.flow_guard_poll_spinbox,
            self.flow_guard_new_page_checkbox,
        ):
            widget.setEnabled(enabled)

    def _load_flow_guard_settings(self, settings):
        settings = normalize_flow_guard_settings(settings)
        self.flow_guard_enabled_checkbox.setChecked(settings["enabled"])
        self.flow_guard_generation_mode_combo.setCurrentText(
            settings["generation_mode"]
        )
        self.flow_guard_video_type_combo.setCurrentText(settings["video_type"])
        self.flow_guard_aspect_ratio_combo.setCurrentText(settings["aspect_ratio"])
        self.flow_guard_model_combo.setCurrentText(settings["model"])
        self.flow_guard_resolution_combo.setCurrentText(settings["resolution"])
        self.flow_guard_duration_combo.setCurrentText(settings["duration"])
        self.flow_guard_output_count_combo.setCurrentText(
            settings["output_count"]
        )
        self.flow_guard_poll_spinbox.setValue(settings["poll_seconds"])
        self.flow_guard_new_page_checkbox.setChecked(
            settings["actively_open_settings"]
        )
        self._update_flow_guard_enabled_state()

    def _get_flow_guard_settings(self):
        return normalize_flow_guard_settings(
            {
                "enabled": self.flow_guard_enabled_checkbox.isChecked(),
                "generation_mode": (
                    self.flow_guard_generation_mode_combo.currentText()
                ),
                "video_type": self.flow_guard_video_type_combo.currentText(),
                "aspect_ratio": self.flow_guard_aspect_ratio_combo.currentText(),
                "model": self.flow_guard_model_combo.currentText(),
                "resolution": self.flow_guard_resolution_combo.currentText(),
                "duration": self.flow_guard_duration_combo.currentText(),
                "output_count": (
                    self.flow_guard_output_count_combo.currentText()
                ),
                "poll_seconds": self.flow_guard_poll_spinbox.value(),
                "actively_open_settings": (
                    self.flow_guard_new_page_checkbox.isChecked()
                ),
            }
        )

    def _build_smart_video_editor_tab(self):
        self.smart_video_editor_tab = QtWidgets.QWidget(self.settingTabWidget)
        layout = QtWidgets.QVBoxLayout(self.smart_video_editor_tab)
        scroll = QtWidgets.QScrollArea(self.smart_video_editor_tab)
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget(scroll)
        form = QtWidgets.QFormLayout(content)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)

        self.smart_lead_padding_spinbox = QtWidgets.QSpinBox(content)
        self.smart_lead_padding_spinbox.setRange(0, 3000)
        self.smart_lead_padding_spinbox.setSuffix(" ms")
        self.smart_tail_padding_spinbox = QtWidgets.QSpinBox(content)
        self.smart_tail_padding_spinbox.setRange(0, 3000)
        self.smart_tail_padding_spinbox.setSuffix(" ms")
        form.addRow("片段开头留白：", self.smart_lead_padding_spinbox)
        form.addRow("片段结尾留白：", self.smart_tail_padding_spinbox)

        self.smart_silence_detection_checkbox = QtWidgets.QCheckBox(
            "启用分贝静音检测（推荐；自动切点必须有音量证据）", content
        )
        self.smart_silence_detection_checkbox.setToolTip(
            "Whisper 只负责定位说到哪个单词；实际首尾切点必须位于 FFmpeg "
            "检测到的低音量区间。找不到静音时会保留原片并提示核对。"
        )
        self.smart_silence_db_spinbox = QtWidgets.QSpinBox(content)
        self.smart_silence_db_spinbox.setRange(-80, -5)
        self.smart_silence_db_spinbox.setSuffix(" dB")
        self.smart_silence_db_spinbox.setToolTip(
            "越接近 0 越容易把较小声音当作静音；如果误剪说话，请调低，"
            "例如从 -35 调到 -42 dB。"
        )
        self.smart_min_silence_spinbox = QtWidgets.QSpinBox(content)
        self.smart_min_silence_spinbox.setRange(80, 5000)
        self.smart_min_silence_spinbox.setSingleStep(50)
        self.smart_min_silence_spinbox.setSuffix(" ms")
        self.smart_boundary_search_spinbox = QtWidgets.QSpinBox(content)
        self.smart_boundary_search_spinbox.setRange(100, 10000)
        self.smart_boundary_search_spinbox.setSingleStep(100)
        self.smart_boundary_search_spinbox.setSuffix(" ms")
        self.smart_boundary_search_spinbox.setToolTip(
            "只在首词之前、尾词之后的这个范围内寻找静音边界。"
        )
        form.addRow("", self.smart_silence_detection_checkbox)
        form.addRow("静音音量阈值：", self.smart_silence_db_spinbox)
        form.addRow("最短静音时长：", self.smart_min_silence_spinbox)
        form.addRow("切点搜索范围：", self.smart_boundary_search_spinbox)

        self.smart_compress_pauses_checkbox = QtWidgets.QCheckBox(
            "实验性：压缩句子内部的过长停顿（默认关闭）", content
        )
        self.smart_compress_pauses_checkbox.setToolTip(
            "正常智能剪辑已经会删除每段开头和结尾的气口。句内停顿可能包含"
            "Whisper 漏掉的单词，开启后存在误剪风险。"
        )
        self.smart_pause_threshold_spinbox = QtWidgets.QSpinBox(content)
        self.smart_pause_threshold_spinbox.setRange(300, 10000)
        self.smart_pause_threshold_spinbox.setSingleStep(100)
        self.smart_pause_threshold_spinbox.setSuffix(" ms")
        self.smart_retained_pause_spinbox = QtWidgets.QSpinBox(content)
        self.smart_retained_pause_spinbox.setRange(0, 5000)
        self.smart_retained_pause_spinbox.setSingleStep(50)
        self.smart_retained_pause_spinbox.setSuffix(" ms")
        form.addRow("", self.smart_compress_pauses_checkbox)
        form.addRow("超过此时长才压缩：", self.smart_pause_threshold_spinbox)
        form.addRow("压缩后保留：", self.smart_retained_pause_spinbox)

        self.smart_pass_similarity_spinbox = QtWidgets.QSpinBox(content)
        self.smart_pass_similarity_spinbox.setRange(40, 100)
        self.smart_pass_similarity_spinbox.setSuffix(" %")
        self.smart_severe_similarity_spinbox = QtWidgets.QSpinBox(content)
        self.smart_severe_similarity_spinbox.setRange(0, 95)
        self.smart_severe_similarity_spinbox.setSuffix(" %")
        form.addRow("自动通过相似度：", self.smart_pass_similarity_spinbox)
        form.addRow("严重异常低于：", self.smart_severe_similarity_spinbox)

        subtitle_note = QtWidgets.QLabel(
            "智能剪辑会对最终成片调用主界面的原字幕功能重新强制对齐，"
            "并使用主界面当前的换行、每块最多单词和字幕块间隔参数。",
            content,
        )
        subtitle_note.setWordWrap(True)
        subtitle_note.setStyleSheet("color:#555;")
        form.addRow("字幕生成：", subtitle_note)

        self.smart_output_folder_edit = QtWidgets.QLineEdit(content)
        self.smart_ffmpeg_path_edit = QtWidgets.QLineEdit(content)
        self.smart_ffmpeg_path_edit.setPlaceholderText(
            "留空时使用整理任务结果的编码器或系统 ffmpeg"
        )
        browse_row = QtWidgets.QWidget(content)
        browse_layout = QtWidgets.QHBoxLayout(browse_row)
        browse_layout.setContentsMargins(0, 0, 0, 0)
        browse_layout.addWidget(self.smart_ffmpeg_path_edit, 1)
        browse_button = QtWidgets.QPushButton("浏览…", browse_row)
        browse_button.clicked.connect(self._browse_smart_ffmpeg)
        browse_layout.addWidget(browse_button)
        self.smart_existing_output_combo = QtWidgets.QComboBox(content)
        self.smart_existing_output_combo.addItem("保留旧文件并生成新版本", "version")
        self.smart_existing_output_combo.addItem("覆盖旧输出", "overwrite")
        self.smart_existing_output_combo.addItem("已有输出时跳过", "skip")
        self.smart_auto_export_checkbox = QtWidgets.QCheckBox(
            "没有发生裁切且全部通过时直接导出；有裁切或异常时打开核对界面", content
        )
        form.addRow("输出目录名：", self.smart_output_folder_edit)
        form.addRow("FFmpeg 编码器：", browse_row)
        form.addRow("已有输出：", self.smart_existing_output_combo)
        form.addRow("", self.smart_auto_export_checkbox)

        note = QtWidgets.QLabel(
            "该功能从任务列表右键菜单启动：以任务语音文案为正确内容，Whisper 只负责"
            "定位和核对。原视频永不覆盖，分析报告和识别缓存保存在输出目录中。"
            "正常模式同时要求单词位置和分贝静音证据，找不到可靠静音就保留原片。"
            "句内停顿压缩属于实验功能，并且也必须通过分贝检测，默认关闭。",
            content,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        form.addRow(note)

        scroll.setWidget(content)
        layout.addWidget(scroll)
        self.settingTabWidget.addTab(self.smart_video_editor_tab, "智能剪辑")
        self.smart_compress_pauses_checkbox.toggled.connect(
            self._update_smart_video_editor_enabled_state
        )
        self.smart_silence_detection_checkbox.toggled.connect(
            self._update_smart_video_editor_enabled_state
        )

    def _browse_smart_ffmpeg(self):
        current = self.smart_ffmpeg_path_edit.text().strip()
        selected, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择 FFmpeg 编码器",
            current,
            "可执行文件 (*.exe);;所有文件 (*)",
        )
        if selected:
            self.smart_ffmpeg_path_edit.setText(selected)

    def _load_smart_video_editor_settings(self, settings):
        settings = normalize_smart_video_editor_settings(settings)
        self.smart_lead_padding_spinbox.setValue(settings["lead_padding_ms"])
        self.smart_tail_padding_spinbox.setValue(settings["tail_padding_ms"])
        self.smart_silence_detection_checkbox.setChecked(
            settings["silence_detection_enabled"]
        )
        self.smart_silence_db_spinbox.setValue(settings["silence_threshold_db"])
        self.smart_min_silence_spinbox.setValue(settings["min_silence_ms"])
        self.smart_boundary_search_spinbox.setValue(settings["boundary_search_ms"])
        self.smart_compress_pauses_checkbox.setChecked(
            settings["compress_internal_pauses"]
        )
        self.smart_pause_threshold_spinbox.setValue(settings["pause_threshold_ms"])
        self.smart_retained_pause_spinbox.setValue(settings["retained_pause_ms"])
        self.smart_pass_similarity_spinbox.setValue(
            settings["pass_similarity_percent"]
        )
        self.smart_severe_similarity_spinbox.setValue(
            settings["severe_similarity_percent"]
        )
        self.smart_output_folder_edit.setText(settings["output_folder_name"])
        self.smart_ffmpeg_path_edit.setText(settings["ffmpeg_path"])
        index = self.smart_existing_output_combo.findData(
            settings["existing_output"]
        )
        self.smart_existing_output_combo.setCurrentIndex(index if index >= 0 else 0)
        self.smart_auto_export_checkbox.setChecked(settings["auto_export_clean"])
        self._update_smart_video_editor_enabled_state()

    def _update_smart_video_editor_enabled_state(self):
        silence_enabled = self.smart_silence_detection_checkbox.isChecked()
        self.smart_silence_db_spinbox.setEnabled(silence_enabled)
        self.smart_min_silence_spinbox.setEnabled(silence_enabled)
        self.smart_boundary_search_spinbox.setEnabled(silence_enabled)
        pause_enabled = (
            silence_enabled and self.smart_compress_pauses_checkbox.isChecked()
        )
        self.smart_pause_threshold_spinbox.setEnabled(pause_enabled)
        self.smart_retained_pause_spinbox.setEnabled(pause_enabled)

    def _get_smart_video_editor_settings(self):
        return normalize_smart_video_editor_settings({
            "lead_padding_ms": self.smart_lead_padding_spinbox.value(),
            "tail_padding_ms": self.smart_tail_padding_spinbox.value(),
            "silence_detection_enabled": (
                self.smart_silence_detection_checkbox.isChecked()
            ),
            "silence_threshold_db": self.smart_silence_db_spinbox.value(),
            "min_silence_ms": self.smart_min_silence_spinbox.value(),
            "boundary_search_ms": self.smart_boundary_search_spinbox.value(),
            "compress_internal_pauses": self.smart_compress_pauses_checkbox.isChecked(),
            "internal_pause_mode": (
                "experimental"
                if self.smart_compress_pauses_checkbox.isChecked()
                else "off"
            ),
            "pause_threshold_ms": self.smart_pause_threshold_spinbox.value(),
            "retained_pause_ms": self.smart_retained_pause_spinbox.value(),
            "pass_similarity_percent": self.smart_pass_similarity_spinbox.value(),
            "severe_similarity_percent": self.smart_severe_similarity_spinbox.value(),
            "output_folder_name": self.smart_output_folder_edit.text().strip(),
            "ffmpeg_path": self.smart_ffmpeg_path_edit.text().strip(),
            "existing_output": self.smart_existing_output_combo.currentData(),
            "auto_export_clean": self.smart_auto_export_checkbox.isChecked(),
        })

    @staticmethod
    def _section_label(text, parent):
        label = QtWidgets.QLabel(text, parent)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _build_audio_settings_editor(self):
        table = self.tableWidget
        table.setColumnCount(7)
        table.setHorizontalHeaderLabels(
            ["名称", "模型", "Voice 数量", "语速", "音调", "稳定性", "性别/标签"]
        )
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        table.doubleClicked.connect(
            lambda index: self.edit_audio_profile(index.row())
        )

        button_widget = QtWidgets.QWidget(self.tab_3)
        button_layout = QtWidgets.QHBoxLayout(button_widget)
        button_layout.setContentsMargins(0, 0, 0, 0)
        self.add_audio_profile_btn = QtWidgets.QPushButton("新增")
        self.edit_audio_profile_btn = QtWidgets.QPushButton("查看/编辑")
        self.copy_audio_profile_btn = QtWidgets.QPushButton("复制")
        self.remove_audio_profile_btn = QtWidgets.QPushButton("删除")
        button_layout.addWidget(self.add_audio_profile_btn)
        button_layout.addWidget(self.edit_audio_profile_btn)
        button_layout.addWidget(self.copy_audio_profile_btn)
        button_layout.addWidget(self.remove_audio_profile_btn)
        button_layout.addStretch()
        self.gridLayout_2.addWidget(button_widget, 1, 0)

        help_label = QtWidgets.QLabel(
            "双击配置可查看或修改。这里保存的名称应与本地任务表格中的语音参数完全一致。",
            self.tab_3,
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color:#666;")
        self.gridLayout_2.addWidget(help_label, 2, 0)

        self.add_audio_profile_btn.clicked.connect(self.add_audio_profile)
        self.edit_audio_profile_btn.clicked.connect(
            lambda: self.edit_audio_profile()
        )
        self.copy_audio_profile_btn.clicked.connect(self.copy_audio_profile)
        self.remove_audio_profile_btn.clicked.connect(self.remove_audio_profile)
        table.itemSelectionChanged.connect(self._update_audio_profile_buttons)
        self._update_audio_profile_buttons()

    @staticmethod
    def _audio_number_text(value):
        try:
            return "{:g}".format(float(value))
        except (TypeError, ValueError):
            return ""

    def _refresh_audio_settings_table(self, selected_name=""):
        table = self.tableWidget
        table.setRowCount(0)
        selected_row = -1
        for row, (name, profile) in enumerate(self.audio_settings.items()):
            table.insertRow(row)
            values = (
                name,
                str(profile.get("model") or ""),
                str(audio_profile_voice_count(profile)),
                self._audio_number_text(profile.get("speed")),
                str(profile.get("pitch") or ""),
                self._audio_number_text(profile.get("stability")),
                str(profile.get("sex") or ""),
            )
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if column in (2, 3, 4, 5):
                    item.setTextAlignment(QtCore.Qt.AlignCenter)
                table.setItem(row, column, item)
            if name == selected_name:
                selected_row = row
        if selected_row >= 0:
            table.selectRow(selected_row)
            table.scrollToItem(table.item(selected_row, 0))
        self._update_audio_profile_buttons()

    def _selected_audio_profile_name(self):
        row = self.tableWidget.currentRow()
        if row < 0:
            return ""
        item = self.tableWidget.item(row, 0)
        return item.text() if item is not None else ""

    def _update_audio_profile_buttons(self):
        has_selection = bool(self._selected_audio_profile_name())
        self.edit_audio_profile_btn.setEnabled(has_selection)
        self.copy_audio_profile_btn.setEnabled(has_selection)
        self.remove_audio_profile_btn.setEnabled(has_selection)

    def _open_audio_profile_dialog(
        self,
        original_name="",
        profile=None,
        suggested_name="",
    ):
        dialog = AudioProfileDialog(
            existing_names=self.audio_settings,
            original_name=original_name,
            profile=profile,
            suggested_name=suggested_name,
            parent=self,
        )
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.result_name, dialog.result_profile

    def add_audio_profile(self):
        result = self._open_audio_profile_dialog(suggested_name="新语音配置")
        if result is None:
            return
        name, profile = result
        self.audio_settings[name] = profile
        self._refresh_audio_settings_table(name)

    def edit_audio_profile(self, row=None):
        if isinstance(row, int) and row >= 0:
            self.tableWidget.selectRow(row)
        original_name = self._selected_audio_profile_name()
        if not original_name:
            return
        result = self._open_audio_profile_dialog(
            original_name=original_name,
            profile=self.audio_settings[original_name],
        )
        if result is None:
            return
        name, profile = result
        updated = {}
        for existing_name, existing_profile in self.audio_settings.items():
            if existing_name == original_name:
                updated[name] = profile
            else:
                updated[existing_name] = existing_profile
        self.audio_settings = updated
        self._refresh_audio_settings_table(name)

    def copy_audio_profile(self):
        original_name = self._selected_audio_profile_name()
        if not original_name:
            return
        suggested_name = "{} - 副本".format(original_name)
        suffix = 2
        while suggested_name.casefold() in {
            name.casefold() for name in self.audio_settings
        }:
            suggested_name = "{} - 副本 {}".format(original_name, suffix)
            suffix += 1
        result = self._open_audio_profile_dialog(
            profile=copy.deepcopy(self.audio_settings[original_name]),
            suggested_name=suggested_name,
        )
        if result is None:
            return
        name, profile = result
        self.audio_settings[name] = profile
        self._refresh_audio_settings_table(name)

    def remove_audio_profile(self):
        name = self._selected_audio_profile_name()
        if not name:
            return
        answer = QMessageBox.question(
            self,
            "删除语音配置",
            "确定删除“{}”吗？\n引用这个名称的任务将无法生成音频。".format(name),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.audio_settings.pop(name, None)
        self._refresh_audio_settings_table()

    def _build_task_result_tab(self):
        self.resize(max(self.width(), 860), max(self.height(), 680))
        self.task_result_tab = QtWidgets.QWidget()
        tab_layout = QtWidgets.QVBoxLayout(self.task_result_tab)

        scroll = QtWidgets.QScrollArea(self.task_result_tab)
        scroll.setWidgetResizable(True)
        scroll_content = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(scroll_content)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        form.addRow(self._section_label('流程', scroll_content))
        self.task_result_run_export_checkbox = QtWidgets.QCheckBox('整理本地结果文件')
        self.task_result_run_upload_checkbox = QtWidgets.QCheckBox('上传到 Google Drive')
        self.task_result_only_changed_checkbox = QtWidgets.QCheckBox('只上传本次新增或更新的文件')
        self.task_result_open_dir_checkbox = QtWidgets.QCheckBox('完成后打开 result 目录')
        self.task_result_wsp_checkbox = QtWidgets.QCheckBox('同时整理 WSP 版本')
        flow_widget = QtWidgets.QWidget(scroll_content)
        flow_layout = QtWidgets.QGridLayout(flow_widget)
        flow_layout.setContentsMargins(0, 0, 0, 0)
        flow_layout.addWidget(self.task_result_run_export_checkbox, 0, 0)
        flow_layout.addWidget(self.task_result_run_upload_checkbox, 0, 1)
        flow_layout.addWidget(self.task_result_only_changed_checkbox, 1, 0)
        flow_layout.addWidget(self.task_result_open_dir_checkbox, 1, 1)
        flow_layout.addWidget(self.task_result_wsp_checkbox, 2, 0)
        form.addRow(flow_widget)
        self.task_result_filename_max_length_spinbox = QtWidgets.QSpinBox()
        self.task_result_filename_max_length_spinbox.setRange(0, 255)
        self.task_result_filename_max_length_spinbox.setSpecialValueText(
            '不限制（旧规则）'
        )
        self.task_result_filename_max_length_spinbox.setSuffix(' 个字符')
        self.task_result_filename_max_length_spinbox.setToolTip(
            '包含扩展名；默认 50。新生成的成品采用短名称，同目录已存在的旧长名称会继续识别，'
            '不会因此重复整理或上传。'
        )
        form.addRow(
            '成品文件名上限：',
            self.task_result_filename_max_length_spinbox,
        )

        form.addRow(self._section_label('视频审核与分流', scroll_content))
        self.task_result_detection_checkbox = QtWidgets.QCheckBox('检测视频中的目标元素并分流')
        self.task_result_detection_mode_combo = QtWidgets.QComboBox()
        self.task_result_detection_mode_combo.addItem('每次运行时询问', 'ask')
        self.task_result_detection_mode_combo.addItem('AI 自动检测', 'ai')
        self.task_result_detection_mode_combo.addItem('不检测，全部按未命中处理', 'skip')
        self.task_result_detection_mode_combo.addItem('逐个人工审核抽帧图', 'manual')
        self.task_result_review_confidence_spinbox = QtWidgets.QDoubleSpinBox()
        self.task_result_review_confidence_spinbox.setRange(0.0, 1.0)
        self.task_result_review_confidence_spinbox.setSingleStep(0.05)
        self.task_result_review_confidence_spinbox.setDecimals(2)
        self.task_result_failure_review_checkbox = QtWidgets.QCheckBox('检测失败时放入审核目录')
        self.task_result_review_folder_edit = QtWidgets.QLineEdit()
        form.addRow('', self.task_result_detection_checkbox)
        form.addRow('检测方式：', self.task_result_detection_mode_combo)
        form.addRow('判定阈值：', self.task_result_review_confidence_spinbox)
        form.addRow('', self.task_result_failure_review_checkbox)
        form.addRow('审核目录名：', self.task_result_review_folder_edit)

        form.addRow(self._section_label('AI 检测', scroll_content))
        self.task_result_target_name_edit = QtWidgets.QLineEdit()
        self.task_result_target_name_edit.setPlaceholderText('例如：目标元素')
        self.task_result_target_description_edit = QtWidgets.QPlainTextEdit()
        self.task_result_target_description_edit.setMaximumHeight(80)
        self.task_result_target_description_edit.setPlaceholderText('说明需要识别什么，以及哪些相似情况不应命中')
        self.task_result_detection_rules_edit = QtWidgets.QPlainTextEdit()
        self.task_result_detection_rules_edit.setMaximumHeight(100)
        self.task_result_detection_rules_edit.setPlaceholderText('每行一条判定规则')
        self.task_result_positive_keywords_edit = QtWidgets.QLineEdit()
        self.task_result_positive_keywords_edit.setPlaceholderText('多个词用逗号或分号分隔')
        self.task_result_negative_keywords_edit = QtWidgets.QLineEdit()
        self.task_result_negative_keywords_edit.setPlaceholderText('多个词用逗号或分号分隔')
        self.task_result_ambiguous_keywords_edit = QtWidgets.QLineEdit()
        self.task_result_ambiguous_keywords_edit.setPlaceholderText('多个词用逗号或分号分隔')
        self.task_result_report_name_edit = QtWidgets.QLineEdit()
        self.task_result_report_name_edit.setPlaceholderText('视频检测报告.json')
        self.task_result_gemini_model_edit = QtWidgets.QLineEdit()
        self.task_result_gemini_keys_edit = QtWidgets.QLineEdit()
        self.task_result_gemini_keys_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.task_result_gemini_keys_edit.setPlaceholderText('多个 Key 用逗号或分号分隔')
        form.addRow('目标名称：', self.task_result_target_name_edit)
        form.addRow('目标说明：', self.task_result_target_description_edit)
        form.addRow('判定规则：', self.task_result_detection_rules_edit)
        form.addRow('明确命中词：', self.task_result_positive_keywords_edit)
        form.addRow('排除词：', self.task_result_negative_keywords_edit)
        form.addRow('模糊描述词：', self.task_result_ambiguous_keywords_edit)
        form.addRow('检测报告名：', self.task_result_report_name_edit)
        form.addRow('Gemini 模型：', self.task_result_gemini_model_edit)
        form.addRow('Gemini API Keys：', self.task_result_gemini_keys_edit)

        form.addRow(self._section_label('视频压缩', scroll_content))
        self.task_result_compress_checkbox = QtWidgets.QCheckBox('上传前按关键词压缩视频')
        self.task_result_compress_keywords_edit = QtWidgets.QLineEdit()
        self.task_result_compress_keywords_edit.setPlaceholderText('多个关键词用逗号或分号分隔')
        self.task_result_shana_ffmpeg_edit = QtWidgets.QLineEdit()
        self.task_result_shana_preset_edit = QtWidgets.QLineEdit()
        form.addRow('', self.task_result_compress_checkbox)
        form.addRow('压缩关键词：', self.task_result_compress_keywords_edit)
        form.addRow('Shana 编码器：', self.task_result_shana_ffmpeg_edit)
        form.addRow('Shana 预设：', self.task_result_shana_preset_edit)

        form.addRow(self._section_label('上传目标', scroll_content))
        self.task_result_drive_folder_edit = QtWidgets.QLineEdit()
        self.task_result_drive_folder_edit.setPlaceholderText('Google Drive 文件夹链接或 ID')
        self.task_result_upload_date_edit = QtWidgets.QLineEdit()
        self.task_result_upload_date_edit.setPlaceholderText('留空自动判断，例如 0819')
        self.task_result_upload_slot_combo = QtWidgets.QComboBox()
        self.task_result_upload_slot_combo.addItem('自动判断', '')
        self.task_result_upload_slot_combo.addItem('01', '01')
        self.task_result_upload_slot_combo.addItem('02', '02')
        self.task_result_upload_slot_combo.addItem('03', '03')
        form.addRow('Drive 父目录：', self.task_result_drive_folder_edit)
        form.addRow('指定上传日期：', self.task_result_upload_date_edit)
        form.addRow('指定上传批次：', self.task_result_upload_slot_combo)

        form.addRow(self._section_label('上传历史与版本', scroll_content))
        self.task_result_upload_history_checkbox = QtWidgets.QCheckBox(
            '保存每个视频的网盘链接和上传状态'
        )
        self.task_result_upload_history_days_spinbox = QtWidgets.QSpinBox()
        self.task_result_upload_history_days_spinbox.setRange(7, 365)
        self.task_result_upload_history_days_spinbox.setSuffix(' 天')
        self.task_result_replace_old_checkbox = QtWidgets.QCheckBox(
            '修改版确认写入任务表后，将同名旧版移入网盘回收站'
        )
        self.task_result_replace_old_checkbox.setToolTip(
            '只匹配去掉 [SHANA] 前缀后完全同名的视频；新链接未通过任务表读回校验时不会处理旧文件。'
        )
        form.addRow('', self.task_result_upload_history_checkbox)
        form.addRow('活跃记录保留：', self.task_result_upload_history_days_spinbox)
        form.addRow('', self.task_result_replace_old_checkbox)

        form.addRow(self._section_label('Google 表格', scroll_content))
        self.task_result_review_sheet_checkbox = QtWidgets.QCheckBox('写入人工检查表格')
        self.task_result_review_sheet_url_edit = QtWidgets.QLineEdit()
        self.task_result_review_submitter_edit = QtWidgets.QLineEdit()
        self.task_result_review_monitor_checkbox = QtWidgets.QCheckBox(
            '监视审核结果并提醒'
        )
        self.task_result_review_monitor_minutes_spinbox = QtWidgets.QSpinBox()
        self.task_result_review_monitor_minutes_spinbox.setRange(1, 1440)
        self.task_result_review_monitor_minutes_spinbox.setSuffix(' 分钟')
        self.task_result_review_monitor_minutes_spinbox.setToolTip(
            '只在状态变化时弹出系统提醒，不会反复提醒同一个结果'
        )
        self.task_result_submission_sheet_checkbox = QtWidgets.QCheckBox('写入任务提交表格')
        self.task_result_submission_sheet_url_edit = QtWidgets.QLineEdit()
        self.task_result_submission_creator_edit = QtWidgets.QLineEdit()
        self.task_result_creator_marker_edit = QtWidgets.QLineEdit()
        form.addRow('', self.task_result_review_sheet_checkbox)
        form.addRow('审核表格链接：', self.task_result_review_sheet_url_edit)
        form.addRow('审核提交人：', self.task_result_review_submitter_edit)
        form.addRow('', self.task_result_review_monitor_checkbox)
        form.addRow('审核检查间隔：', self.task_result_review_monitor_minutes_spinbox)
        form.addRow('', self.task_result_submission_sheet_checkbox)
        form.addRow('任务表格链接：', self.task_result_submission_sheet_url_edit)
        form.addRow('任务制作人：', self.task_result_submission_creator_edit)
        form.addRow('文件名制作人标记：', self.task_result_creator_marker_edit)

        note = QtWidgets.QLabel(
            '“整理任务结果”按钮始终处理主界面当前选择的日期。Google 授权文件、Token 和历史提交日志已随功能迁移，首次授权失效时浏览器会重新登录。',
            scroll_content,
        )
        note.setWordWrap(True)
        note.setStyleSheet('color: #666;')
        form.addRow(note)

        scroll.setWidget(scroll_content)
        tab_layout.addWidget(scroll)
        self.settingTabWidget.addTab(self.task_result_tab, '整理任务结果')

        self.task_result_run_upload_checkbox.toggled.connect(self._update_task_result_enabled_state)
        self.task_result_detection_checkbox.toggled.connect(self._update_task_result_enabled_state)
        self.task_result_compress_checkbox.toggled.connect(self._update_task_result_enabled_state)
        self.task_result_review_sheet_checkbox.toggled.connect(self._update_task_result_enabled_state)
        self.task_result_review_monitor_checkbox.toggled.connect(
            self._update_task_result_enabled_state
        )
        self.task_result_submission_sheet_checkbox.toggled.connect(self._update_task_result_enabled_state)
        self.task_result_upload_history_checkbox.toggled.connect(
            self._update_task_result_enabled_state
        )

    def _build_task_schema_tab(self):
        self.task_schema_tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.task_schema_tab)

        title = self._section_label('任务表格结构', self.task_schema_tab)
        layout.addWidget(title)

        description = QtWidgets.QLabel(
            '配置本地任务登记 ODS 的表头别名、缺失字段默认值，以及“整理任务结果”'
            '回写 Google 任务表格时使用的字段。表格以后改列名或移动列，只需在这里调整。',
            self.task_schema_tab,
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self.task_schema_status_label = QtWidgets.QLabel(self.task_schema_tab)
        self.task_schema_status_label.setWordWrap(True)
        self.task_schema_status_label.setStyleSheet('color:#555;')
        layout.addWidget(self.task_schema_status_label)

        self.edit_task_schema_button = QtWidgets.QPushButton(
            '打开任务表格结构设置…',
            self.task_schema_tab,
        )
        self.edit_task_schema_button.setMinimumHeight(36)
        self.edit_task_schema_button.clicked.connect(self.edit_task_table_schema)
        layout.addWidget(self.edit_task_schema_button)

        note = QtWidgets.QLabel(
            '高级配置保存在程序根目录 config.json 的 task_table_schema 中。通常直接使用上面的图形界面即可。',
            self.task_schema_tab,
        )
        note.setWordWrap(True)
        note.setStyleSheet('color:#777;')
        layout.addWidget(note)
        layout.addStretch()

        self.settingTabWidget.addTab(self.task_schema_tab, '任务表格')
        self._refresh_task_schema_status()

    def _current_task_ods_path(self):
        parent = self.parent()
        if parent is None or not hasattr(parent, 'task_path_edit'):
            return None
        root = Path(parent.task_path_edit.text().strip())
        task_date = parent.dateEdit.date().toString('MMdd')
        config = load_task_result_config()
        if os.path.exists('config.json'):
            try:
                with open('config.json', 'r', encoding='utf-8') as handle:
                    config = load_task_result_config(json.load(handle))
            except (OSError, ValueError):
                pass
        table_file_name = task_result_config_str(
            config,
            'task_table_file_name',
            'tasks.ods',
        )
        path = root / task_date / table_file_name
        return path if path.exists() else None

    def _refresh_task_schema_status(self):
        try:
            schema = load_task_table_schema()
            local_aliases = sum(
                len(schema['fields'][name]['aliases']) for name in FIELD_ORDER
            )
            submission_aliases = sum(
                len(schema['submission_sheet']['fields'][name]['aliases'])
                for name in SUBMISSION_FIELD_ORDER
            )
            current_ods = self._current_task_ods_path()
            current_text = (
                '；当前日期的 ODS 可用于检测'
                if current_ods is not None
                else '；当前日期尚未找到 ODS，仍可先编辑结构'
            )
            self.task_schema_status_label.setText(
                '已配置本地字段 {} 个（{} 个别名），Google 回写字段 {} 个（{} 个别名）{}'.format(
                    len(FIELD_ORDER),
                    local_aliases,
                    len(SUBMISSION_FIELD_ORDER),
                    submission_aliases,
                    current_text,
                )
            )
            self.task_schema_status_label.setStyleSheet('color:#555;')
        except (OSError, TaskTableSchemaError) as error:
            self.task_schema_status_label.setText('结构配置异常：{}'.format(error))
            self.task_schema_status_label.setStyleSheet('color:#B3261E;font-weight:bold;')

    def edit_task_table_schema(self):
        try:
            schema = load_task_table_schema()
        except (OSError, TaskTableSchemaError) as error:
            QMessageBox.critical(self, '任务表格结构', str(error))
            return
        new_schema = TaskTableSchemaDialog.get_schema(
            schema,
            self._current_task_ods_path(),
            self,
        )
        if new_schema is None:
            return
        try:
            save_task_table_schema(new_schema)
        except OSError as error:
            QMessageBox.critical(self, '任务表格结构', '结构配置保存失败：{}'.format(error))
            return
        self._refresh_task_schema_status()
        parent = self.parent()
        if parent is not None and hasattr(parent, 'applyTaskTableLabels'):
            parent.applyTaskTableLabels()
        QMessageBox.information(
            self,
            '任务表格结构',
            '结构配置已保存，下一次加载任务或整理结果时自动生效。',
        )

    def _set_combo_data(self, combo, value):
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    @staticmethod
    def _split_config_lines(value):
        parts = str(value or '').replace('；', ';').replace('，', ',').splitlines()
        result = []
        for line in parts:
            for item in line.replace(',', ';').split(';'):
                item = item.strip()
                if item and item not in result:
                    result.append(item)
        return result

    def _load_task_result_config(self, config):
        self.task_result_run_export_checkbox.setChecked(task_result_config_bool(config, 'run_export', True))
        self.task_result_run_upload_checkbox.setChecked(task_result_config_bool(config, 'run_upload', True))
        self.task_result_only_changed_checkbox.setChecked(task_result_config_bool(config, 'upload_only_changed_files', True))
        self.task_result_open_dir_checkbox.setChecked(task_result_config_bool(config, 'open_result_dir', True))
        self.task_result_wsp_checkbox.setChecked(task_result_config_bool(config, 'wsp_export', False))
        try:
            filename_max_length = int(config.get(
                'task_output_filename_max_length',
                DEFAULT_OUTPUT_FILENAME_MAX_LENGTH,
            ))
        except (TypeError, ValueError):
            filename_max_length = DEFAULT_OUTPUT_FILENAME_MAX_LENGTH
        self.task_result_filename_max_length_spinbox.setValue(
            max(0, min(255, filename_max_length))
        )
        self.task_result_detection_checkbox.setChecked(task_result_config_bool(config, 'enable_video_review_detection', True))
        self._set_combo_data(self.task_result_detection_mode_combo, task_result_config_str(config, 'video_detection_mode', 'ask').lower())
        try:
            confidence = float(config.get('review_min_confidence', 0.4))
        except (TypeError, ValueError):
            confidence = 0.4
        self.task_result_review_confidence_spinbox.setValue(confidence)
        self.task_result_failure_review_checkbox.setChecked(task_result_config_bool(config, 'detection_failure_goes_to_review', True))
        self.task_result_review_folder_edit.setText(task_result_config_str(config, 'review_folder_name'))
        target_name = task_result_config_str(config, 'video_detection_target_name', '目标元素') or '目标元素'
        self.task_result_target_name_edit.setText(target_name)
        self.task_result_target_description_edit.setPlainText(task_result_config_str(config, 'video_detection_target_description'))
        self.task_result_detection_rules_edit.setPlainText('\n'.join(task_result_config_list(config, 'video_detection_rules')))
        self.task_result_positive_keywords_edit.setText('; '.join(task_result_config_list(config, 'video_detection_positive_keywords')))
        self.task_result_negative_keywords_edit.setText('; '.join(task_result_config_list(config, 'video_detection_negative_keywords')))
        self.task_result_ambiguous_keywords_edit.setText('; '.join(task_result_config_list(config, 'video_detection_ambiguous_keywords')))
        self.task_result_report_name_edit.setText(task_result_config_str(config, 'video_detection_report_name', '视频检测报告.json'))
        self.task_result_detection_checkbox.setText(f'检测视频中的{target_name}并分流')
        skip_index = self.task_result_detection_mode_combo.findData('skip')
        if skip_index >= 0:
            self.task_result_detection_mode_combo.setItemText(skip_index, f'不检测，全部按无{target_name}处理')
        self.task_result_gemini_model_edit.setText(task_result_config_str(config, 'gemini_model', 'gemini-2.5-flash-lite'))
        self.task_result_gemini_keys_edit.setText('; '.join(task_result_config_list(config, 'gemini_api_keys')))
        self.task_result_compress_checkbox.setChecked(task_result_config_bool(config, 'compress_enabled', True))
        self.task_result_compress_keywords_edit.setText('; '.join(task_result_config_list(config, 'compress_name_keywords')))
        self.task_result_shana_ffmpeg_edit.setText(task_result_config_str(config, 'shana_ffmpeg_path'))
        self.task_result_shana_preset_edit.setText(task_result_config_str(config, 'shana_preset_path'))
        self.task_result_drive_folder_edit.setText(task_result_config_str(config, 'drive_parent_folder_id'))
        self.task_result_upload_date_edit.setText(task_result_config_str(config, 'upload_date_override'))
        self._set_combo_data(self.task_result_upload_slot_combo, task_result_config_str(config, 'upload_slot_override'))
        self.task_result_upload_history_checkbox.setChecked(
            task_result_config_bool(config, 'video_upload_history_enabled', True)
        )
        try:
            history_days = int(config.get('video_upload_history_retention_days', 31))
        except (TypeError, ValueError):
            history_days = 31
        self.task_result_upload_history_days_spinbox.setValue(
            max(7, min(365, history_days))
        )
        self.task_result_replace_old_checkbox.setChecked(
            task_result_config_bool(config, 'video_upload_replace_old_enabled', True)
        )
        self.task_result_review_sheet_checkbox.setChecked(task_result_config_bool(config, 'review_sheet_enabled', True))
        self.task_result_review_sheet_url_edit.setText(task_result_config_str(config, 'review_sheet_url'))
        self.task_result_review_submitter_edit.setText(task_result_config_str(config, 'review_sheet_submitter'))
        self.task_result_review_monitor_checkbox.setChecked(
            task_result_config_bool(config, 'review_status_monitor_enabled', True)
        )
        try:
            review_poll_seconds = int(
                config.get('review_status_monitor_poll_seconds', 300)
            )
        except (TypeError, ValueError):
            review_poll_seconds = 300
        self.task_result_review_monitor_minutes_spinbox.setValue(
            max(1, round(review_poll_seconds / 60))
        )
        self.task_result_submission_sheet_checkbox.setChecked(task_result_config_bool(config, 'task_submission_sheet_enabled', True))
        self.task_result_submission_sheet_url_edit.setText(task_result_config_str(config, 'task_submission_sheet_url'))
        self.task_result_submission_creator_edit.setText(task_result_config_str(config, 'task_submission_creator'))
        self.task_result_creator_marker_edit.setText(task_result_config_str(config, 'task_submission_creator_marker'))
        self._update_task_result_enabled_state()

    def _update_task_result_enabled_state(self):
        upload_enabled = self.task_result_run_upload_checkbox.isChecked()
        detection_enabled = upload_enabled and self.task_result_detection_checkbox.isChecked()
        compression_enabled = upload_enabled and self.task_result_compress_checkbox.isChecked()
        for widget in (
            self.task_result_only_changed_checkbox,
            self.task_result_drive_folder_edit,
            self.task_result_upload_date_edit,
            self.task_result_upload_slot_combo,
            self.task_result_detection_checkbox,
            self.task_result_compress_checkbox,
            self.task_result_review_sheet_checkbox,
            self.task_result_submission_sheet_checkbox,
        ):
            widget.setEnabled(upload_enabled)
        for widget in (
            self.task_result_detection_mode_combo,
            self.task_result_review_confidence_spinbox,
            self.task_result_failure_review_checkbox,
            self.task_result_review_folder_edit,
            self.task_result_target_name_edit,
            self.task_result_target_description_edit,
            self.task_result_detection_rules_edit,
            self.task_result_positive_keywords_edit,
            self.task_result_negative_keywords_edit,
            self.task_result_ambiguous_keywords_edit,
            self.task_result_report_name_edit,
            self.task_result_gemini_model_edit,
            self.task_result_gemini_keys_edit,
        ):
            widget.setEnabled(detection_enabled)
        for widget in (
            self.task_result_compress_keywords_edit,
            self.task_result_shana_ffmpeg_edit,
            self.task_result_shana_preset_edit,
        ):
            widget.setEnabled(compression_enabled)
        review_sheet_enabled = upload_enabled and self.task_result_review_sheet_checkbox.isChecked()
        self.task_result_review_sheet_url_edit.setEnabled(review_sheet_enabled)
        self.task_result_review_submitter_edit.setEnabled(review_sheet_enabled)
        monitor_available = self.task_result_review_sheet_checkbox.isChecked()
        self.task_result_review_monitor_checkbox.setEnabled(monitor_available)
        self.task_result_review_monitor_minutes_spinbox.setEnabled(
            monitor_available
            and self.task_result_review_monitor_checkbox.isChecked()
        )
        submission_enabled = upload_enabled and self.task_result_submission_sheet_checkbox.isChecked()
        self.task_result_submission_sheet_url_edit.setEnabled(submission_enabled)
        self.task_result_submission_creator_edit.setEnabled(submission_enabled)
        self.task_result_creator_marker_edit.setEnabled(submission_enabled)
        history_enabled = (
            upload_enabled and self.task_result_upload_history_checkbox.isChecked()
        )
        self.task_result_upload_history_days_spinbox.setEnabled(history_enabled)
        self.task_result_replace_old_checkbox.setEnabled(history_enabled)

    def _get_task_result_config(self):
        return {
            'run_export': self.task_result_run_export_checkbox.isChecked(),
            'run_upload': self.task_result_run_upload_checkbox.isChecked(),
            'upload_only_changed_files': self.task_result_only_changed_checkbox.isChecked(),
            'open_result_dir': self.task_result_open_dir_checkbox.isChecked(),
            'wsp_export': self.task_result_wsp_checkbox.isChecked(),
            'task_output_filename_max_length': (
                self.task_result_filename_max_length_spinbox.value()
            ),
            'enable_video_review_detection': self.task_result_detection_checkbox.isChecked(),
            'video_detection_mode': self.task_result_detection_mode_combo.currentData(),
            'review_min_confidence': self.task_result_review_confidence_spinbox.value(),
            'detection_failure_goes_to_review': self.task_result_failure_review_checkbox.isChecked(),
            'review_folder_name': self.task_result_review_folder_edit.text().strip(),
            'video_detection_target_name': self.task_result_target_name_edit.text().strip(),
            'video_detection_target_description': self.task_result_target_description_edit.toPlainText().strip(),
            'video_detection_rules': self._split_config_lines(self.task_result_detection_rules_edit.toPlainText()),
            'video_detection_positive_keywords': self._split_config_lines(self.task_result_positive_keywords_edit.text()),
            'video_detection_negative_keywords': self._split_config_lines(self.task_result_negative_keywords_edit.text()),
            'video_detection_ambiguous_keywords': self._split_config_lines(self.task_result_ambiguous_keywords_edit.text()),
            'video_detection_report_name': self.task_result_report_name_edit.text().strip(),
            'gemini_model': self.task_result_gemini_model_edit.text().strip(),
            'gemini_api_keys': task_result_config_list({'value': self.task_result_gemini_keys_edit.text()}, 'value'),
            'compress_enabled': self.task_result_compress_checkbox.isChecked(),
            'compress_name_keywords': task_result_config_list({'value': self.task_result_compress_keywords_edit.text()}, 'value'),
            'shana_ffmpeg_path': self.task_result_shana_ffmpeg_edit.text().strip(),
            'shana_preset_path': self.task_result_shana_preset_edit.text().strip(),
            'drive_parent_folder_id': self.task_result_drive_folder_edit.text().strip(),
            'upload_date_override': self.task_result_upload_date_edit.text().strip(),
            'upload_slot_override': self.task_result_upload_slot_combo.currentData(),
            'video_upload_history_enabled': self.task_result_upload_history_checkbox.isChecked(),
            'video_upload_history_retention_days': self.task_result_upload_history_days_spinbox.value(),
            'video_upload_replace_old_enabled': self.task_result_replace_old_checkbox.isChecked(),
            'review_sheet_enabled': self.task_result_review_sheet_checkbox.isChecked(),
            'review_sheet_url': self.task_result_review_sheet_url_edit.text().strip(),
            'review_sheet_submitter': self.task_result_review_submitter_edit.text().strip(),
            'review_status_monitor_enabled': self.task_result_review_monitor_checkbox.isChecked(),
            'review_status_monitor_poll_seconds': (
                self.task_result_review_monitor_minutes_spinbox.value() * 60
            ),
            'task_submission_sheet_enabled': self.task_result_submission_sheet_checkbox.isChecked(),
            'task_submission_sheet_url': self.task_result_submission_sheet_url_edit.text().strip(),
            'task_submission_creator': self.task_result_submission_creator_edit.text().strip(),
            'task_submission_creator_marker': self.task_result_creator_marker_edit.text().strip(),
            'video_filename_creator_marker': self.task_result_creator_marker_edit.text().strip(),
        }

    @staticmethod
    def _masked_api_key(api_key):
        if len(api_key) <= 10:
            return '*' * len(api_key)
        return f"{api_key[:6]}...{api_key[-4:]}"

    def _append_api_key(self, api_key):
        """添加一个 Key；真实值保存在 UserRole 中，列表仅显示掩码。"""
        api_key = api_key.strip()
        if not api_key or api_key in self.get_api_keys():
            return False

        item = QtWidgets.QListWidgetItem()
        item.setData(QtCore.Qt.UserRole, api_key)
        self._refresh_api_key_item(item)
        self.elevenlabs_api_key_list.addItem(item)
        return True

    def _refresh_api_key_item(self, item):
        api_key = item.data(QtCore.Qt.UserRole)
        status_text = describe_api_key_status(self.api_key_statuses, api_key)
        item.setText(f"{self._masked_api_key(api_key)}    [{status_text}]")

    def add_api_key(self):
        api_key = self.elevenlabs_api_key_edit.text().strip()
        if not api_key:
            return

        if not self._append_api_key(api_key):
            QMessageBox.information(self, "提示", "这个 API Key 已在列表中。")
        self.elevenlabs_api_key_edit.clear()

    def remove_selected_api_keys(self):
        for item in self.elevenlabs_api_key_list.selectedItems():
            self.elevenlabs_api_key_list.takeItem(
                self.elevenlabs_api_key_list.row(item)
            )

    def reset_selected_api_key_statuses(self):
        """清除选中 Key 的状态，使它在下一次生成时重新测试。"""
        for item in self.elevenlabs_api_key_list.selectedItems():
            api_key = item.data(QtCore.Qt.UserRole)
            self.api_key_statuses.pop(api_key_id(api_key), None)
            self._refresh_api_key_item(item)

    def get_api_keys(self):
        return [
            self.elevenlabs_api_key_list.item(index).data(QtCore.Qt.UserRole)
            for index in range(self.elevenlabs_api_key_list.count())
        ]

    def get_config(self):
        """获取界面中的配置"""
        config = {
            'elevenlabs_api_keys': self.get_api_keys(),
            'audio_settings': copy.deepcopy(self.audio_settings),
            TASK_RESULT_HOTKEY_CONFIG_KEY: normalize_hotkey_sequence(
                self.task_result_hotkey_edit.keySequence().toString(
                    QtGui.QKeySequence.PortableText
                )
            ),
            LOAD_TASK_HOTKEY_CONFIG_KEY: normalize_hotkey_sequence(
                self.load_task_hotkey_edit.keySequence().toString(
                    QtGui.QKeySequence.PortableText
                )
            ),
            FLOW_GUARD_CONFIG_KEY: self._get_flow_guard_settings(),
            SMART_VIDEO_EDITOR_CONFIG_KEY: self._get_smart_video_editor_settings(),
        }
        if not self._chrome_managed_by_plugin:
            config[CHROME_NEXT_HOTKEY_CONFIG_KEY] = normalize_hotkey_sequence(
                self.chrome_hotkey_edit.keySequence().toString(
                    QtGui.QKeySequence.PortableText
                )
            )
        config.update(self._get_task_result_config())
        if self.plugin_host is not None:
            self.plugin_host.update_settings_config(config)
        return config

    def save_config(self):
        """保存配置到文件"""
        try:
            # 读取现有配置
            if os.path.exists("config.json"):
                with open("config.json", "r", encoding="utf-8") as f:
                    config = json.load(f)
            else:
                config = {}

            # 更新API key
            new_config = self.get_config()
            config.update(new_config)
            config.pop('elevenlabs_api_key', None)
            config[API_KEY_STATUSES_CONFIG_KEY] = prune_api_key_statuses(
                self.api_key_statuses,
                new_config['elevenlabs_api_keys'],
            )

            # 保存配置
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)

            return True
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存配置失败: {e}")
            return False

    def accept(self):
        """确定按钮点击事件"""
        output_folder_name = self.smart_output_folder_edit.text().strip()
        if (
            not output_folder_name
            or output_folder_name in {".", ".."}
            or any(character in output_folder_name for character in '<>:"/\\|?*')
        ):
            self.settingTabWidget.setCurrentWidget(self.smart_video_editor_tab)
            self.smart_output_folder_edit.setFocus()
            QMessageBox.warning(
                self,
                "智能剪辑目录无效",
                "输出目录必须是一个普通文件夹名称，不能包含路径或特殊字符。",
            )
            return
        if (
            self.smart_severe_similarity_spinbox.value()
            >= self.smart_pass_similarity_spinbox.value()
        ):
            self.settingTabWidget.setCurrentWidget(self.smart_video_editor_tab)
            self.smart_severe_similarity_spinbox.setFocus()
            QMessageBox.warning(
                self,
                "相似度范围无效",
                "严重异常阈值必须低于自动通过阈值。",
            )
            return
        if (
            self.smart_compress_pauses_checkbox.isChecked()
            and self.smart_retained_pause_spinbox.value()
            >= self.smart_pause_threshold_spinbox.value()
        ):
            self.settingTabWidget.setCurrentWidget(self.smart_video_editor_tab)
            self.smart_retained_pause_spinbox.setFocus()
            QMessageBox.warning(
                self,
                "气口参数无效",
                "压缩后保留的停顿必须短于触发压缩的时长。",
            )
            return
        if (
            self.task_result_run_upload_checkbox.isChecked()
            and not self.task_result_drive_folder_edit.text().strip()
        ):
            self.settingTabWidget.setCurrentWidget(self.task_result_tab)
            self.task_result_drive_folder_edit.setFocus()
            QMessageBox.warning(self, '缺少上传目录', '启用 Google Drive 上传时，必须填写父目录链接或 ID。')
            return
        if (
            self.task_result_run_upload_checkbox.isChecked()
            and self.task_result_detection_checkbox.isChecked()
            and not self.task_result_review_folder_edit.text().strip()
        ):
            self.settingTabWidget.setCurrentWidget(self.task_result_tab)
            self.task_result_review_folder_edit.setFocus()
            QMessageBox.warning(self, '缺少目录名称', '启用视频检测时，必须填写人工检查目录名称。')
            return
        if (
            self.task_result_run_upload_checkbox.isChecked()
            and self.task_result_submission_sheet_checkbox.isChecked()
            and not self.task_result_creator_marker_edit.text().strip()
        ):
            self.settingTabWidget.setCurrentWidget(self.task_result_tab)
            self.task_result_creator_marker_edit.setFocus()
            QMessageBox.warning(self, '缺少文件名标记', '启用任务表回写时，必须填写文件名制作人标记。')
            return
        has_override_date = bool(self.task_result_upload_date_edit.text().strip())
        has_override_slot = bool(self.task_result_upload_slot_combo.currentData())
        if has_override_date != has_override_slot:
            self.settingTabWidget.setCurrentWidget(self.task_result_tab)
            QMessageBox.warning(self, '上传批次不完整', '指定上传批次时，日期和批次必须同时设置；否则两项都留空使用自动判断。')
            return
        hotkey_fields = (
            ('整理任务结果', self.task_result_hotkey_edit, self.hotkey_tab),
            ('加载当前任务', self.load_task_hotkey_edit, self.hotkey_tab),
        )
        if not self._chrome_managed_by_plugin:
            hotkey_fields = (
                ('启动下一个浏览器', self.chrome_hotkey_edit, self.hotkey_tab),
            ) + hotkey_fields
        if self.plugin_host is not None:
            hotkey_fields += tuple(self.plugin_host.settings_hotkey_fields())
        normalized_hotkeys = []
        for label, editor, page_widget in hotkey_fields:
            try:
                normalized = normalize_hotkey_sequence(
                    editor.keySequence().toString(QtGui.QKeySequence.PortableText)
                )
            except ValueError as error:
                self.settingTabWidget.setCurrentWidget(page_widget)
                editor.setFocus()
                QMessageBox.warning(self, '快捷键无效', f'{label}：{error}')
                return
            normalized_hotkeys.append((label, normalized, editor, page_widget))
        duplicate_keys = {}
        for label, normalized, editor, page_widget in normalized_hotkeys:
            duplicate_keys.setdefault(normalized.casefold(), []).append(
                (label, editor, page_widget)
            )
        conflict = next((items for items in duplicate_keys.values() if len(items) > 1), None)
        if conflict:
            self.settingTabWidget.setCurrentWidget(conflict[0][2])
            conflict[0][1].setFocus()
            QMessageBox.warning(
                self,
                '快捷键冲突',
                '以下功能不能使用同一个快捷键：' + '、'.join(item[0] for item in conflict),
            )
            return

        if self.plugin_host is not None:
            plugin_error = self.plugin_host.validate_settings_pages()
            if plugin_error is not None:
                _plugin_id, _page_id, widget, message = plugin_error
                if widget is not None:
                    self.settingTabWidget.setCurrentWidget(widget)
                QMessageBox.warning(self, '插件设置无效', message)
                return

        # 用户输入后直接点“确定”时，也自动加入列表。
        pending_api_key = self.elevenlabs_api_key_edit.text().strip()
        if pending_api_key:
            self._append_api_key(pending_api_key)
            self.elevenlabs_api_key_edit.clear()
        if self.save_config():
            super().accept()

    @staticmethod
    def get_settings(parent=None, plugin_host=None):
        """静态方法：显示设置对话框并返回配置"""
        dialog = MainSettingDialog(parent, plugin_host=plugin_host)
        result = dialog.exec_()

        if result == QtWidgets.QDialog.Accepted:
            return dialog.get_config()
        return None
