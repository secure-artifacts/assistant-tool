from collections import defaultdict

from PyQt5 import QtCore, QtGui, QtWidgets

from model.ClipboardHelper import set_internal_clipboard_text
from model.ReviewSubmissionHistory import (
    acknowledge_review_items,
    review_history_snapshot,
)


STATUS_TEXT = {
    "pending": "等待审核",
    "passed": "可以使用",
    "needs_changes": "需要修改",
}


class ReviewStatusDialog(QtWidgets.QDialog):
    def __init__(self, history_path=None, parent=None):
        super().__init__(parent)
        self.history_path = history_path
        self.setWindowTitle("审核结果提醒")
        self.resize(980, 590)
        layout = QtWidgets.QVBoxLayout(self)
        self.summary_label = QtWidgets.QLabel(self)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.tabs = QtWidgets.QTabWidget(self)
        self.passed_tree = self._make_tree(
            ("管理员", "视频", "阶段", "状态"),
        )
        self.changes_tree = self._make_tree(
            ("管理员", "视频", "阶段", "严重程度", "修改建议"),
        )
        self.history_tree = self._make_tree(
            ("管理员", "视频", "阶段", "状态", "修改建议"),
        )
        self.tabs.addTab(self.passed_tree, "通过待发送")
        self.tabs.addTab(self.changes_tree, "需要修改")
        self.tabs.addTab(self.history_tree, "提交历史")
        layout.addWidget(self.tabs, 1)

        hint = QtWidgets.QLabel(
            "选中一条或多条后按 Ctrl+C 可直接复制 Google Drive 链接；"
            "双击可打开视频。标记只清除当前提醒，"
            "如果该视频以后进入返修或出现新审核结果，仍会再次提醒。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        layout.addWidget(hint)

        buttons = QtWidgets.QHBoxLayout()
        self.open_btn = QtWidgets.QPushButton("打开选中视频", self)
        self.copy_link_btn = QtWidgets.QPushButton("复制选中链接", self)
        self.copy_summary_btn = QtWidgets.QPushButton("复制当前页汇总", self)
        self.ack_btn = QtWidgets.QPushButton("标记已发送/已处理", self)
        self.refresh_btn = QtWidgets.QPushButton("立即检查审核表", self)
        close_btn = QtWidgets.QPushButton("关闭", self)
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.copy_link_btn)
        buttons.addWidget(self.copy_summary_btn)
        buttons.addWidget(self.ack_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.refresh_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self.open_btn.clicked.connect(self.open_selected)
        self.copy_link_btn.clicked.connect(self.copy_selected_links)
        self.copy_summary_btn.clicked.connect(self.copy_current)
        self.ack_btn.clicked.connect(self.acknowledge_selected)
        self.refresh_btn.clicked.connect(self.request_remote_refresh)
        close_btn.clicked.connect(self.accept)
        self.tabs.currentChanged.connect(self.update_buttons)
        self.copy_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence.Copy,
            self,
        )
        self.copy_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self.copy_shortcut.activated.connect(self.copy_selected_links)
        for tree in (self.passed_tree, self.changes_tree, self.history_tree):
            tree.itemDoubleClicked.connect(lambda _item, _column: self.open_selected())
            tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            tree.customContextMenuRequested.connect(
                lambda position, current_tree=tree: self.show_tree_context_menu(
                    current_tree,
                    position,
                )
            )
        self.refresh()

    def _make_tree(self, headers):
        tree = QtWidgets.QTreeWidget(self)
        tree.setColumnCount(len(headers))
        tree.setHeaderLabels(list(headers))
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        tree.setSortingEnabled(True)
        header = tree.header()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        if len(headers) > 1:
            header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        return tree

    @staticmethod
    def _fill(tree, items, fields):
        tree.setSortingEnabled(False)
        tree.clear()
        for data in items:
            values = []
            for field in fields:
                if field == "status":
                    values.append(STATUS_TEXT.get(data.get(field), str(data.get(field) or "")))
                else:
                    values.append(str(data.get(field) or ""))
            item = QtWidgets.QTreeWidgetItem(values)
            item.setData(0, QtCore.Qt.UserRole, data.get("key"))
            item.setData(0, QtCore.Qt.UserRole + 1, data.get("link"))
            if data.get("status") == "needs_changes":
                item.setBackground(0, QtGui.QColor("#FFD6D6"))
            elif data.get("status") == "passed":
                item.setBackground(0, QtGui.QColor("#DDF3E4"))
            tree.addTopLevelItem(item)
        tree.setSortingEnabled(True)

    def refresh(self):
        snapshot = review_history_snapshot(self.history_path)
        self._fill(
            self.passed_tree,
            snapshot["passed"],
            ("admin", "name", "phase", "status"),
        )
        self._fill(
            self.changes_tree,
            snapshot["needs_changes"],
            ("admin", "name", "phase", "severity", "note"),
        )
        self._fill(
            self.history_tree,
            snapshot["all"],
            ("admin", "name", "phase", "status", "note"),
        )
        self.tabs.setTabText(0, "通过待发送 ({})".format(snapshot["passed_count"]))
        self.tabs.setTabText(1, "需要修改 ({})".format(snapshot["needs_changes_count"]))
        self.tabs.setTabText(2, "提交历史 ({})".format(len(snapshot["all"])))
        self.summary_label.setText(
            "待发给管理员：{} 个；需要修改：{} 个；已保存提交历史：{} 个。".format(
                snapshot["passed_count"],
                snapshot["needs_changes_count"],
                len(snapshot["all"]),
            )
        )
        self.update_buttons()

    def request_remote_refresh(self):
        parent = self.parent()
        if parent is not None and hasattr(parent, "requestReviewStatusCheck"):
            parent.requestReviewStatusCheck()
        self.refresh_btn.setText("正在检查…")
        self.refresh_btn.setEnabled(False)
        QtCore.QTimer.singleShot(1500, self.refresh)
        QtCore.QTimer.singleShot(4000, self.finish_remote_refresh)

    def finish_remote_refresh(self):
        self.refresh()
        self.refresh_btn.setText("立即检查审核表")
        self.refresh_btn.setEnabled(True)

    def current_tree(self):
        return (
            self.passed_tree,
            self.changes_tree,
            self.history_tree,
        )[self.tabs.currentIndex()]

    def selected_items(self):
        tree = self.current_tree()
        selected = tree.selectedItems()
        if not selected and tree.currentItem() is not None:
            selected = [tree.currentItem()]
        return selected

    def update_buttons(self):
        history_tab = self.tabs.currentIndex() == 2
        self.ack_btn.setEnabled(not history_tab)
        self.ack_btn.setText(
            "标记已发送" if self.tabs.currentIndex() == 0 else "标记已处理"
        )

    def open_selected(self):
        items = self.selected_items()
        if not items:
            QtWidgets.QMessageBox.information(self, "审核提醒", "请先选中一个视频。")
            return
        link = str(items[0].data(0, QtCore.Qt.UserRole + 1) or "").strip()
        if link:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(link))

    def copy_selected_links(self):
        links = []
        for item in self.selected_items():
            link = str(
                item.data(0, QtCore.Qt.UserRole + 1) or ""
            ).strip()
            if link and link not in links:
                links.append(link)
        if not links:
            self.summary_label.setText("请先选中要复制的视频。")
            return 0
        set_internal_clipboard_text("\n".join(links))
        self.summary_label.setText(
            "已复制 {} 个 Google Drive 链接，可以直接粘贴发送。".format(
                len(links)
            )
        )
        return len(links)

    def show_tree_context_menu(self, tree, position):
        item = tree.itemAt(position)
        if item is None:
            return
        if not item.isSelected():
            tree.clearSelection()
            item.setSelected(True)
            tree.setCurrentItem(item)
        menu = QtWidgets.QMenu(tree)
        copy_action = menu.addAction("复制 Google Drive 链接")
        open_action = menu.addAction("打开视频")
        selected_action = menu.exec_(tree.viewport().mapToGlobal(position))
        if selected_action is copy_action:
            self.copy_selected_links()
        elif selected_action is open_action:
            self.open_selected()

    def copy_current(self):
        snapshot = review_history_snapshot(self.history_path)
        if self.tabs.currentIndex() == 0:
            items = snapshot["passed"]
            heading = "审核通过，待发送"
        elif self.tabs.currentIndex() == 1:
            items = snapshot["needs_changes"]
            heading = "需要修改"
        else:
            items = snapshot["all"]
            heading = "审核提交历史"
        if not items:
            QtWidgets.QMessageBox.information(self, "审核提醒", "当前页面没有可复制的记录。")
            return
        grouped = defaultdict(list)
        for item in items:
            grouped[item.get("admin") or "未填写管理员"].append(item)
        lines = [heading]
        for admin, admin_items in grouped.items():
            lines.append("\n{}：".format(admin))
            for item in admin_items:
                extra = ""
                if item.get("status") == "needs_changes":
                    extra = "（{}{}）".format(
                        item.get("severity") or "",
                        ("；" + item.get("note")) if item.get("note") else "",
                    )
                lines.append("- {}{} {}".format(item.get("name"), extra, item.get("link")))
        set_internal_clipboard_text("\n".join(lines))
        QtWidgets.QMessageBox.information(self, "审核提醒", "当前页汇总已复制。")

    def acknowledge_selected(self):
        items = self.selected_items()
        if not items:
            QtWidgets.QMessageBox.information(self, "审核提醒", "请先选中要处理的记录。")
            return
        keys = [item.data(0, QtCore.Qt.UserRole) for item in items]
        acknowledge_review_items(keys, self.history_path)
        self.refresh()
