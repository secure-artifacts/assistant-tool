import math
import os
from html import escape, unescape
from datetime import datetime
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from model.ClipboardHelper import set_internal_clipboard_text
from model.GoogleSheetMonitor import (
    normalize_monitor_settings,
    read_monitor_state,
)
from model.GoogleSheetsHelper import extract_spreadsheet_id
from model.TaskReferenceDownloader import extract_google_drive_urls
from model.AboutInfo import (
    ABOUT_SUMMARY,
    APP_DISPLAY_NAME,
    MAINTENANCE_LESSONS,
    maintenance_lessons_text,
)
from model.InventoryManager import (
    STATUS_CRITICAL,
    STATUS_INITIAL,
    STATUS_LABELS,
    STATUS_MODERATE,
    material_directory_stats,
)


STATUS_COLORS = {
    STATUS_INITIAL: QtGui.QColor("#FFF3A8"),
    STATUS_MODERATE: QtGui.QColor("#FFD39B"),
    STATUS_CRITICAL: QtGui.QColor("#FF9FB1"),
}


def google_drive_urls_from_mime_data(mime_data):
    """Extract visible or embedded Google links from clipboard/drop data."""
    if mime_data is None:
        return []

    values = []
    if mime_data.hasUrls():
        values.extend(
            url.toString()
            for url in mime_data.urls()
            if not url.isLocalFile()
        )
    if mime_data.hasHtml():
        values.append(unescape(mime_data.html()))
    if mime_data.hasText():
        values.append(mime_data.text())

    links = []
    seen = set()
    for value in values:
        for link in extract_google_drive_urls(value):
            if link not in seen:
                seen.add(link)
                links.append(link)
    return links


class AboutDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"关于 {APP_DISPLAY_NAME}")
        self.resize(820, 660)
        self.setMinimumSize(620, 460)

        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel(APP_DISPLAY_NAME, self)
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 5)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        summary = QtWidgets.QLabel(ABOUT_SUMMARY, self)
        summary.setWordWrap(True)
        layout.addWidget(summary)

        note = QtWidgets.QLabel(
            "下面记录已经确认过的低级错误及强制维护规则，保留、不粉饰，"
            "供以后维护程序的人或 AI 检查。",
            self,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #8A3B12; font-weight: bold;")
        layout.addWidget(note)

        self.lesson_browser = QtWidgets.QTextBrowser(self)
        self.lesson_browser.setOpenExternalLinks(False)
        parts = []
        for index, lesson in enumerate(MAINTENANCE_LESSONS, 1):
            parts.append(
                "<div style='margin-bottom:14px;'>"
                f"<h3 style='margin:0 0 5px 0;'>{index}. {escape(lesson['title'])}</h3>"
                f"<p style='margin:2px 0;'><b>犯过的错误：</b>{escape(lesson['mistake'])}</p>"
                f"<p style='margin:2px 0; color:#1F5F3B;'><b>以后必须遵守：</b>"
                f"{escape(lesson['rule'])}</p>"
                "</div>"
            )
        self.lesson_browser.setHtml("".join(parts))
        layout.addWidget(self.lesson_browser, 1)

        footer = QtWidgets.QHBoxLayout()
        copy_button = QtWidgets.QPushButton("复制维护复盘", self)
        close_button = QtWidgets.QPushButton("关闭", self)
        copy_button.clicked.connect(self.copy_lessons)
        close_button.clicked.connect(self.accept)
        footer.addWidget(copy_button)
        footer.addStretch(1)
        footer.addWidget(close_button)
        layout.addLayout(footer)

    def copy_lessons(self):
        set_internal_clipboard_text(maintenance_lessons_text())
        self.lesson_browser.setToolTip("维护复盘已复制到剪贴板")


def _format_quantity(value):
    value = float(value)
    if abs(value - round(value)) < 0.0001:
        return str(int(round(value)))
    return "{:.2f}".format(value).rstrip("0").rstrip(".")


def _format_file_size(size_bytes):
    size = float(size_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024


class MaterialDropEdit(QtWidgets.QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setPlaceholderText(
            "拖入本地文件/文件夹，或粘贴 Google Drive 文件/文件夹链接（每行一个）"
        )
        self.setMaximumHeight(125)

    def paths(self):
        return [
            line.strip().strip('"')
            for line in self.toPlainText().splitlines()
            if line.strip().strip('"')
        ]

    def add_paths(self, paths):
        existing = self.paths()
        seen = {os.path.normcase(path) for path in existing}
        for raw_path in paths:
            path = str(raw_path or "").strip()
            key = os.path.normcase(path)
            if path and key not in seen:
                seen.add(key)
                existing.append(path)
        self.setPlainText("\n".join(existing))

    @staticmethod
    def _mime_sources(mime_data):
        sources = []
        if mime_data.hasUrls():
            for url in mime_data.urls():
                if url.isLocalFile():
                    sources.append(url.toLocalFile())
        sources.extend(google_drive_urls_from_mime_data(mime_data))
        return list(dict.fromkeys(source for source in sources if source))

    def insertFromMimeData(self, source):
        sources = self._mime_sources(source)
        if sources:
            self.add_paths(sources)
            return
        super().insertFromMimeData(source)

    def dragEnterEvent(self, event):
        if self._mime_sources(event.mimeData()):
            event.acceptProposedAction()
            self.setStyleSheet("border: 2px dashed #4CAF50;")
            return
        event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet("")
        event.accept()

    def dropEvent(self, event):
        self.setStyleSheet("")
        sources = self._mime_sources(event.mimeData())
        if sources:
            self.add_paths(sources)
            event.acceptProposedAction()
        else:
            event.ignore()


class AppendMaterialDialog(QtWidgets.QDialog):
    def __init__(self, material, parent=None):
        super().__init__(parent)
        self.material = material
        self.setWindowTitle("追加素材")
        self.resize(680, 390)

        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            f"追加到：{material.get('name', '')}\n"
            "可拖入多个本地文件/文件夹，也可粘贴 Google Drive 文件或文件夹链接。",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.sources_edit = MaterialDropEdit(self)
        layout.addWidget(self.sources_edit, 1)

        source_buttons = QtWidgets.QHBoxLayout()
        choose_files_button = QtWidgets.QPushButton("选择文件", self)
        choose_folder_button = QtWidgets.QPushButton("选择文件夹", self)
        paste_link_button = QtWidgets.QPushButton("粘贴网盘链接", self)
        source_buttons.addWidget(choose_files_button)
        source_buttons.addWidget(choose_folder_button)
        source_buttons.addWidget(paste_link_button)
        source_buttons.addStretch(1)
        layout.addLayout(source_buttons)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel, self)
        append_button = buttons.addButton("开始追加", QtWidgets.QDialogButtonBox.AcceptRole)
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        append_button.clicked.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        choose_files_button.clicked.connect(self.choose_files)
        choose_folder_button.clicked.connect(self.choose_folder)
        paste_link_button.clicked.connect(self.paste_links)

    def choose_files(self):
        paths, _filter = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "选择一个或多个追加文件",
        )
        if paths:
            self.sources_edit.add_paths(paths)

    def choose_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "选择要追加的文件夹",
        )
        if path:
            self.sources_edit.add_paths([path])

    def paste_links(self):
        links = google_drive_urls_from_mime_data(
            QtWidgets.QApplication.clipboard().mimeData()
        )
        if not links:
            QtWidgets.QMessageBox.warning(
                self,
                "粘贴网盘链接",
                "剪贴板中没有可识别的 Google Drive/Docs 链接。",
            )
            return
        self.sources_edit.add_paths(links)

    def _accept(self):
        if not self.sources_edit.paths():
            QtWidgets.QMessageBox.warning(
                self,
                "缺少素材",
                "请添加本地文件、文件夹或 Google Drive 链接。",
            )
            return
        self.accept()

    def source_paths(self):
        return self.sources_edit.paths()


class PersonProfileDialog(QtWidgets.QDialog):
    def __init__(self, person=None, parent=None):
        super().__init__(parent)
        self.person = person if isinstance(person, dict) else None
        self.new_avatar_path = ""
        self.remove_avatar_requested = False
        self.setWindowTitle("编辑人物资料" if self.person else "添加人物")
        self.resize(720, 650 if self.person is None else 480)

        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.name_edit = QtWidgets.QLineEdit(self)
        self.name_edit.setPlaceholderText("人物名称")
        if self.person:
            self.name_edit.setText(str(self.person.get("name") or ""))
        form.addRow("人物名称：", self.name_edit)

        avatar_widget = QtWidgets.QWidget(self)
        avatar_layout = QtWidgets.QHBoxLayout(avatar_widget)
        avatar_layout.setContentsMargins(0, 0, 0, 0)
        self.avatar_preview = QtWidgets.QLabel(avatar_widget)
        self.avatar_preview.setFixedSize(88, 88)
        self.avatar_preview.setAlignment(QtCore.Qt.AlignCenter)
        self.avatar_preview.setStyleSheet("border: 1px solid #bbb; color: #777;")
        avatar_layout.addWidget(self.avatar_preview)
        avatar_controls = QtWidgets.QVBoxLayout()
        self.avatar_path_edit = QtWidgets.QLineEdit(avatar_widget)
        self.avatar_path_edit.setReadOnly(True)
        self.avatar_path_edit.setPlaceholderText("可选；未选择时使用默认图标")
        avatar_controls.addWidget(self.avatar_path_edit)
        avatar_buttons = QtWidgets.QHBoxLayout()
        choose_avatar_button = QtWidgets.QPushButton("选择人物图片", avatar_widget)
        remove_avatar_button = QtWidgets.QPushButton("移除人物图片", avatar_widget)
        avatar_buttons.addWidget(choose_avatar_button)
        avatar_buttons.addWidget(remove_avatar_button)
        avatar_buttons.addStretch(1)
        avatar_controls.addLayout(avatar_buttons)
        avatar_layout.addLayout(avatar_controls, 1)
        form.addRow("人物图片：", avatar_widget)

        self.sheet_links_edit = QtWidgets.QPlainTextEdit(self)
        self.sheet_links_edit.setPlaceholderText(
            "每行一个 Google 表格链接；可以绑定多个"
        )
        self.sheet_links_edit.setMaximumHeight(115)
        if self.person:
            links = self.person.get("bindings", {}).get("google_sheets", [])
            self.sheet_links_edit.setPlainText("\n".join(map(str, links)))
        form.addRow("绑定表格：", self.sheet_links_edit)
        layout.addLayout(form)

        self.sources_edit = None
        if self.person is None:
            material_group = QtWidgets.QGroupBox("初始人物素材（可选）", self)
            material_layout = QtWidgets.QVBoxLayout(material_group)
            self.sources_edit = MaterialDropEdit(material_group)
            material_layout.addWidget(self.sources_edit)
            source_buttons = QtWidgets.QHBoxLayout()
            choose_files_button = QtWidgets.QPushButton("选择文件", material_group)
            choose_folder_button = QtWidgets.QPushButton("选择文件夹", material_group)
            paste_link_button = QtWidgets.QPushButton("粘贴网盘链接", material_group)
            source_buttons.addWidget(choose_files_button)
            source_buttons.addWidget(choose_folder_button)
            source_buttons.addWidget(paste_link_button)
            source_buttons.addStretch(1)
            material_layout.addLayout(source_buttons)
            layout.addWidget(material_group, 1)
            choose_files_button.clicked.connect(self.choose_material_files)
            choose_folder_button.clicked.connect(self.choose_material_folder)
            paste_link_button.clicked.connect(self.paste_material_links)

        note = QtWidgets.QLabel(
            "人物资料使用可扩展结构保存；图片会复制到人物目录，原图移动后仍可显示。",
            self,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        layout.addWidget(note)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel, self)
        save_button = buttons.addButton("保存人物", QtWidgets.QDialogButtonBox.AcceptRole)
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        save_button.clicked.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        choose_avatar_button.clicked.connect(self.choose_avatar)
        remove_avatar_button.clicked.connect(self.remove_avatar)
        current_avatar = str(self.person.get("avatar_path") or "") if self.person else ""
        self.avatar_path_edit.setText(current_avatar)
        self._update_avatar_preview(current_avatar)

    def _update_avatar_preview(self, path):
        pixmap = QtGui.QPixmap(str(path or ""))
        if pixmap.isNull():
            self.avatar_preview.setPixmap(QtGui.QPixmap())
            self.avatar_preview.setText("默认图标")
            return
        self.avatar_preview.setText("")
        self.avatar_preview.setPixmap(
            pixmap.scaled(
                self.avatar_preview.size(),
                QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.SmoothTransformation,
            )
        )

    def choose_avatar(self):
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择人物图片",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if not path:
            return
        self.new_avatar_path = path
        self.remove_avatar_requested = False
        self.avatar_path_edit.setText(path)
        self._update_avatar_preview(path)

    def remove_avatar(self):
        self.new_avatar_path = ""
        self.remove_avatar_requested = True
        self.avatar_path_edit.clear()
        self._update_avatar_preview("")

    def choose_material_files(self):
        paths, _filter = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "选择一个或多个人物素材文件",
        )
        if paths:
            self.sources_edit.add_paths(paths)

    def choose_material_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "选择人物素材文件夹",
        )
        if path:
            self.sources_edit.add_paths([path])

    def paste_material_links(self):
        links = google_drive_urls_from_mime_data(
            QtWidgets.QApplication.clipboard().mimeData()
        )
        if not links:
            QtWidgets.QMessageBox.warning(
                self,
                "粘贴网盘链接",
                "剪贴板中没有可识别的 Google Drive/Docs 链接。",
            )
            return
        self.sources_edit.add_paths(links)

    def _accept(self):
        if not self.name_edit.text().strip():
            QtWidgets.QMessageBox.warning(self, "缺少名称", "请输入人物名称。")
            return
        self.accept()

    def values(self):
        return {
            "name": self.name_edit.text().strip(),
            "avatar_path": self.new_avatar_path,
            "remove_avatar": self.remove_avatar_requested,
            "google_sheet_links": [
                line.strip()
                for line in self.sheet_links_edit.toPlainText().splitlines()
                if line.strip()
            ],
            "source_paths": self.sources_edit.paths() if self.sources_edit else [],
        }


class ImportMaterialsToPersonDialog(QtWidgets.QDialog):
    def __init__(self, person, materials, parent=None):
        super().__init__(parent)
        self.person = dict(person or {})
        self.materials = list(materials or [])
        self.setWindowTitle("从素材管理导入")
        self.resize(820, 520)

        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            f"选择要导入到人物“{self.person.get('name', '')}”的素材。"
            "可一次选择多项，导入后会按素材名称分别建文件夹。",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        choose_toolbar = QtWidgets.QHBoxLayout()
        select_all_button = QtWidgets.QPushButton("全选", self)
        clear_all_button = QtWidgets.QPushButton("全不选", self)
        choose_toolbar.addWidget(select_all_button)
        choose_toolbar.addWidget(clear_all_button)
        choose_toolbar.addStretch(1)
        layout.addLayout(choose_toolbar)

        self.table = QtWidgets.QTableWidget(len(self.materials), 5, self)
        self.table.setHorizontalHeaderLabels(
            ["选择", "素材名称", "文件数", "占用空间", "素材位置"]
        )
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0,
            QtWidgets.QHeaderView.ResizeToContents,
        )
        for column in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(
                column,
                QtWidgets.QHeaderView.ResizeToContents,
            )
        self.table.horizontalHeader().setSectionResizeMode(
            4,
            QtWidgets.QHeaderView.Stretch,
        )
        for row, material in enumerate(self.materials):
            stats = material_directory_stats(material.get("path", ""))
            check_item = QtWidgets.QTableWidgetItem("导入")
            check_item.setFlags(
                QtCore.Qt.ItemIsEnabled
                | QtCore.Qt.ItemIsSelectable
                | QtCore.Qt.ItemIsUserCheckable
            )
            check_item.setCheckState(QtCore.Qt.Unchecked)
            check_item.setData(QtCore.Qt.UserRole, material.get("id"))
            self.table.setItem(row, 0, check_item)
            values = (
                material.get("name", ""),
                stats.get("file_count", 0),
                _format_file_size(stats.get("size_bytes", 0)),
                material.get("path", ""),
            )
            for column, value in enumerate(values, start=1):
                self.table.setItem(row, column, QtWidgets.QTableWidgetItem(str(value)))
        layout.addWidget(self.table, 1)

        self.remove_originals_checkbox = QtWidgets.QCheckBox(
            "导入成功后从“素材管理”移除原素材",
            self,
        )
        self.remove_originals_checkbox.setToolTip(
            "勾选后，只有全部素材成功导入人物目录，才会移除原素材记录和原素材目录；"
            "导入失败时不会删除。"
        )
        layout.addWidget(self.remove_originals_checkbox)

        warning = QtWidgets.QLabel(
            "默认不勾选：人物目录得到一份副本，素材管理中的原记录和文件继续保留。",
            self,
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #666;")
        layout.addWidget(warning)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel, self)
        import_button = buttons.addButton("开始导入", QtWidgets.QDialogButtonBox.AcceptRole)
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        import_button.clicked.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        clear_all_button.clicked.connect(lambda: self._set_all_checked(False))

    def _set_all_checked(self, checked):
        state = QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(state)

    def selected_material_ids(self):
        return [
            str(self.table.item(row, 0).data(QtCore.Qt.UserRole))
            for row in range(self.table.rowCount())
            if self.table.item(row, 0).checkState() == QtCore.Qt.Checked
        ]

    def remove_originals(self):
        return self.remove_originals_checkbox.isChecked()

    def _accept(self):
        if not self.selected_material_ids():
            QtWidgets.QMessageBox.warning(
                self,
                "尚未选择素材",
                "请至少勾选一项要导入的人物素材。",
            )
            return
        self.accept()


class MaterialCopyThread(QtCore.QThread):
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)
    progress = QtCore.pyqtSignal(str)

    def __init__(
        self,
        store,
        name,
        source_paths,
        parent=None,
        material_id=None,
        operation="material",
        profile=None,
        use_drive_folder_name=False,
    ):
        super().__init__(parent)
        self.store = store
        self.name = name
        self.source_paths = list(source_paths)
        self.material_id = material_id
        self.operation = operation
        self.profile = dict(profile or {})
        self.use_drive_folder_name = bool(use_drive_folder_name)

    def run(self):
        try:
            if self.operation == "person_add":
                result = self.store.add_person(
                    self.profile.get("name"),
                    avatar_path=self.profile.get("avatar_path", ""),
                    google_sheet_links=self.profile.get("google_sheet_links", []),
                    source_paths=self.source_paths,
                    progress_callback=self.progress.emit,
                )
            elif self.operation == "person_import":
                result = self.store.import_materials_to_person(
                    self.material_id,
                    self.profile.get("material_ids", []),
                    remove_originals=self.profile.get("remove_originals", False),
                    progress_callback=self.progress.emit,
                )
            elif self.operation == "person_append":
                result = self.store.append_person_material(
                    self.material_id,
                    self.source_paths,
                    progress_callback=self.progress.emit,
                )
            elif self.material_id:
                result = self.store.append_material(
                    self.material_id,
                    self.source_paths,
                    progress_callback=self.progress.emit,
                )
            else:
                result = self.store.add_material(
                    self.name,
                    self.source_paths,
                    progress_callback=self.progress.emit,
                    use_drive_folder_name=self.use_drive_folder_name,
                )
            self.completed.emit(result)
        except Exception as error:
            self.failed.emit(str(error))




class MaterialGroupAssignmentDialog(QtWidgets.QDialog):
    """Select whole material/person entries and distribute one image per task."""

    def __init__(self, groups, tasks, parent=None):
        super().__init__(parent)
        self.groups = list(groups or [])
        self.tasks = list(tasks or [])
        self.setWindowTitle("按素材条目分配图片")
        self.resize(920, 560)

        layout = QtWidgets.QVBoxLayout(self)
        task_names = [str(task.get("label") or "") for task in self.tasks]
        self.intro_label = QtWidgets.QLabel(
            f"已选 {len(task_names)} 个任务。勾选素材条目后，每个任务分配 1 张。",
            self,
        )
        task_tooltip_names = task_names[:20]
        if len(task_names) > 20:
            task_tooltip_names.append(f"……另有 {len(task_names) - 20} 个任务")
        self.intro_label.setToolTip(
            "图片按素材条目从上到下取用，再按任务列表顺序移动。\n"
            "目标任务：\n" + "\n".join(task_tooltip_names)
        )
        layout.addWidget(self.intro_label)

        toolbar = QtWidgets.QHBoxLayout()
        self.filter_edit = QtWidgets.QLineEdit(self)
        self.filter_edit.setPlaceholderText("筛选素材条目名称、类型或路径")
        select_visible_btn = QtWidgets.QPushButton("勾选筛选结果", self)
        material_only_btn = QtWidgets.QPushButton("只选素材管理", self)
        person_only_btn = QtWidgets.QPushButton("只选人物素材", self)
        clear_btn = QtWidgets.QPushButton("清除勾选", self)
        toolbar.addWidget(self.filter_edit, 1)
        toolbar.addWidget(select_visible_btn)
        toolbar.addWidget(material_only_btn)
        toolbar.addWidget(person_only_btn)
        toolbar.addWidget(clear_btn)
        layout.addLayout(toolbar)

        order_toolbar = QtWidgets.QHBoxLayout()
        order_note = QtWidgets.QLabel(
            "取图顺序：从上到下",
            self,
        )
        order_note.setToolTip("选中条目后，可用“上移/下移”调整图片取用顺序。")
        self.move_up_btn = QtWidgets.QPushButton("上移", self)
        self.move_down_btn = QtWidgets.QPushButton("下移", self)
        self.open_group_btn = QtWidgets.QPushButton("打开条目目录", self)
        order_toolbar.addWidget(order_note)
        order_toolbar.addStretch(1)
        order_toolbar.addWidget(self.move_up_btn)
        order_toolbar.addWidget(self.move_down_btn)
        order_toolbar.addWidget(self.open_group_btn)
        layout.addLayout(order_toolbar)

        self.group_table = QtWidgets.QTableWidget(0, 5, self)
        self.group_table.setHorizontalHeaderLabels(
            ["选择", "来源", "素材条目", "可用图片", "保存位置"]
        )
        self.group_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.group_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.group_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.group_table.verticalHeader().setVisible(False)
        self.group_table.horizontalHeader().setMaximumSectionSize(420)
        for column in range(4):
            self.group_table.horizontalHeader().setSectionResizeMode(
                column,
                QtWidgets.QHeaderView.ResizeToContents,
            )
        self.group_table.horizontalHeader().setSectionResizeMode(
            4,
            QtWidgets.QHeaderView.Stretch,
        )
        layout.addWidget(self.group_table, 1)

        self.distribution_label = QtWidgets.QLabel("", self)
        self.distribution_label.setWordWrap(True)
        self.distribution_label.setStyleSheet(
            "background:#F4F6F8;border:1px solid #D8DDE3;padding:8px;"
        )
        layout.addWidget(self.distribution_label)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel, self)
        self.assign_button = buttons.addButton(
            "开始分配",
            QtWidgets.QDialogButtonBox.AcceptRole,
        )
        self.assign_button.setToolTip(
            "把所选条目中的图片按顺序移动到任务目录，每个任务 1 张。"
        )
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        self.assign_button.clicked.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.filter_edit.textChanged.connect(self.apply_filter)
        select_visible_btn.clicked.connect(lambda: self.set_visible_checked(True))
        material_only_btn.clicked.connect(lambda: self.select_source_kind("material"))
        person_only_btn.clicked.connect(lambda: self.select_source_kind("person"))
        clear_btn.clicked.connect(self.clear_checks)
        self.move_up_btn.clicked.connect(lambda: self.move_current_group(-1))
        self.move_down_btn.clicked.connect(lambda: self.move_current_group(1))
        self.open_group_btn.clicked.connect(self.open_current_group)
        self.group_table.cellDoubleClicked.connect(
            lambda _row, _column: self.open_current_group()
        )
        self.group_table.itemChanged.connect(self.update_distribution_label)
        self.populate()

    @staticmethod
    def group_key(group):
        return (
            str(group.get("source_kind") or ""),
            str(group.get("source_id") or ""),
        )

    def checked_group_keys(self):
        keys = set()
        for row in range(self.group_table.rowCount()):
            if self.group_table.item(row, 0).checkState() == QtCore.Qt.Checked:
                group = self.group_table.item(row, 2).data(QtCore.Qt.UserRole)
                keys.add(self.group_key(group))
        return keys

    def populate(self, checked_keys=None, selected_row=None):
        checked_keys = (
            self.checked_group_keys()
            if checked_keys is None and self.group_table.rowCount()
            else set(checked_keys or ())
        )
        self.group_table.blockSignals(True)
        self.group_table.setRowCount(len(self.groups))
        for row, group in enumerate(self.groups):
            check_item = QtWidgets.QTableWidgetItem("")
            check_item.setFlags(
                QtCore.Qt.ItemIsEnabled
                | QtCore.Qt.ItemIsSelectable
                | QtCore.Qt.ItemIsUserCheckable
            )
            check_item.setCheckState(
                QtCore.Qt.Checked
                if self.group_key(group) in checked_keys
                else QtCore.Qt.Unchecked
            )
            type_item = QtWidgets.QTableWidgetItem(
                str(group.get("source_type_label") or "")
            )
            name_item = QtWidgets.QTableWidgetItem(str(group.get("name") or ""))
            name_item.setData(QtCore.Qt.UserRole, group)
            count_item = QtWidgets.QTableWidgetItem(
                str(len(group.get("images") or []))
            )
            count_item.setTextAlignment(QtCore.Qt.AlignCenter)
            path_item = QtWidgets.QTableWidgetItem(str(group.get("path") or ""))
            path_item.setToolTip(str(group.get("path") or ""))
            self.group_table.setItem(row, 0, check_item)
            self.group_table.setItem(row, 1, type_item)
            self.group_table.setItem(row, 2, name_item)
            self.group_table.setItem(row, 3, count_item)
            self.group_table.setItem(row, 4, path_item)
        self.group_table.blockSignals(False)
        self.apply_filter(self.filter_edit.text())
        if self.groups:
            row = 0 if selected_row is None else max(
                0,
                min(int(selected_row), len(self.groups) - 1),
            )
            self.group_table.selectRow(row)
        self.update_distribution_label()

    def apply_filter(self, text):
        needle = str(text or "").strip().casefold()
        for row, group in enumerate(self.groups):
            haystack = " ".join(
                str(group.get(key) or "")
                for key in ("source_type_label", "name", "path")
            ).casefold()
            self.group_table.setRowHidden(
                row,
                bool(needle and needle not in haystack),
            )

    def set_visible_checked(self, checked):
        for row in range(self.group_table.rowCount()):
            if not self.group_table.isRowHidden(row):
                self.group_table.item(row, 0).setCheckState(
                    QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked
                )

    def select_source_kind(self, source_kind):
        for row, group in enumerate(self.groups):
            self.group_table.item(row, 0).setCheckState(
                QtCore.Qt.Checked
                if group.get("source_kind") == source_kind
                else QtCore.Qt.Unchecked
            )

    def clear_checks(self):
        for row in range(self.group_table.rowCount()):
            self.group_table.item(row, 0).setCheckState(QtCore.Qt.Unchecked)

    def move_current_group(self, offset):
        row = self.group_table.currentRow()
        target_row = row + int(offset)
        if row < 0 or target_row < 0 or target_row >= len(self.groups):
            return
        checked_keys = self.checked_group_keys()
        self.groups[row], self.groups[target_row] = (
            self.groups[target_row],
            self.groups[row],
        )
        self.populate(checked_keys, selected_row=target_row)

    def selected_groups(self):
        selected = []
        for row in range(self.group_table.rowCount()):
            if self.group_table.item(row, 0).checkState() != QtCore.Qt.Checked:
                continue
            group = self.group_table.item(row, 2).data(QtCore.Qt.UserRole)
            if group:
                selected.append(group)
        return selected

    def selected_images(self):
        images = []
        seen_paths = set()
        for group in self.selected_groups():
            for image in group.get("images") or []:
                path = os.path.normcase(str(image.get("path") or ""))
                if path and path not in seen_paths:
                    seen_paths.add(path)
                    images.append(image)
        return images

    def distribution_summary(self):
        images = self.selected_images()
        assigned_count = min(len(images), len(self.tasks))
        missing_count = max(0, len(self.tasks) - assigned_count)
        remaining_count = max(0, len(images) - assigned_count)
        return {
            "selected_group_count": len(self.selected_groups()),
            "available_image_count": len(images),
            "assigned_count": assigned_count,
            "missing_count": missing_count,
            "remaining_count": remaining_count,
            "last_assigned_task": (
                self.tasks[assigned_count - 1].get("label")
                if assigned_count
                else ""
            ),
            "first_missing_task": (
                self.tasks[assigned_count].get("label")
                if missing_count
                else ""
            ),
        }

    def update_distribution_label(self, _item=None):
        summary = self.distribution_summary()
        group_count = summary["selected_group_count"]
        image_count = summary["available_image_count"]
        if not group_count:
            text = "请选择素材条目。"
            details = "勾选一个或多个“素材管理”或“人物素材”条目。"
        elif not image_count:
            text = f"已选 {group_count} 项，但没有可用图片。"
            details = "所选素材条目中没有可分配的图片。"
        elif summary["missing_count"]:
            text = (
                f"可分配 {summary['assigned_count']}/{len(self.tasks)} 个任务，"
                f"还缺 {summary['missing_count']} 张。"
            )
            details = (
                f"可用图片：{image_count} 张；"
                f"最后分配到：{summary['last_assigned_task'] or '无'}；"
                f"从“{summary['first_missing_task'] or '未知任务'}”开始缺图。"
            )
        else:
            text = f"可分配全部 {summary['assigned_count']} 个任务。"
            if summary["remaining_count"]:
                text += f" 剩余 {summary['remaining_count']} 张。"
            details = (
                f"已选 {group_count} 个素材条目，共 {image_count} 张图片；"
                "每个任务移动 1 张，其余图片留在原素材条目中。"
            )
        self.distribution_label.setText(text)
        self.distribution_label.setToolTip(details)
        self.assign_button.setEnabled(bool(summary["assigned_count"]))

    def assignments(self):
        return [
            {
                "path": image.get("path"),
                "target_dir": task.get("target_dir"),
                "task_label": task.get("label"),
            }
            for image, task in zip(self.selected_images(), self.tasks)
        ]

    def open_current_group(self):
        row = self.group_table.currentRow()
        if row < 0:
            return
        group = self.group_table.item(row, 2).data(QtCore.Qt.UserRole)
        path = Path(str((group or {}).get("path") or ""))
        if not path.exists():
            QtWidgets.QMessageBox.warning(
                self,
                "打开素材条目",
                f"保存位置不存在：\n{path}",
            )
            return
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(path.resolve()))
        )

    def _accept(self):
        summary = self.distribution_summary()
        if not summary["selected_group_count"]:
            QtWidgets.QMessageBox.information(
                self,
                "请选择素材条目",
                "请勾选至少一个“素材管理”或“人物素材”条目。",
            )
            return
        if not summary["assigned_count"]:
            QtWidgets.QMessageBox.information(
                self,
                "没有可分配图片",
                "所选素材条目中没有可分配的图片。",
            )
            return
        self.accept()


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
    log = QtCore.pyqtSignal(str)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)
        self.setModal(False)
        self.setWindowModality(QtCore.Qt.NonModal)
        self.store = store
        self.material_copy_thread = None
        self.material_copy_operation = None
        self.pending_material_sources = []
        self.setWindowTitle("库存与素材管理器")
        self.resize(900, 620)

        layout = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget(self)
        self.inventory_tab = QtWidgets.QWidget(self.tabs)
        inventory_layout = QtWidgets.QVBoxLayout(self.inventory_tab)
        toolbar = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton("添加库存")
        self.edit_button = QtWidgets.QPushButton("修改")
        self.stock_button = QtWidgets.QPushButton("补充数量")
        self.delete_button = QtWidgets.QPushButton("删除")
        for button in (self.add_button, self.edit_button, self.stock_button, self.delete_button):
            toolbar.addWidget(button)
        toolbar.addStretch()
        inventory_layout.addLayout(toolbar)

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
        inventory_layout.addWidget(self.table)

        legend = QtWidgets.QLabel(
            "颜色：黄色＝不足 2 天（初步）　橙色＝不足 1 天（中度）　粉红＝已经耗尽（高危）\n"
            "剩余数量按实际经过时间持续估算，不要求程序一直开着。"
        )
        legend.setWordWrap(True)
        inventory_layout.addWidget(legend)

        self.tabs.addTab(self.inventory_tab, "库存管理")
        self.material_tab = QtWidgets.QWidget(self.tabs)
        self._build_material_tab()
        self.tabs.addTab(self.material_tab, "素材管理")
        self.person_tab = QtWidgets.QWidget(self.tabs)
        self._build_person_tab()
        self.tabs.addTab(self.person_tab, "人物素材管理")
        layout.addWidget(self.tabs)

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
        self.refresh_materials()
        self.refresh_people()

    def _build_material_tab(self):
        layout = QtWidgets.QVBoxLayout(self.material_tab)
        intro = QtWidgets.QLabel(
            "填写名称后，可混合添加本地文件、文件夹及 Google Drive 文件/文件夹链接。"
            "网盘内容会在后台下载；Google 文档、表格和演示文稿会自动导出。"
            "全部成功后才会写入素材库，双击列表可打开目录。",
            self.material_tab,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QtWidgets.QFormLayout()
        name_row = QtWidgets.QWidget(self.material_tab)
        name_row_layout = QtWidgets.QHBoxLayout(name_row)
        name_row_layout.setContentsMargins(0, 0, 0, 0)
        self.material_name_edit = QtWidgets.QLineEdit(self.material_tab)
        self.material_name_edit.setPlaceholderText("例如：常用片头、客户 Logo")
        self.material_use_drive_folder_name_checkbox = QtWidgets.QCheckBox(
            "使用谷歌文件夹名称",
            name_row,
        )
        self.material_use_drive_folder_name_checkbox.setToolTip(
            "保存时读取第一个 Google Drive 文件夹的真实名称；"
            "多个来源时仍使用第一个谷歌文件夹命名"
        )
        name_row_layout.addWidget(self.material_name_edit, 1)
        name_row_layout.addWidget(self.material_use_drive_folder_name_checkbox)
        form.addRow("素材名称：", name_row)
        self.material_sources_edit = MaterialDropEdit(self.material_tab)
        form.addRow("素材来源：", self.material_sources_edit)
        layout.addLayout(form)

        source_buttons = QtWidgets.QHBoxLayout()
        self.material_add_files_btn = QtWidgets.QPushButton("选择文件", self.material_tab)
        self.material_add_folder_btn = QtWidgets.QPushButton("选择文件夹", self.material_tab)
        self.material_paste_link_btn = QtWidgets.QPushButton("粘贴网盘链接", self.material_tab)
        self.material_submit_btn = QtWidgets.QPushButton("保存到素材库", self.material_tab)
        source_buttons.addWidget(self.material_add_files_btn)
        source_buttons.addWidget(self.material_add_folder_btn)
        source_buttons.addWidget(self.material_paste_link_btn)
        source_buttons.addStretch(1)
        source_buttons.addWidget(self.material_submit_btn)
        layout.addLayout(source_buttons)

        library_toolbar = QtWidgets.QHBoxLayout()
        self.material_check_btn = QtWidgets.QPushButton("查库", self.material_tab)
        self.material_append_btn = QtWidgets.QPushButton("追加素材", self.material_tab)
        self.material_open_library_btn = QtWidgets.QPushButton("打开素材库", self.material_tab)
        self.material_remove_btn = QtWidgets.QPushButton("移除记录", self.material_tab)
        self.material_copy_sources_btn = QtWidgets.QPushButton("复制来源", self.material_tab)
        self.material_remove_btn.setToolTip("只移除管理记录，不删除素材库中的文件")
        library_toolbar.addWidget(self.material_check_btn)
        library_toolbar.addWidget(self.material_append_btn)
        library_toolbar.addWidget(self.material_open_library_btn)
        library_toolbar.addWidget(self.material_remove_btn)
        library_toolbar.addWidget(self.material_copy_sources_btn)
        library_toolbar.addStretch(1)
        layout.addLayout(library_toolbar)

        self.material_table = QtWidgets.QTableWidget(0, 7, self.material_tab)
        self.material_table.setHorizontalHeaderLabels(
            ["素材名称", "状态", "来源", "文件数", "占用空间", "添加时间", "保存位置"]
        )
        self.material_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.material_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.material_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.material_table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.material_table.verticalHeader().setVisible(False)
        self.material_table.horizontalHeader().setSectionResizeMode(
            0,
            QtWidgets.QHeaderView.ResizeToContents,
        )
        for column in range(1, 6):
            self.material_table.horizontalHeader().setSectionResizeMode(
                column,
                QtWidgets.QHeaderView.ResizeToContents,
            )
        self.material_table.horizontalHeader().setSectionResizeMode(
            6,
            QtWidgets.QHeaderView.Stretch,
        )
        layout.addWidget(self.material_table, 1)

        self.material_status_label = QtWidgets.QLabel(
            f"素材库位置：{self.store.material_root}",
            self.material_tab,
        )
        self.material_status_label.setWordWrap(True)
        self.material_status_label.setStyleSheet("color: #555;")
        layout.addWidget(self.material_status_label)

        self.material_add_files_btn.clicked.connect(self.choose_material_files)
        self.material_add_folder_btn.clicked.connect(self.choose_material_folder)
        self.material_paste_link_btn.clicked.connect(self.paste_material_links)
        self.material_submit_btn.clicked.connect(self.add_material)
        self.material_use_drive_folder_name_checkbox.toggled.connect(
            self._update_material_name_mode
        )
        self.material_check_btn.clicked.connect(self.check_materials)
        self.material_append_btn.clicked.connect(self.append_material)
        self.material_open_library_btn.clicked.connect(self.open_material_library)
        self.material_remove_btn.clicked.connect(self.remove_material_record)
        self.material_copy_sources_btn.clicked.connect(self.copy_selected_material_sources)
        self.material_table.doubleClicked.connect(self.open_selected_material)
        self.material_table.customContextMenuRequested.connect(
            self.show_material_context_menu
        )
        self._update_material_name_mode()

    def prepare_material_sources(self, sources, source_label="剪贴板"):
        """Show the material tab and merge incoming links into its source editor."""
        sources = list(sources or [])
        if self.material_copy_thread is not None:
            existing_keys = {
                os.path.normcase(str(source))
                for source in (
                    self.material_sources_edit.paths()
                    + self.pending_material_sources
                )
            }
            added_count = 0
            for source in sources:
                source = str(source or "").strip()
                key = os.path.normcase(source)
                if source and key not in existing_keys:
                    existing_keys.add(key)
                    self.pending_material_sources.append(source)
                    added_count += 1
            self.tabs.setCurrentWidget(self.material_tab)
            self.material_status_label.setText(
                f"当前素材仍在处理；已把{source_label}中的 {added_count} 个新链接排队，"
                "本轮完成后自动填入。"
            )
            return added_count
        before = self.material_sources_edit.paths()
        self.material_sources_edit.add_paths(sources)
        after = self.material_sources_edit.paths()
        added_count = max(0, len(after) - len(before))
        self.tabs.setCurrentWidget(self.material_tab)
        self.material_status_label.setText(
            f"已从{source_label}接收 {len(sources)} 个链接，"
            f"新增 {added_count} 个；填写素材名称后即可保存并下载。"
        )
        self.material_name_edit.setFocus()
        return added_count

    def _build_person_tab(self):
        layout = QtWidgets.QVBoxLayout(self.person_tab)
        intro = QtWidgets.QLabel(
            "每个人物可以保存独立图片、多个 Google 表格链接和专属素材。"
            "双击人物可打开其素材目录，右键可快速追加素材或管理绑定信息。",
            self.person_tab,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        primary_toolbar = QtWidgets.QHBoxLayout()
        self.person_add_btn = QtWidgets.QPushButton("添加人物", self.person_tab)
        self.person_edit_btn = QtWidgets.QPushButton("编辑资料", self.person_tab)
        self.person_append_btn = QtWidgets.QPushButton("追加素材", self.person_tab)
        self.person_import_btn = QtWidgets.QPushButton("从素材管理导入", self.person_tab)
        self.person_open_material_btn = QtWidgets.QPushButton("打开人物素材", self.person_tab)
        for button in (
            self.person_add_btn,
            self.person_edit_btn,
            self.person_append_btn,
            self.person_import_btn,
            self.person_open_material_btn,
        ):
            primary_toolbar.addWidget(button)
        primary_toolbar.addStretch(1)
        layout.addLayout(primary_toolbar)

        secondary_toolbar = QtWidgets.QHBoxLayout()
        self.person_open_sheet_btn = QtWidgets.QPushButton("打开绑定表格", self.person_tab)
        self.person_copy_sheets_btn = QtWidgets.QPushButton("复制表格链接", self.person_tab)
        self.person_check_btn = QtWidgets.QPushButton("查库", self.person_tab)
        self.person_remove_btn = QtWidgets.QPushButton("移除记录", self.person_tab)
        self.person_remove_btn.setToolTip("只移除人物记录，不删除人物目录和素材文件")
        for button in (
            self.person_open_sheet_btn,
            self.person_copy_sheets_btn,
            self.person_check_btn,
            self.person_remove_btn,
        ):
            secondary_toolbar.addWidget(button)
        secondary_toolbar.addStretch(1)
        layout.addLayout(secondary_toolbar)

        self.people_table = QtWidgets.QTableWidget(0, 7, self.person_tab)
        self.people_table.setHorizontalHeaderLabels([
            "人物", "绑定表格", "素材来源", "文件数", "占用空间", "更新时间", "素材位置"
        ])
        self.people_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.people_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.people_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.people_table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.people_table.setIconSize(QtCore.QSize(56, 56))
        self.people_table.verticalHeader().setVisible(False)
        self.people_table.horizontalHeader().setSectionResizeMode(
            0,
            QtWidgets.QHeaderView.ResizeToContents,
        )
        for column in range(1, 6):
            self.people_table.horizontalHeader().setSectionResizeMode(
                column,
                QtWidgets.QHeaderView.ResizeToContents,
            )
        self.people_table.horizontalHeader().setSectionResizeMode(
            6,
            QtWidgets.QHeaderView.Stretch,
        )
        layout.addWidget(self.people_table, 1)

        self.person_status_label = QtWidgets.QLabel(
            f"人物素材库位置：{self.store.people_root}",
            self.person_tab,
        )
        self.person_status_label.setWordWrap(True)
        self.person_status_label.setStyleSheet("color: #555;")
        layout.addWidget(self.person_status_label)

        self.person_add_btn.clicked.connect(self.add_person)
        self.person_edit_btn.clicked.connect(self.edit_person)
        self.person_append_btn.clicked.connect(self.append_person_material)
        self.person_import_btn.clicked.connect(self.import_person_materials)
        self.person_open_material_btn.clicked.connect(self.open_selected_person_material)
        self.person_open_sheet_btn.clicked.connect(self.show_person_sheet_menu)
        self.person_copy_sheets_btn.clicked.connect(self.copy_person_sheet_links)
        self.person_check_btn.clicked.connect(self.check_people)
        self.person_remove_btn.clicked.connect(self.remove_person_record)
        self.people_table.doubleClicked.connect(self.open_selected_person_material)
        self.people_table.customContextMenuRequested.connect(
            self.show_person_context_menu
        )

    def _selected(self):
        row = self.table.currentRow()
        if row < 0:
            QtWidgets.QMessageBox.information(self, "请选择库存", "请先选择一行库存。")
            return None
        item = self.table.item(row, 0)
        return item.data(QtCore.Qt.UserRole) if item else None

    def _handle_error(self, error):
        QtWidgets.QMessageBox.critical(self, "库存操作失败", str(error))

    def _handle_material_error(self, error):
        QtWidgets.QMessageBox.critical(self, "素材操作失败", str(error))

    def _handle_person_error(self, error):
        QtWidgets.QMessageBox.critical(self, "人物素材操作失败", str(error))

    def choose_material_files(self):
        paths, _filter = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "选择一个或多个素材文件",
        )
        if paths:
            self.material_sources_edit.add_paths(paths)

    def choose_material_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "选择素材文件夹",
        )
        if path:
            self.material_sources_edit.add_paths([path])

    def paste_material_links(self):
        mime_data = QtWidgets.QApplication.clipboard().mimeData()
        if not mime_data.hasText() and not mime_data.hasHtml():
            QtWidgets.QMessageBox.information(
                self,
                "粘贴网盘链接",
                "剪贴板中没有文字。",
            )
            return
        links = google_drive_urls_from_mime_data(mime_data)
        if not links:
            QtWidgets.QMessageBox.warning(
                self,
                "粘贴网盘链接",
                "剪贴板中没有可识别的 Google Drive/Docs 链接。",
            )
            return
        self.material_sources_edit.add_paths(links)

    def _update_material_name_mode(self, _checked=None, copying=None):
        automatic = self.material_use_drive_folder_name_checkbox.isChecked()
        if copying is None:
            copying = self.material_copy_thread is not None
        self.material_name_edit.setEnabled(not copying and not automatic)
        self.material_name_edit.setPlaceholderText(
            "保存时自动读取第一个谷歌文件夹名称"
            if automatic else "例如：常用片头、客户 Logo"
        )

    def _set_material_copying(self, copying):
        for widget in (
            self.add_button,
            self.edit_button,
            self.stock_button,
            self.delete_button,
            self.material_name_edit,
            self.material_use_drive_folder_name_checkbox,
            self.material_sources_edit,
            self.material_add_files_btn,
            self.material_add_folder_btn,
            self.material_paste_link_btn,
            self.material_submit_btn,
            self.material_check_btn,
            self.material_append_btn,
            self.material_remove_btn,
            self.material_copy_sources_btn,
            self.material_table,
            self.person_add_btn,
            self.person_edit_btn,
            self.person_append_btn,
            self.person_import_btn,
            self.person_open_material_btn,
            self.person_open_sheet_btn,
            self.person_copy_sheets_btn,
            self.person_check_btn,
            self.person_remove_btn,
            self.people_table,
        ):
            widget.setEnabled(not copying)
        self.material_submit_btn.setText(
            "正在复制，请稍候…" if copying else "保存到素材库"
        )
        self._update_material_name_mode(copying=copying)

    def add_material(self):
        name = self.material_name_edit.text().strip()
        paths = self.material_sources_edit.paths()
        use_drive_folder_name = (
            self.material_use_drive_folder_name_checkbox.isChecked()
        )
        if not name and not use_drive_folder_name:
            QtWidgets.QMessageBox.warning(self, "缺少名称", "请输入素材名称。")
            return
        if not paths:
            QtWidgets.QMessageBox.warning(
                self,
                "缺少素材",
                "请添加本地文件、文件夹或 Google Drive 链接。",
            )
            return
        if self.material_copy_thread is not None:
            return

        self._set_material_copying(True)
        display_name = name if not use_drive_folder_name else "谷歌文件夹原名"
        self.material_status_label.setText(
            f"正在把“{display_name}”复制到素材库……"
        )
        self.material_copy_operation = {
            "kind": "material_add",
            "name": name,
            "sources": list(paths),
            "use_drive_folder_name": use_drive_folder_name,
        }
        thread = MaterialCopyThread(
            self.store,
            name,
            paths,
            self,
            use_drive_folder_name=use_drive_folder_name,
        )
        self.material_copy_thread = thread
        thread.completed.connect(self._material_copy_completed)
        thread.failed.connect(self._material_copy_failed)
        thread.progress.connect(self._material_copy_progress)
        thread.finished.connect(self._material_copy_finished)
        thread.start()

    def _material_copy_progress(self, message):
        message = str(message or "").strip()
        if not message:
            return
        operation = self.material_copy_operation or {}
        if str(operation.get("kind", "")).startswith("person_"):
            self.person_status_label.setText(message)
        else:
            self.material_status_label.setText(message)
        self.log.emit(message)

    def _material_copy_completed(self, material):
        operation = self.material_copy_operation or {}
        kind = operation.get("kind")
        if kind == "person_import":
            person = material.get("person", {})
            imported_count = len(material.get("imported_materials", []))
            removed_count = len(material.get("removed_materials", []))
            self.refresh_people(selected_id=person.get("id"))
            self.refresh_materials()
            message = (
                f"已向人物“{person.get('name', '')}”导入 {imported_count} 项素材"
            )
            if removed_count:
                message += f"，并从素材管理移除 {removed_count} 项原素材"
            self.person_status_label.setText(message)
            self.log.emit(message)
            self.changed.emit()
            return
        if str(kind).startswith("person_"):
            appended = kind == "person_append"
            self.refresh_people(selected_id=material.get("id"))
            action_text = "已追加人物素材" if appended else "已添加人物"
            message = f"{action_text}“{material.get('name', '')}”"
            self.person_status_label.setText(message)
            self.log.emit(message)
            self.changed.emit()
            return

        appended = kind == "material_append"
        if not appended:
            self.material_name_edit.clear()
            self.material_sources_edit.clear()
        self.refresh_materials(selected_id=material.get("id"))
        action_text = "已追加到" if appended else "已保存"
        self.material_status_label.setText(
            f"{action_text}“{material.get('name', '')}”：{material.get('path', '')}"
        )
        self.log.emit(
            f"{action_text}“{material.get('name', '')}”：{material.get('path', '')}"
        )
        self.changed.emit()

    def _material_copy_failed(self, message):
        operation = self.material_copy_operation or {}
        kind = operation.get("kind")
        if kind == "person_import":
            self.person_status_label.setText(
                "从素材管理导入失败，人物素材和原素材均未改变。"
            )
            self.log.emit(f"人物素材库导入失败：{message}")
            self._handle_person_error(message)
            return
        if str(kind).startswith("person_"):
            appended = kind == "person_append"
            action_text = "追加" if appended else "添加"
            self.person_status_label.setText(
                "人物素材追加失败，原人物素材未改变。"
                if appended
                else "人物添加失败。"
            )
            self.log.emit(f"人物{action_text}失败：{message}")
            self._handle_person_error(message)
            return

        appended = kind == "material_append"
        action_text = "追加" if appended else "保存"
        self.material_status_label.setText(
            "素材追加失败，原素材未改变。"
            if appended
            else "素材复制失败，原输入内容已保留。"
        )
        self.log.emit(f"{action_text}失败：{message}")
        self._handle_material_error(message)

    def _material_copy_finished(self):
        thread = self.material_copy_thread
        self.material_copy_thread = None
        self._set_material_copying(False)
        self.material_copy_operation = None
        if thread is not None:
            thread.deleteLater()
        if self.pending_material_sources:
            pending = self.pending_material_sources
            self.pending_material_sources = []
            self.prepare_material_sources(pending, source_label="剪贴板排队")

    def append_material(self):
        material = self._selected_material()
        if not material or self.material_copy_thread is not None:
            return
        dialog = AppendMaterialDialog(material, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        paths = dialog.source_paths()
        self._set_material_copying(True)
        self.material_status_label.setText(
            f"正在向“{material.get('name', '')}”追加素材……"
        )
        self.material_copy_operation = {
            "kind": "material_append",
            "name": material.get("name", ""),
            "material_id": material.get("id"),
            "sources": list(paths),
        }
        thread = MaterialCopyThread(
            self.store,
            material.get("name", ""),
            paths,
            self,
            material_id=material.get("id"),
        )
        self.material_copy_thread = thread
        thread.completed.connect(self._material_copy_completed)
        thread.failed.connect(self._material_copy_failed)
        thread.progress.connect(self._material_copy_progress)
        thread.finished.connect(self._material_copy_finished)
        thread.start()

    def show_material_context_menu(self, position):
        index = self.material_table.indexAt(position)
        if not index.isValid():
            return
        self.material_table.selectRow(index.row())
        menu = QtWidgets.QMenu(self.material_table)
        append_action = menu.addAction("追加素材…")
        open_action = menu.addAction("打开素材目录")
        copy_sources_action = menu.addAction("复制来源")
        menu.addSeparator()
        remove_action = menu.addAction("移除记录")
        selected = menu.exec_(self.material_table.viewport().mapToGlobal(position))
        if selected == append_action:
            self.append_material()
        elif selected == open_action:
            self.open_selected_material()
        elif selected == copy_sources_action:
            self.copy_selected_material_sources()
        elif selected == remove_action:
            self.remove_material_record()

    def _selected_person(self, show_message=True):
        row = self.people_table.currentRow()
        if row < 0:
            if show_message:
                QtWidgets.QMessageBox.information(
                    self,
                    "请选择人物",
                    "请先选择一行人物。",
                )
            return None
        cell = self.people_table.item(row, 0)
        data = cell.data(QtCore.Qt.UserRole) if cell else None
        return data if isinstance(data, dict) else None

    def refresh_people(self, selected_id=None):
        if selected_id is None:
            selected = self._selected_person(show_message=False)
            selected_id = selected.get("id") if selected else None
        try:
            people = self.store.list_people()
        except (OSError, ValueError) as error:
            self._handle_person_error(error)
            return

        self.people_table.setRowCount(len(people))
        selected_row = -1
        default_icon = self.style().standardIcon(QtWidgets.QStyle.SP_DirHomeIcon)
        for row, person in enumerate(people):
            material_path = person.get("material_path") or person.get("path")
            stats = material_directory_stats(material_path)
            links = person.get("bindings", {}).get("google_sheets", [])
            updated_at = str(person.get("updated_at", "")).replace("T", " ")[:16]
            file_status = stats["file_count"] if stats["file_count"] else "暂无素材"
            values = [
                person.get("name", ""),
                f"{len(links)} 个",
                person.get("source_summary") or "尚未导入",
                file_status,
                _format_file_size(stats["size_bytes"]),
                updated_at,
                material_path,
            ]
            warning_color = (
                QtGui.QColor("#FFD6D6")
                if not Path(str(person.get("path") or "")).is_dir()
                else None
            )
            for column, value in enumerate(values):
                cell = QtWidgets.QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(QtCore.Qt.UserRole, person)
                    avatar_path = Path(str(person.get("avatar_path") or ""))
                    avatar_icon = QtGui.QIcon(str(avatar_path)) if avatar_path.is_file() else default_icon
                    cell.setIcon(avatar_icon)
                elif column == 1 and links:
                    cell.setToolTip("\n".join(map(str, links)))
                elif column == 2:
                    sources = []
                    for source in person.get("sources", []):
                        if source.get("type") == "material_library":
                            description = f"素材库：{source.get('label') or '未命名素材'}"
                            if source.get("removed_original"):
                                description += "（原素材已移除）"
                            sources.append(description)
                        elif source.get("value"):
                            sources.append(str(source.get("value")))
                    if sources:
                        cell.setToolTip("\n".join(sources))
                if warning_color is not None:
                    cell.setBackground(warning_color)
                self.people_table.setItem(row, column, cell)
            self.people_table.setRowHeight(row, 64)
            if person.get("id") == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.people_table.selectRow(selected_row)

    def add_person(self):
        if self.material_copy_thread is not None:
            return
        dialog = PersonProfileDialog(parent=self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        profile = dialog.values()
        self._set_material_copying(True)
        self.person_status_label.setText(
            f"正在创建人物“{profile.get('name', '')}”……"
        )
        self.material_copy_operation = {
            "kind": "person_add",
            "name": profile.get("name", ""),
        }
        thread = MaterialCopyThread(
            self.store,
            profile.get("name", ""),
            profile.get("source_paths", []),
            self,
            operation="person_add",
            profile=profile,
        )
        self.material_copy_thread = thread
        thread.completed.connect(self._material_copy_completed)
        thread.failed.connect(self._material_copy_failed)
        thread.progress.connect(self._material_copy_progress)
        thread.finished.connect(self._material_copy_finished)
        thread.start()

    def edit_person(self):
        person = self._selected_person()
        if not person or self.material_copy_thread is not None:
            return
        dialog = PersonProfileDialog(person, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        values = dialog.values()
        try:
            updated = self.store.update_person_profile(
                person.get("id"),
                values.get("name"),
                google_sheet_links=values.get("google_sheet_links", []),
                avatar_path=values.get("avatar_path", ""),
                remove_avatar=values.get("remove_avatar", False),
            )
        except (KeyError, OSError, ValueError) as error:
            self._handle_person_error(error)
            return
        self.refresh_people(selected_id=updated.get("id"))
        self.person_status_label.setText(
            f"已更新人物“{updated.get('name', '')}”的资料。"
        )
        self.log.emit(f"已更新人物资料：{updated.get('name', '')}")
        self.changed.emit()

    def append_person_material(self):
        person = self._selected_person()
        if not person or self.material_copy_thread is not None:
            return
        dialog = AppendMaterialDialog(person, self)
        dialog.setWindowTitle("追加人物素材")
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        paths = dialog.source_paths()
        self._set_material_copying(True)
        self.person_status_label.setText(
            f"正在向人物“{person.get('name', '')}”追加素材……"
        )
        self.material_copy_operation = {
            "kind": "person_append",
            "name": person.get("name", ""),
            "person_id": person.get("id"),
        }
        thread = MaterialCopyThread(
            self.store,
            person.get("name", ""),
            paths,
            self,
            material_id=person.get("id"),
            operation="person_append",
        )
        self.material_copy_thread = thread
        thread.completed.connect(self._material_copy_completed)
        thread.failed.connect(self._material_copy_failed)
        thread.progress.connect(self._material_copy_progress)
        thread.finished.connect(self._material_copy_finished)
        thread.start()

    def import_person_materials(self):
        person = self._selected_person()
        if not person or self.material_copy_thread is not None:
            return
        try:
            materials = self.store.list_materials()
        except (OSError, ValueError) as error:
            self._handle_material_error(error)
            return
        if not materials:
            QtWidgets.QMessageBox.information(
                self,
                "素材管理为空",
                "素材管理中还没有可导入的素材。",
            )
            return
        dialog = ImportMaterialsToPersonDialog(person, materials, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        material_ids = dialog.selected_material_ids()
        remove_originals = dialog.remove_originals()
        self._set_material_copying(True)
        self.person_status_label.setText(
            f"正在从素材管理向人物“{person.get('name', '')}”导入素材……"
        )
        self.material_copy_operation = {
            "kind": "person_import",
            "name": person.get("name", ""),
            "person_id": person.get("id"),
            "material_ids": list(material_ids),
            "remove_originals": remove_originals,
        }
        thread = MaterialCopyThread(
            self.store,
            person.get("name", ""),
            [],
            self,
            material_id=person.get("id"),
            operation="person_import",
            profile={
                "material_ids": material_ids,
                "remove_originals": remove_originals,
            },
        )
        self.material_copy_thread = thread
        thread.completed.connect(self._material_copy_completed)
        thread.failed.connect(self._material_copy_failed)
        thread.progress.connect(self._material_copy_progress)
        thread.finished.connect(self._material_copy_finished)
        thread.start()

    def open_selected_person_material(self, _index=None):
        person = self._selected_person()
        if not person:
            return
        person_dir = Path(str(person.get("path") or ""))
        if not person_dir.is_dir():
            QtWidgets.QMessageBox.warning(
                self,
                "人物素材不存在",
                "人物目录已经不存在，请点击“查库”清理记录。",
            )
            return
        material_path = Path(
            str(person.get("material_path") or person_dir / "素材")
        )
        try:
            material_path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._handle_person_error(error)
            return
        if not QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(material_path.resolve()))
        ):
            QtWidgets.QMessageBox.warning(
                self,
                "打开失败",
                f"无法打开：\n{material_path}",
            )

    @staticmethod
    def _person_sheet_links(person):
        bindings = person.get("bindings", {}) if isinstance(person, dict) else {}
        return [
            str(link).strip()
            for link in bindings.get("google_sheets", [])
            if str(link).strip()
        ]

    def _open_person_sheet_link(self, link):
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(str(link))):
            QtWidgets.QMessageBox.warning(self, "打开表格失败", f"无法打开：\n{link}")

    def show_person_sheet_menu(self, _checked=False):
        person = self._selected_person()
        if not person:
            return
        links = self._person_sheet_links(person)
        if not links:
            QtWidgets.QMessageBox.information(
                self,
                "没有绑定表格",
                "这个人物还没有绑定 Google 表格，请点击“编辑资料”添加。",
            )
            return
        if len(links) == 1:
            self._open_person_sheet_link(links[0])
            return
        menu = QtWidgets.QMenu(self.person_open_sheet_btn)
        actions = {}
        for index, link in enumerate(links, start=1):
            action = menu.addAction(f"表格 {index}")
            action.setToolTip(link)
            actions[action] = link
        selected = menu.exec_(
            self.person_open_sheet_btn.mapToGlobal(
                QtCore.QPoint(0, self.person_open_sheet_btn.height())
            )
        )
        if selected in actions:
            self._open_person_sheet_link(actions[selected])

    def copy_person_sheet_links(self):
        person = self._selected_person()
        if not person:
            return
        links = self._person_sheet_links(person)
        if not links:
            QtWidgets.QMessageBox.information(
                self,
                "没有绑定表格",
                "这个人物还没有绑定 Google 表格。",
            )
            return
        set_internal_clipboard_text("\n".join(links))
        self.person_status_label.setText(f"已复制 {len(links)} 个表格链接。")

    def check_people(self):
        if self.material_copy_thread is not None:
            return
        try:
            result = self.store.check_people()
            self.refresh_people()
        except (OSError, ValueError) as error:
            self._handle_person_error(error)
            return
        removed = result.get("removed", [])
        cleared = result.get("cleared_avatars", [])
        if removed or cleared:
            details = []
            if removed:
                details.append(f"移除 {len(removed)} 条目录失效记录")
            if cleared:
                details.append(f"清除 {len(cleared)} 个失效人物图片")
            QtWidgets.QMessageBox.information(
                self,
                "人物查库完成",
                "；".join(details) + "。磁盘上的其他文件没有被删除。",
            )
            self.changed.emit()
        else:
            QtWidgets.QMessageBox.information(
                self,
                "人物查库完成",
                f"已检查 {len(result.get('kept', []))} 个人物，记录正常。",
            )

    def remove_person_record(self):
        person = self._selected_person()
        if not person:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "移除人物记录",
            f"从管理列表移除“{person.get('name', '')}”吗？\n"
            "人物图片、绑定素材和人物目录都会保留，不会从磁盘删除。",
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        try:
            self.store.remove_person_record(person.get("id"))
            self.refresh_people()
            self.changed.emit()
        except (KeyError, OSError, ValueError) as error:
            self._handle_person_error(error)

    def show_person_context_menu(self, position):
        index = self.people_table.indexAt(position)
        if not index.isValid():
            return
        self.people_table.selectRow(index.row())
        person = self._selected_person(show_message=False)
        menu = QtWidgets.QMenu(self.people_table)
        edit_action = menu.addAction("编辑人物资料…")
        append_action = menu.addAction("追加人物素材…")
        import_action = menu.addAction("从素材管理导入…")
        open_material_action = menu.addAction("打开人物素材目录")
        sheet_menu = menu.addMenu("打开绑定表格")
        sheet_actions = {}
        for sheet_index, link in enumerate(self._person_sheet_links(person), start=1):
            action = sheet_menu.addAction(f"表格 {sheet_index}")
            action.setToolTip(link)
            sheet_actions[action] = link
        if not sheet_actions:
            sheet_menu.setEnabled(False)
        copy_sheets_action = menu.addAction("复制表格链接")
        menu.addSeparator()
        remove_action = menu.addAction("移除人物记录")
        selected = menu.exec_(self.people_table.viewport().mapToGlobal(position))
        if selected == edit_action:
            self.edit_person()
        elif selected == append_action:
            self.append_person_material()
        elif selected == import_action:
            self.import_person_materials()
        elif selected == open_material_action:
            self.open_selected_person_material()
        elif selected in sheet_actions:
            self._open_person_sheet_link(sheet_actions[selected])
        elif selected == copy_sheets_action:
            self.copy_person_sheet_links()
        elif selected == remove_action:
            self.remove_person_record()

    def _selected_material(self, show_message=True):
        row = self.material_table.currentRow()
        if row < 0:
            if show_message:
                QtWidgets.QMessageBox.information(
                    self,
                    "请选择素材",
                    "请先选择一行素材。",
                )
            return None
        cell = self.material_table.item(row, 0)
        data = cell.data(QtCore.Qt.UserRole) if cell else None
        return data if isinstance(data, dict) else None

    def refresh_materials(self, selected_id=None):
        if selected_id is None:
            selected = self._selected_material(show_message=False)
            selected_id = selected.get("id") if selected else None
        try:
            materials = self.store.list_materials()
        except (OSError, ValueError) as error:
            self._handle_material_error(error)
            return

        self.material_table.setRowCount(len(materials))
        selected_row = -1
        for row, material in enumerate(materials):
            stats = material_directory_stats(material["path"])
            created_at = str(material.get("created_at", "")).replace("T", " ")[:16]
            values = [
                material["name"],
                stats["status"],
                material.get("source_summary", f"{material.get('source_count', 0)} 项"),
                stats["file_count"],
                _format_file_size(stats["size_bytes"]),
                created_at,
                material["path"],
            ]
            warning_color = (
                QtGui.QColor("#FFD6D6")
                if stats["status"] != "正常"
                else None
            )
            for column, value in enumerate(values):
                cell = QtWidgets.QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(QtCore.Qt.UserRole, material)
                if column == 2:
                    source_lines = [
                        str(source.get("value", ""))
                        for source in material.get("sources", [])
                        if source.get("value")
                    ]
                    if source_lines:
                        cell.setToolTip("\n".join(source_lines))
                if warning_color is not None:
                    cell.setBackground(warning_color)
                self.material_table.setItem(row, column, cell)
            if material["id"] == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.material_table.selectRow(selected_row)

    def check_materials(self):
        if self.material_copy_thread is not None:
            return
        try:
            result = self.store.check_materials()
            self.refresh_materials()
        except (OSError, ValueError) as error:
            self._handle_material_error(error)
            return

        removed = result["removed"]
        if removed:
            details = "\n".join(
                f"• {item['name']}：{item['check_reason']}"
                for item in removed
            )
            QtWidgets.QMessageBox.information(
                self,
                "查库完成",
                f"已从素材管理中移除 {len(removed)} 条失效记录：\n{details}\n\n"
                "仅移除管理记录，没有删除磁盘文件。",
            )
            self.changed.emit()
        else:
            QtWidgets.QMessageBox.information(
                self,
                "查库完成",
                f"已检查 {len(result['kept'])} 条素材，保存位置均存在且不为空。",
            )

    def open_selected_material(self, _index=None):
        material = self._selected_material()
        if not material:
            return
        path = Path(material["path"])
        if not path.exists():
            QtWidgets.QMessageBox.warning(
                self,
                "素材不存在",
                "保存位置已经不存在，请点击“查库”清理记录。",
            )
            return
        if not QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(path.resolve()))
        ):
            QtWidgets.QMessageBox.warning(self, "打开失败", f"无法打开：\n{path}")

    def open_material_library(self):
        try:
            self.store.material_root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._handle_material_error(error)
            return
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(self.store.material_root.resolve()))
        )

    def copy_selected_material_sources(self):
        material = self._selected_material()
        if not material:
            return
        sources = [
            str(source.get("value", "")).strip()
            for source in material.get("sources", [])
            if str(source.get("value", "")).strip()
        ]
        if not sources:
            QtWidgets.QMessageBox.information(
                self,
                "复制来源",
                "这是一条旧素材记录，没有保存来源信息。",
            )
            return
        set_internal_clipboard_text("\n".join(sources))
        self.material_status_label.setText(f"已复制 {len(sources)} 条素材来源。")

    def remove_material_record(self):
        material = self._selected_material()
        if not material:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "移除素材记录",
            f"从管理列表移除“{material['name']}”吗？\n"
            "素材库中的文件会保留，不会被删除。",
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        try:
            self.store.remove_material_record(material["id"])
            self.refresh_materials()
            self.changed.emit()
        except (KeyError, OSError, ValueError) as error:
            self._handle_material_error(error)

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

    def reject(self):
        self.hide()
