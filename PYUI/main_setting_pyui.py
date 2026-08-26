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
    normalize_hotkey_sequence,
)
from model.ApiKeyHelper import (
    API_KEY_STATUSES_CONFIG_KEY,
    api_key_id,
    describe_api_key_status,
    normalize_api_keys,
    prune_api_key_statuses,
)
from model.TaskResultOrganizer import (
    config_bool as task_result_config_bool,
    config_list as task_result_config_list,
    config_str as task_result_config_str,
    load_effective_config as load_task_result_config,
    migrate_legacy_task_result_config,
)
from model.TaskTableSchema import (
    FIELD_ORDER,
    SUBMISSION_FIELD_ORDER,
    TaskTableSchemaError,
    load_task_table_schema,
    save_task_table_schema,
)


class MainSettingDialog(QtWidgets.QDialog, Ui_MainSettingDialog):
    def __init__(self, parent=None):
        super(MainSettingDialog, self).__init__(parent)
        self.setupUi(self)
        self._build_task_result_tab()
        self._build_task_schema_tab()
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
        config = load_task_result_config()
        try:
            migrate_legacy_task_result_config("config.json")
            if os.path.exists("config.json"):
                with open("config.json", "r", encoding="utf-8") as f:
                    config = load_task_result_config(json.load(f))
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
            self._load_task_result_config(config)
        except Exception as e:
            print(f"加载配置失败: {e}")
            self._load_task_result_config(config)
        try:
            hotkey = normalize_hotkey_sequence(hotkey)
        except ValueError:
            hotkey = DEFAULT_CHROME_NEXT_HOTKEY
        self.chrome_hotkey_edit.setKeySequence(QtGui.QKeySequence(hotkey))

    @staticmethod
    def _section_label(text, parent):
        label = QtWidgets.QLabel(text, parent)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

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

        form.addRow(self._section_label('Google 表格', scroll_content))
        self.task_result_review_sheet_checkbox = QtWidgets.QCheckBox('写入人工检查表格')
        self.task_result_review_sheet_url_edit = QtWidgets.QLineEdit()
        self.task_result_review_submitter_edit = QtWidgets.QLineEdit()
        self.task_result_submission_sheet_checkbox = QtWidgets.QCheckBox('写入任务提交表格')
        self.task_result_submission_sheet_url_edit = QtWidgets.QLineEdit()
        self.task_result_submission_creator_edit = QtWidgets.QLineEdit()
        self.task_result_creator_marker_edit = QtWidgets.QLineEdit()
        form.addRow('', self.task_result_review_sheet_checkbox)
        form.addRow('审核表格链接：', self.task_result_review_sheet_url_edit)
        form.addRow('审核提交人：', self.task_result_review_submitter_edit)
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
        self.task_result_submission_sheet_checkbox.toggled.connect(self._update_task_result_enabled_state)

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
        self.task_result_review_sheet_checkbox.setChecked(task_result_config_bool(config, 'review_sheet_enabled', True))
        self.task_result_review_sheet_url_edit.setText(task_result_config_str(config, 'review_sheet_url'))
        self.task_result_review_submitter_edit.setText(task_result_config_str(config, 'review_sheet_submitter'))
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
        submission_enabled = upload_enabled and self.task_result_submission_sheet_checkbox.isChecked()
        self.task_result_submission_sheet_url_edit.setEnabled(submission_enabled)
        self.task_result_submission_creator_edit.setEnabled(submission_enabled)
        self.task_result_creator_marker_edit.setEnabled(submission_enabled)

    def _get_task_result_config(self):
        return {
            'run_export': self.task_result_run_export_checkbox.isChecked(),
            'run_upload': self.task_result_run_upload_checkbox.isChecked(),
            'upload_only_changed_files': self.task_result_only_changed_checkbox.isChecked(),
            'open_result_dir': self.task_result_open_dir_checkbox.isChecked(),
            'wsp_export': self.task_result_wsp_checkbox.isChecked(),
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
            'review_sheet_enabled': self.task_result_review_sheet_checkbox.isChecked(),
            'review_sheet_url': self.task_result_review_sheet_url_edit.text().strip(),
            'review_sheet_submitter': self.task_result_review_submitter_edit.text().strip(),
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
            CHROME_NEXT_HOTKEY_CONFIG_KEY: normalize_hotkey_sequence(
                self.chrome_hotkey_edit.keySequence().toString(
                    QtGui.QKeySequence.PortableText
                )
            ),
        }
        config.update(self._get_task_result_config())
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
        try:
            normalize_hotkey_sequence(
                self.chrome_hotkey_edit.keySequence().toString(
                    QtGui.QKeySequence.PortableText
                )
            )
        except ValueError as error:
            self.settingTabWidget.setCurrentWidget(self.hotkey_tab)
            self.chrome_hotkey_edit.setFocus()
            QMessageBox.warning(self, '快捷键无效', str(error))
            return

        # 用户输入后直接点“确定”时，也自动加入列表。
        pending_api_key = self.elevenlabs_api_key_edit.text().strip()
        if pending_api_key:
            self._append_api_key(pending_api_key)
            self.elevenlabs_api_key_edit.clear()
        if self.save_config():
            super().accept()
    
    @staticmethod
    def get_settings(parent=None):
        """静态方法：显示设置对话框并返回配置"""
        dialog = MainSettingDialog(parent)
        result = dialog.exec_()
        
        if result == QtWidgets.QDialog.Accepted:
            return dialog.get_config()
        return None
