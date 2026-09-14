import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets

from app_plugins.builtin.inventory import InventoryPlugin
from model.ClipboardHelper import set_internal_clipboard_text
from PYUI.utility_managers_pyui import (
    InventoryManagerDialog,
    MaterialCopyThread,
    MaterialDropEdit,
    MaterialGroupAssignmentDialog,
)
from model.InventoryManager import InventoryStore


class MaterialGroupAssignmentDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_selected_entries_distribute_one_image_per_task_and_report_shortage(self):
        groups = [
            {
                "source_kind": "material",
                "source_id": "m1",
                "source_type_label": "素材管理",
                "name": "产品图",
                "path": "C:/materials/product",
                "images": [
                    {"path": "C:/materials/product/one.png"},
                    {"path": "C:/materials/product/two.png"},
                ],
            },
            {
                "source_kind": "person",
                "source_id": "p1",
                "source_type_label": "人物素材",
                "name": "Alice",
                "path": "C:/people/Alice",
                "images": [{"path": "C:/people/Alice/portrait.jpg"}],
            },
        ]
        tasks = [
            {"label": "1 | 第一条", "target_dir": "C:/tasks/1"},
            {"label": "2 | 第二条", "target_dir": "C:/tasks/2"},
            {"label": "3 | 第三条", "target_dir": "C:/tasks/3"},
            {"label": "4 | 第四条", "target_dir": "C:/tasks/4"},
        ]
        dialog = MaterialGroupAssignmentDialog(groups, tasks)
        try:
            dialog.group_table.item(0, 0).setCheckState(QtCore.Qt.Checked)
            dialog.group_table.item(1, 0).setCheckState(QtCore.Qt.Checked)

            assignments = dialog.assignments()
            summary = dialog.distribution_summary()

            self.assertEqual(len(assignments), 3)
            self.assertEqual(assignments[0]["target_dir"], "C:/tasks/1")
            self.assertEqual(assignments[1]["target_dir"], "C:/tasks/2")
            self.assertEqual(assignments[2]["target_dir"], "C:/tasks/3")
            self.assertEqual(summary["last_assigned_task"], "3 | 第三条")
            self.assertEqual(summary["first_missing_task"], "4 | 第四条")
            self.assertEqual(summary["missing_count"], 1)
            self.assertEqual(
                dialog.distribution_label.text(),
                "可分配 3/4 个任务，还缺 1 张。",
            )
            self.assertIn(
                "从“4 | 第四条”开始缺图",
                dialog.distribution_label.toolTip(),
            )
            self.assertEqual(
                dialog.intro_label.text(),
                "已选 4 个任务。勾选素材条目后，每个任务分配 1 张。",
            )
            self.assertTrue(dialog.assign_button.isEnabled())
        finally:
            dialog.close()

    def test_filter_selects_matching_whole_entry_not_individual_images(self):
        groups = [
            {
                "source_kind": "material",
                "source_id": "m1",
                "source_type_label": "素材管理",
                "name": "天使",
                "path": "C:/angel",
                "images": [{"path": "C:/angel/a.png"}, {"path": "C:/angel/b.png"}],
            },
            {
                "source_kind": "person",
                "source_id": "p1",
                "source_type_label": "人物素材",
                "name": "产品人物",
                "path": "C:/product",
                "images": [{"path": "C:/product/a.jpg"}],
            },
        ]
        tasks = [{"label": "1", "target_dir": "C:/tasks/1"}]
        dialog = MaterialGroupAssignmentDialog(groups, tasks)
        try:
            dialog.apply_filter("天使")
            dialog.set_visible_checked(True)

            self.assertEqual(
                dialog.group_table.item(0, 0).checkState(),
                QtCore.Qt.Checked,
            )
            self.assertEqual(
                dialog.group_table.item(1, 0).checkState(),
                QtCore.Qt.Unchecked,
            )
            self.assertEqual(dialog.distribution_summary()["available_image_count"], 2)
        finally:
            dialog.close()


class ClipboardInventoryBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    @staticmethod
    def make_clipboard_plugin():
        class FakeContext:
            def __init__(self):
                self.logs = []

            def log(self, message, level=None):
                self.logs.append(str(message))

        plugin = InventoryPlugin()
        plugin.context = FakeContext()
        plugin.clipboard_monitor_enabled = True
        plugin.calls = []

        def open_manager(material_sources=None, source_label=""):
            plugin.calls.append((list(material_sources or []), source_label))
            return len(material_sources or [])

        plugin.open_manager = open_manager
        return plugin

    def test_inventory_dialog_accepts_links_and_queues_them_while_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            dialog = InventoryManagerDialog(store)
            try:
                added = dialog.prepare_material_sources([
                    "https://drive.google.com/file/d/file-1/view",
                    "https://drive.google.com/file/d/file-1/view",
                ])
                self.assertEqual(added, 1)
                self.assertEqual(len(dialog.material_sources_edit.paths()), 1)
                self.assertIs(dialog.tabs.currentWidget(), dialog.material_tab)

                dialog.material_copy_thread = object()
                queued = dialog.prepare_material_sources([
                    "https://drive.google.com/file/d/file-2/view"
                ])
                self.assertEqual(queued, 1)
                self.assertEqual(len(dialog.pending_material_sources), 1)
            finally:
                dialog.material_copy_thread = None
                dialog.close()

    def test_material_text_field_converts_rich_text_links_to_lines(self):
        mime_data = QtCore.QMimeData()
        mime_data.setText("第一个素材\n第二个素材")
        mime_data.setHtml(
            '<a href="https://drive.google.com/file/d/file-rich/view">第一个素材</a>'
            '<br><a href="https://docs.google.com/document/d/doc-rich/edit">第二个素材</a>'
        )
        edit = MaterialDropEdit()
        try:
            edit.insertFromMimeData(mime_data)
            self.assertEqual(
                edit.paths(),
                [
                    "https://drive.google.com/file/d/file-rich/view",
                    "https://docs.google.com/document/d/doc-rich/edit",
                ],
            )
        finally:
            edit.close()

    def test_material_auto_name_checkbox_allows_blank_name_and_reaches_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = InventoryStore(
                root / "inventory.json",
                material_root=root / "library",
            )
            dialog = InventoryManagerDialog(store)
            try:
                checkbox = dialog.material_use_drive_folder_name_checkbox
                checkbox.setChecked(True)
                dialog.material_sources_edit.setPlainText(
                    "https://drive.google.com/drive/folders/folder-456"
                )
                self.assertFalse(dialog.material_name_edit.isEnabled())
                self.assertIn("自动读取", dialog.material_name_edit.placeholderText())

                with patch.object(MaterialCopyThread, "start"):
                    dialog.add_material()

                self.assertIsNotNone(dialog.material_copy_thread)
                self.assertTrue(dialog.material_copy_thread.use_drive_folder_name)
                self.assertEqual(dialog.material_copy_thread.name, "")
            finally:
                thread = dialog.material_copy_thread
                dialog.material_copy_thread = None
                if thread is not None:
                    thread.deleteLater()
                dialog.close()

    def test_clipboard_google_links_open_inventory_once_per_clipboard_value(self):
        plugin = self.make_clipboard_plugin()
        clipboard = QtWidgets.QApplication.clipboard()
        clipboard.setText(
            "https://drive.google.com/file/d/file-1/view\n"
            "https://docs.google.com/document/d/doc-2/edit"
        )

        plugin.inspect_clipboard()
        plugin.inspect_clipboard()

        self.assertEqual(len(plugin.calls), 1)
        self.assertEqual(len(plugin.calls[0][0]), 2)
        self.assertEqual(plugin.calls[0][1], "剪贴板")

    def test_clipboard_monitor_reads_link_hidden_behind_rich_text(self):
        plugin = self.make_clipboard_plugin()
        mime_data = QtCore.QMimeData()
        mime_data.setText("客户素材")
        mime_data.setHtml(
            '<a href="https://drive.google.com/drive/folders/rich-folder">'
            "客户素材</a>"
        )
        QtWidgets.QApplication.clipboard().setMimeData(mime_data)

        plugin.inspect_clipboard()

        self.assertEqual(
            plugin.calls,
            [
                (
                    ["https://drive.google.com/drive/folders/rich-folder"],
                    "剪贴板",
                )
            ],
        )

    def test_clipboard_google_links_are_ignored_while_inventory_is_visible(self):
        class FakeInventoryDialog:
            visible = True

            def isVisible(self):
                return self.visible

        plugin = self.make_clipboard_plugin()
        plugin.dialog = FakeInventoryDialog()
        clipboard = QtWidgets.QApplication.clipboard()
        first_link = "https://drive.google.com/file/d/visible-window/view"
        clipboard.setText(first_link)

        plugin.inspect_clipboard()

        self.assertEqual(plugin.calls, [])
        self.assertEqual(plugin.context.logs, [])
        self.assertEqual(plugin._last_clipboard_text, first_link)

        # 隐藏后，同一段已忽略的剪贴板内容也不应补触发；只有新复制的链接才触发。
        plugin.dialog.visible = False
        plugin.inspect_clipboard()
        self.assertEqual(plugin.calls, [])

        clipboard.setText("https://drive.google.com/file/d/new-after-hide/view")
        plugin.inspect_clipboard()
        self.assertEqual(len(plugin.calls), 1)

    def test_link_copied_by_application_does_not_open_inventory(self):
        plugin = self.make_clipboard_plugin()
        link = "https://drive.google.com/file/d/internal-copy/view"
        set_internal_clipboard_text(link)

        plugin.inspect_clipboard()

        self.assertEqual(plugin.calls, [])
        self.assertIsNone(plugin._last_clipboard_text)

        # Re-copying the same text from an external program removes the
        # internal MIME marker and must still behave like a normal user copy.
        QtWidgets.QApplication.clipboard().setText(link)
        plugin.inspect_clipboard()
        self.assertEqual(len(plugin.calls), 1)


if __name__ == "__main__":
    unittest.main()
