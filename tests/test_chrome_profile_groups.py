import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QListWidgetItem

from PYUI.chrome_runner_pyui import ChromeRunnerDialog


class ChromeProfileGroupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def create_dialog(self, config_path):
        with patch.object(ChromeRunnerDialog, "load_profiles", lambda _self: None):
            dialog = ChromeRunnerDialog(config_path=str(config_path))
        dialog.chrome_path = "chrome.exe"
        dialog.profiles = [
            {"name": "Alpha", "directory": "Profile 1", "path": "A"},
            {"name": "Beta", "directory": "Profile 2", "path": "B"},
            {"name": "Gamma", "directory": "Profile 3", "path": "C"},
        ]
        dialog.chrome_list_widget.clear()
        for profile in dialog.profiles:
            item = QListWidgetItem(profile["name"])
            item.setData(Qt.UserRole, profile["directory"])
            dialog.chrome_list_widget.addItem(item)
        dialog.restore_iterator_position()
        dialog.update_iterator_status()
        return dialog

    def test_group_iterator_uses_visible_order_and_restores_next_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            dialog = self.create_dialog(config_path)
            dialog.profile_groups = [{
                "id": "group-a",
                "name": "工作组",
                # Deliberately different from the visible profile order.
                "profile_directories": ["Profile 3", "Profile 1"],
            }]
            dialog.selected_group_id = "group-a"
            self.assertTrue(dialog.save_profile_groups())
            dialog.populate_profile_group_combo()
            dialog.launch_profile = MagicMock(return_value=(True, None))

            first = dialog.start_group_iterator()

            self.assertEqual(first["directory"], "Profile 1")
            self.assertEqual(
                dialog.iterator_profile_directories,
                ["Profile 1", "Profile 3"],
            )
            dialog.launch_profile.assert_called_once()

            restored = self.create_dialog(config_path)
            restored.launch_profile = MagicMock(return_value=(True, None))
            second = restored.launch_next_profile()

            self.assertEqual(second["directory"], "Profile 3")
            self.assertEqual(restored.active_iterator_group_id, "group-a")
            self.assertEqual(restored.selected_group_id, "group-a")
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["chrome_profile_groups"]["groups"][0]["name"],
                "工作组",
            )

    def test_manual_iterator_clears_group_source_without_deleting_group(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            dialog = self.create_dialog(config_path)
            dialog.profile_groups = [{
                "id": "group-a",
                "name": "工作组",
                "profile_directories": ["Profile 1", "Profile 3"],
                "future_metadata": {"color": "blue"},
            }]
            dialog.selected_group_id = "group-a"
            dialog.active_iterator_group_id = "group-a"
            dialog.iterator_profile_directories = ["Profile 1", "Profile 3"]
            dialog.populate_profile_group_combo()
            dialog.chrome_list_widget.clearSelection()
            dialog.chrome_list_widget.item(1).setSelected(True)
            dialog.launch_profile = MagicMock(return_value=(True, None))

            launched = dialog.start_selected_iterator()

            self.assertEqual(launched["directory"], "Profile 2")
            self.assertIsNone(dialog.active_iterator_group_id)
            self.assertEqual(dialog.profile_groups[0]["future_metadata"], {"color": "blue"})

    def test_each_group_keeps_its_own_next_profile_across_switch_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            dialog = self.create_dialog(config_path)
            dialog.profile_groups = [
                {
                    "id": "group-a",
                    "name": "A 组",
                    "profile_directories": ["Profile 1", "Profile 3"],
                    "next_profile_directory": "Profile 1",
                },
                {
                    "id": "group-b",
                    "name": "B 组",
                    "profile_directories": ["Profile 2", "Profile 3"],
                    "next_profile_directory": "Profile 2",
                },
            ]
            dialog.launch_profile = MagicMock(return_value=(True, None))

            dialog.selected_group_id = "group-a"
            self.assertEqual(
                dialog.start_group_iterator()["directory"], "Profile 1"
            )
            dialog.selected_group_id = "group-b"
            self.assertEqual(
                dialog.start_group_iterator()["directory"], "Profile 2"
            )
            dialog.selected_group_id = "group-a"
            self.assertEqual(
                dialog.start_group_iterator()["directory"], "Profile 3"
            )

            restored = self.create_dialog(config_path)
            restored.launch_profile = MagicMock(return_value=(True, None))
            restored.selected_group_id = "group-b"
            self.assertEqual(
                restored.start_group_iterator()["directory"], "Profile 3"
            )
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            groups = {
                group["id"]: group
                for group in saved["chrome_profile_groups"]["groups"]
            }
            self.assertEqual(
                groups["group-a"]["next_profile_directory"], "Profile 1"
            )
            self.assertEqual(
                groups["group-b"]["next_profile_directory"], "Profile 2"
            )
            self.assertEqual(saved["chrome_profile_groups"]["version"], 2)

    def test_member_update_preserves_or_repairs_saved_group_position(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            dialog = self.create_dialog(config_path)
            group = {
                "id": "group-a",
                "name": "可编辑组",
                "profile_directories": ["Profile 1", "Profile 3"],
                "next_profile_directory": "Profile 3",
            }
            dialog.profile_groups = [group]
            dialog.selected_group_id = "group-a"

            self.assertTrue(dialog.update_profile_group_members(
                group, ["Profile 2", "Profile 3"]
            ))
            self.assertEqual(
                group["profile_directories"], ["Profile 2", "Profile 3"]
            )
            self.assertEqual(group["next_profile_directory"], "Profile 3")

            self.assertTrue(dialog.update_profile_group_members(
                group, ["Profile 2"]
            ))
            self.assertEqual(group["next_profile_directory"], "Profile 2")
            self.assertEqual(
                dialog.save_profile_group_members_btn.text(), "管理分组成员…"
            )


if __name__ == "__main__":
    unittest.main()
