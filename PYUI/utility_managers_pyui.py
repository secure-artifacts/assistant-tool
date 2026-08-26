import math
from datetime import datetime

from PyQt5 import QtCore, QtGui, QtWidgets

from model.GoogleSheetMonitor import (
    normalize_monitor_settings,
    read_monitor_state,
)
from model.GoogleSheetsHelper import extract_spreadsheet_id
from model.InventoryManager import (
    STATUS_CRITICAL,
    STATUS_INITIAL,
    STATUS_LABELS,
    STATUS_MODERATE,
)


STATUS_COLORS = {
    STATUS_INITIAL: QtGui.QColor("#FFF3A8"),
    STATUS_MODERATE: QtGui.QColor("#FFD39B"),
    STATUS_CRITICAL: QtGui.QColor("#FF9FB1"),
}


def _format_quantity(value):
    value = float(value)
    if abs(value - round(value)) < 0.0001:
        return str(int(round(value)))
    return "{:.2f}".format(value).rstrip("0").rstrip(".")


class GoogleSheetMonitorDialog(QtWidgets.QDialog):
    def __init__(self, settings, status_text="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Google 表格监视器")
        self.resize(680, 470)
        self.reset_baseline_requested = False
        settings = normalize_monitor_settings(settings)

        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "监视指定表格中新增的 Google Drive/Docs 链接。第一次启用只建立基线，"
            "不会下载表格里原有的旧链接。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QtWidgets.QFormLayout()
        self.enabled_checkbox = QtWidgets.QCheckBox("启用后台监视")
        self.enabled_checkbox.setChecked(settings["enabled"])
        form.addRow("状态：", self.enabled_checkbox)

        self.url_edit = QtWidgets.QLineEdit(settings["sheet_url"])
        self.url_edit.setPlaceholderText("粘贴独立的 Google 表格网址（最好包含 gid）")
        form.addRow("表格网址：", self.url_edit)

        self.range_edit = QtWidgets.QLineEdit(settings["sheet_range"])
        self.range_edit.setPlaceholderText("留空监视整张工作表；也可填 A:A 或 A2:F")
        form.addRow("监视范围：", self.range_edit)

        self.interval_spin = QtWidgets.QSpinBox()
        self.interval_spin.setRange(15, 3600)
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.setValue(settings["poll_seconds"])
        form.addRow("检查间隔：", self.interval_spin)

        self.folder_edit = QtWidgets.QLineEdit(settings["download_folder"])
        self.folder_edit.setPlaceholderText("当前项目目录内的子目录")
        form.addRow("下载子目录：", self.folder_edit)
        layout.addLayout(form)

        state = read_monitor_state()
        self.status_label = QtWidgets.QLabel(
            "运行状态：{}\n待下载：{} 个；下载记录：{} 个".format(
                status_text or "未启动",
                len(state.get("pending", [])),
                len(state.get("history", [])),
            )
        )
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        note = QtWidgets.QLabel(
            "新增链接会通过系统通知、提示音和主界面彩色状态按钮提醒。"
            "未加载项目时会持久排队，加载项目后自动下载。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")
        layout.addWidget(note)

        buttons_row = QtWidgets.QHBoxLayout()
        self.reset_button = QtWidgets.QPushButton("重新建立监视基线")
        self.reset_button.setToolTip("保留待下载队列，但把当前表格中的链接重新视为旧链接")
        self.reset_button.clicked.connect(self._request_reset)
        buttons_row.addWidget(self.reset_button)
        buttons_row.addStretch()
        layout.addLayout(buttons_row)

        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        button_box.button(QtWidgets.QDialogButtonBox.Save).setText("保存并立即检查")
        button_box.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        button_box.accepted.connect(self._accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _request_reset(self):
        answer = QtWidgets.QMessageBox.question(
            self,
            "重新建立基线",
            "下次检查会把表格当前已有链接当作旧链接，不会下载它们。\n"
            "已进入待下载队列的项目会保留。是否继续？",
        )
        if answer == QtWidgets.QMessageBox.Yes:
            self.reset_baseline_requested = True
            self.status_label.setText("已标记：保存后重新建立基线。")

    def _accept(self):
        url = self.url_edit.text().strip()
        if self.enabled_checkbox.isChecked() and not extract_spreadsheet_id(url):
            QtWidgets.QMessageBox.warning(self, "设置不完整", "启用监视前请填写有效的 Google 表格网址。")
            return
        folder = self.folder_edit.text().strip()
        folder_path = QtCore.QDir.cleanPath(folder)
        if not folder or QtCore.QDir.isAbsolutePath(folder) or folder_path.startswith(".."):
            QtWidgets.QMessageBox.warning(self, "目录不正确", "下载子目录必须是当前项目内部的相对目录。")
            return
        self.accept()

    def settings(self):
        return normalize_monitor_settings(
            {
                "enabled": self.enabled_checkbox.isChecked(),
                "sheet_url": self.url_edit.text(),
                "sheet_range": self.range_edit.text(),
                "poll_seconds": self.interval_spin.value(),
                "download_folder": self.folder_edit.text(),
            }
        )

    @classmethod
    def get_settings(cls, settings, status_text="", parent=None):
        dialog = cls(settings, status_text, parent)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.settings(), dialog.reset_baseline_requested


class InventoryItemDialog(QtWidgets.QDialog):
    def __init__(self, title, item=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(410)
        item = item or {}
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()

        self.name_edit = QtWidgets.QLineEdit(str(item.get("name", "")))
        form.addRow("库存名称：", self.name_edit)

        self.quantity_spin = QtWidgets.QDoubleSpinBox()
        self.quantity_spin.setRange(0, 1_000_000_000)
        self.quantity_spin.setDecimals(3)
        self.quantity_spin.setValue(float(item.get("current_quantity", item.get("quantity", 0))))
        form.addRow("当前数量：", self.quantity_spin)

        self.usage_spin = QtWidgets.QDoubleSpinBox()
        self.usage_spin.setRange(0, 1_000_000_000)
        self.usage_spin.setDecimals(3)
        self.usage_spin.setValue(float(item.get("daily_usage", 0)))
        self.usage_spin.setToolTip("程序会按 每日消耗 ÷ 24小时 连续估算剩余库存")
        form.addRow("每日消耗：", self.usage_spin)
        layout.addLayout(form)

        note = QtWidgets.QLabel("每日消耗填 0 表示只记录数量、不自动扣减。")
        note.setStyleSheet("color: #666;")
        layout.addWidget(note)

        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        box.button(QtWidgets.QDialogButtonBox.Ok).setText("保存")
        box.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _accept(self):
        if not self.name_edit.text().strip():
            QtWidgets.QMessageBox.warning(self, "缺少名称", "请输入库存名称。")
            return
        self.accept()

    def values(self):
        return (
            self.name_edit.text().strip(),
            self.quantity_spin.value(),
            self.usage_spin.value(),
        )


class InventoryManagerDialog(QtWidgets.QDialog):
    changed = QtCore.pyqtSignal()

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("库存管理器")
        self.resize(850, 520)

        layout = QtWidgets.QVBoxLayout(self)
        toolbar = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton("添加库存")
        self.edit_button = QtWidgets.QPushButton("修改")
        self.stock_button = QtWidgets.QPushButton("补充数量")
        self.delete_button = QtWidgets.QPushButton("删除")
        for button in (self.add_button, self.edit_button, self.stock_button, self.delete_button):
            toolbar.addWidget(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["库存名称", "估算剩余", "每日消耗", "预计可用", "报警状态", "估算时间"]
        )
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        for column in range(1, 6):
            self.table.horizontalHeader().setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(self.edit_item)
        layout.addWidget(self.table)

        legend = QtWidgets.QLabel(
            "颜色：黄色＝不足 2 天（初步）　橙色＝不足 1 天（中度）　粉红＝已经耗尽（高危）\n"
            "剩余数量按实际经过时间持续估算，不要求程序一直开着。"
        )
        legend.setWordWrap(True)
        layout.addWidget(legend)

        close_button = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        close_button.button(QtWidgets.QDialogButtonBox.Close).setText("关闭")
        close_button.rejected.connect(self.reject)
        layout.addWidget(close_button)

        self.add_button.clicked.connect(self.add_item)
        self.edit_button.clicked.connect(self.edit_item)
        self.stock_button.clicked.connect(self.add_stock)
        self.delete_button.clicked.connect(self.delete_item)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(60_000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def _selected(self):
        row = self.table.currentRow()
        if row < 0:
            QtWidgets.QMessageBox.information(self, "请选择库存", "请先选择一行库存。")
            return None
        item = self.table.item(row, 0)
        return item.data(QtCore.Qt.UserRole) if item else None

    def _handle_error(self, error):
        QtWidgets.QMessageBox.critical(self, "库存操作失败", str(error))

    def refresh(self):
        try:
            summary = self.store.summary()
        except (OSError, ValueError) as error:
            self._handle_error(error)
            return
        selected_id = self._selected_id_without_message()
        rows = summary["items"]
        self.table.setRowCount(len(rows))
        now_text = datetime.now().strftime("%m-%d %H:%M")
        selected_row = -1
        for row, item in enumerate(rows):
            days_left = item["days_left"]
            values = [
                item["name"],
                _format_quantity(item["current_quantity"]),
                _format_quantity(item["daily_usage"]),
                "不自动消耗" if math.isinf(days_left) else "{:.2f} 天".format(days_left),
                STATUS_LABELS[item["status"]],
                now_text,
            ]
            color = STATUS_COLORS.get(item["status"])
            for column, value in enumerate(values):
                cell = QtWidgets.QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(QtCore.Qt.UserRole, item)
                if color is not None:
                    cell.setBackground(color)
                self.table.setItem(row, column, cell)
            if item["id"] == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.table.selectRow(selected_row)

    def _selected_id_without_message(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        data = item.data(QtCore.Qt.UserRole) if item else None
        return data.get("id") if isinstance(data, dict) else None

    def add_item(self):
        dialog = InventoryItemDialog("添加库存", parent=self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        try:
            self.store.add_item(*dialog.values())
            self.refresh()
            self.changed.emit()
        except (OSError, ValueError) as error:
            self._handle_error(error)

    def edit_item(self, _index=None):
        item = self._selected()
        if not item:
            return
        dialog = InventoryItemDialog("修改库存", item, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        try:
            self.store.update_item(item["id"], *dialog.values())
            self.refresh()
            self.changed.emit()
        except (KeyError, OSError, ValueError) as error:
            self._handle_error(error)

    def add_stock(self):
        item = self._selected()
        if not item:
            return
        amount, ok = QtWidgets.QInputDialog.getDouble(
            self,
            "补充库存",
            "给“{}”增加数量：".format(item["name"]),
            1,
            0.001,
            1_000_000_000,
            3,
        )
        if not ok:
            return
        try:
            self.store.add_stock(item["id"], amount)
            self.refresh()
            self.changed.emit()
        except (KeyError, OSError, ValueError) as error:
            self._handle_error(error)

    def delete_item(self):
        item = self._selected()
        if not item:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "删除库存",
            "确定删除“{}”吗？".format(item["name"]),
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        try:
            self.store.delete_item(item["id"])
            self.refresh()
            self.changed.emit()
        except (KeyError, OSError, ValueError) as error:
            self._handle_error(error)
