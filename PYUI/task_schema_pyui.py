from pathlib import Path

from PyQt5 import QtCore, QtWidgets

from model.OdsHelper import ReadTaskOds2, format_task_table_report
from model.TaskTableSchema import (
    DEFAULT_TASK_TABLE_SCHEMA,
    FIELD_LABELS,
    FIELD_ORDER,
    SUBMISSION_FIELD_LABELS,
    SUBMISSION_FIELD_ORDER,
    TaskTableSchemaError,
    field_label,
    normalize_task_table_schema,
    submission_field_label,
)


class TaskTableSchemaDialog(QtWidgets.QDialog):
    def __init__(self, schema, ods_path=None, parent=None):
        super().__init__(parent)
        self.ods_path = Path(ods_path) if ods_path else None
        self.setWindowTitle("任务表格结构")
        self.resize(930, 850)

        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "程序按别名自动识别表头。多个别名按从左到右的顺序尝试；同一行前面的列为空时，"
            "会继续使用后面的列。以后表格改名时，只需在这里增加新表头。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        source_form = QtWidgets.QFormLayout()
        self.sheet_name_edit = QtWidgets.QLineEdit()
        self.sheet_name_edit.setPlaceholderText("留空使用指定序号的工作表")
        source_form.addRow("工作表名称：", self.sheet_name_edit)

        self.sheet_index_spin = QtWidgets.QSpinBox()
        self.sheet_index_spin.setRange(1, 100)
        self.sheet_index_spin.setToolTip("只在工作表名称留空时使用")
        source_form.addRow("工作表序号：", self.sheet_index_spin)

        self.header_row_spin = QtWidgets.QSpinBox()
        self.header_row_spin.setRange(0, 10000)
        self.header_row_spin.setSpecialValueText("自动识别")
        source_form.addRow("表头所在行：", self.header_row_spin)
        layout.addLayout(source_form)

        self.mapping_table = QtWidgets.QTableWidget(len(FIELD_ORDER), 3)
        self.mapping_table.setHorizontalHeaderLabels(
            ["程序需要的字段", "可接受的表头（逗号分隔，前面的优先）", "缺失时默认值"]
        )
        self.mapping_table.verticalHeader().setVisible(False)
        self.mapping_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.mapping_table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents
        )
        self.mapping_table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.Stretch
        )
        self.mapping_table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeToContents
        )
        self.alias_edits = {}
        self.default_edits = {}
        self.field_label_items = {}
        for row, field_name in enumerate(FIELD_ORDER):
            label_item = QtWidgets.QTableWidgetItem(FIELD_LABELS[field_name])
            label_item.setFlags(label_item.flags() & ~QtCore.Qt.ItemIsEditable)
            label_item.setData(QtCore.Qt.UserRole, field_name)
            self.mapping_table.setItem(row, 0, label_item)
            alias_edit = QtWidgets.QLineEdit()
            default_edit = QtWidgets.QLineEdit()
            self.mapping_table.setCellWidget(row, 1, alias_edit)
            self.mapping_table.setCellWidget(row, 2, default_edit)
            self.field_label_items[field_name] = label_item
            self.alias_edits[field_name] = alias_edit
            self.default_edits[field_name] = default_edit
        self.mapping_table.setMinimumHeight(330)
        layout.addWidget(self.mapping_table)

        submission_group = QtWidgets.QGroupBox("整理任务结果：回写 Google 任务表格")
        submission_layout = QtWidgets.QVBoxLayout(submission_group)
        submission_header_row = QtWidgets.QHBoxLayout()
        submission_header_row.addWidget(QtWidgets.QLabel("表头所在行："))
        self.submission_header_row_spin = QtWidgets.QSpinBox()
        self.submission_header_row_spin.setRange(0, 10000)
        self.submission_header_row_spin.setSpecialValueText("自动识别")
        submission_header_row.addWidget(self.submission_header_row_spin)
        submission_header_row.addWidget(
            QtWidgets.QLabel("列位置会根据表头自动寻找，不再依赖固定的 B、D、I、M 等列。")
        )
        submission_header_row.addStretch()
        submission_layout.addLayout(submission_header_row)

        self.submission_table = QtWidgets.QTableWidget(len(SUBMISSION_FIELD_ORDER), 2)
        self.submission_table.setHorizontalHeaderLabels(
            ["写回字段", "可接受的 Google 表头（逗号分隔）"]
        )
        self.submission_table.verticalHeader().setVisible(False)
        self.submission_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.submission_table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents
        )
        self.submission_table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.Stretch
        )
        self.submission_alias_edits = {}
        self.submission_label_items = {}
        for row, field_name in enumerate(SUBMISSION_FIELD_ORDER):
            label_item = QtWidgets.QTableWidgetItem(SUBMISSION_FIELD_LABELS[field_name])
            label_item.setFlags(label_item.flags() & ~QtCore.Qt.ItemIsEditable)
            self.submission_table.setItem(row, 0, label_item)
            alias_edit = QtWidgets.QLineEdit()
            self.submission_table.setCellWidget(row, 1, alias_edit)
            self.submission_label_items[field_name] = label_item
            self.submission_alias_edits[field_name] = alias_edit
        self.submission_table.setMinimumHeight(225)
        submission_layout.addWidget(self.submission_table)
        layout.addWidget(submission_group)

        template_note = QtWidgets.QLabel(
            "默认值支持模板：{row}=表格实际行号，{data_row}=表头后的数据行号，"
            "还可引用 {task_id}、{task_type} 等程序字段。默认配置文件保存在程序根目录的 "
            "config.json，也可以直接用文本编辑器修改 task_table_schema。"
        )
        template_note.setWordWrap(True)
        template_note.setStyleSheet("color:#555;")
        layout.addWidget(template_note)

        action_row = QtWidgets.QHBoxLayout()
        self.preview_button = QtWidgets.QPushButton("检测当前任务表格")
        self.reset_button = QtWidgets.QPushButton("清空自定义映射")
        action_row.addWidget(self.preview_button)
        action_row.addWidget(self.reset_button)
        action_row.addStretch()
        layout.addLayout(action_row)

        self.preview_text = QtWidgets.QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setMaximumBlockCount(100)
        self.preview_text.setPlaceholderText("点击“检测当前任务表格”查看实际匹配结果。")
        self.preview_text.setMaximumHeight(135)
        layout.addWidget(self.preview_text)

        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        box.button(QtWidgets.QDialogButtonBox.Save).setText("保存结构")
        box.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        self.preview_button.clicked.connect(self.preview_current_table)
        self.reset_button.clicked.connect(self.reset_defaults)
        self.load_schema(schema)

    def load_schema(self, schema):
        schema = normalize_task_table_schema(schema)
        self._header_scan_rows = schema["header_scan_rows"]
        self._submission_header_scan_rows = schema["submission_sheet"]["header_scan_rows"]
        self.sheet_name_edit.setText(schema["sheet_name"])
        self.sheet_index_spin.setValue(schema["sheet_index"] + 1)
        self.header_row_spin.setValue(schema["header_row"])
        for field_name in FIELD_ORDER:
            spec = schema["fields"][field_name]
            self.field_label_items[field_name].setText(
                field_label(schema, field_name)
            )
            self.alias_edits[field_name].setText("，".join(spec["aliases"]))
            self.default_edits[field_name].setText(spec["default"])
        submission = schema["submission_sheet"]
        self.submission_header_row_spin.setValue(submission["header_row"])
        for field_name in SUBMISSION_FIELD_ORDER:
            aliases = submission["fields"][field_name]["aliases"]
            self.submission_label_items[field_name].setText(
                submission_field_label(schema, field_name)
            )
            self.submission_alias_edits[field_name].setText("，".join(aliases))

    def reset_defaults(self):
        answer = QtWidgets.QMessageBox.question(
            self,
            "清空自定义映射",
            "清空后需要重新填写表头别名，是否继续？",
        )
        if answer == QtWidgets.QMessageBox.Yes:
            self.load_schema(DEFAULT_TASK_TABLE_SCHEMA)
            self.preview_text.setPlainText("已清空映射，重新填写并保存后生效。")

    def schema(self):
        fields = {}
        for field_name in FIELD_ORDER:
            fields[field_name] = {
                "aliases": self.alias_edits[field_name].text(),
                "default": self.default_edits[field_name].text(),
            }
        submission_fields = {
            field_name: {"aliases": self.submission_alias_edits[field_name].text()}
            for field_name in SUBMISSION_FIELD_ORDER
        }
        return normalize_task_table_schema(
            {
                "sheet_name": self.sheet_name_edit.text(),
                "sheet_index": self.sheet_index_spin.value() - 1,
                "header_row": self.header_row_spin.value(),
                "header_scan_rows": self._header_scan_rows,
                "labels": {
                    field_name: self.field_label_items[field_name].text()
                    for field_name in FIELD_ORDER
                },
                "fields": fields,
                "submission_sheet": {
                    "header_row": self.submission_header_row_spin.value(),
                    "header_scan_rows": self._submission_header_scan_rows,
                    "labels": {
                        field_name: self.submission_label_items[field_name].text()
                        for field_name in SUBMISSION_FIELD_ORDER
                    },
                    "fields": submission_fields,
                },
            }
        )

    def preview_current_table(self):
        if self.ods_path is None or not self.ods_path.exists():
            self.preview_text.setPlainText(
                "当前日期目录下没有找到已配置的任务表格。可以先保存结构，下载表格后再检测。"
            )
            return
        try:
            tasks, report = ReadTaskOds2(
                self.ods_path,
                schema=self.schema(),
                return_report=True,
            )
            lines = [format_task_table_report(report)]
            unmatched = [
                field_label(self.schema(), name)
                for name in report["unmatched_fields"]
            ]
            if unmatched:
                lines.append("没有对应表头，将使用默认值：" + "、".join(unmatched))
            if not tasks:
                lines.append("当前表格没有读到任务数据行。")
            self.preview_text.setPlainText("\n".join(lines))
        except Exception as error:
            self.preview_text.setPlainText("检测失败：{}".format(error))

    def _accept(self):
        try:
            schema = self.schema()
            if not any(schema["fields"][name]["aliases"] for name in FIELD_ORDER):
                raise TaskTableSchemaError("至少需要保留一个字段别名")
        except (TaskTableSchemaError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, "结构不正确", str(error))
            return
        self.accept()

    @classmethod
    def get_schema(cls, schema, ods_path=None, parent=None):
        dialog = cls(schema, ods_path, parent)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.schema()
