import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtTest, QtWidgets

from PYUI.main_pyui import MainDialog
from PYUI.main_setting_pyui import MainSettingDialog
from model.UiInterval import IntervalPrompt


class FakeHotkeyManager(QtCore.QObject):
    activated = QtCore.pyqtSignal()

    def __init__(self, parent=None, hotkey_id=0):
        super().__init__(parent)
        self.sequence = None
        self.last_error = ""

    def register(self, shortcut):
        self.sequence = shortcut
        return True

    def close(self):
        self.sequence = None


class MainPluginIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_inventory_is_reached_from_plugin_menu_and_settings_page(self):
        with patch("PYUI.main_pyui.GlobalHotkeyManager", FakeHotkeyManager), patch(
            "app_plugins.builtin.inventory.GlobalHotkeyManager",
            FakeHotkeyManager,
        ), patch(
            "app_plugins.builtin.chrome_launcher.GlobalHotkeyManager",
            FakeHotkeyManager,
        ), patch(
            "app_plugins.builtin.chrome_launcher.ChromeRunnerDialog.load_profiles",
            lambda self: None,
        ), patch.object(MainDialog, "setupNotificationTray", lambda self: None), patch.object(
            MainDialog,
            "saveCurrentConfig",
            lambda self: True,
        ):
            window = MainDialog()
            try:
                menu_titles = [action.text() for action in window.main_menu_bar.actions()]
                tool_titles = [action.text() for action in window.tools_menu.actions()]
                plugin_titles = [action.text() for action in window.plugin_host.menu.actions()]
                self.assertIn("插件", menu_titles)
                self.assertIn("任务提交表自查", tool_titles)
                self.assertIn("批量切分音频…", tool_titles)
                self.assertIn("库存与素材管理器", plugin_titles)
                self.assertIn("监听剪贴板中的 Google 链接", plugin_titles)
                self.assertIn("Chrome 启动器", plugin_titles)
                self.assertIn("启动下一个 Chrome", plugin_titles)
                self.assertFalse(hasattr(window, "inventory_manager_btn"))
                self.assertFalse(hasattr(window, "split_audio_btn"))
                self.assertFalse(hasattr(window, "split_len_sbox"))
                self.assertIsNotNone(window.chrome_plugin.dialog)
                self.assertIsNotNone(window.audio_splitter_plugin.dialog)

                task_menu = QtWidgets.QMenu(window)
                task_actions = window.plugin_host.populate_task_context_menu(
                    task_menu,
                    [0],
                )
                self.assertIn(
                    "切分任务音频…",
                    [action.text() for action in task_actions],
                )

                with patch.object(window, "_showAuxNotice") as notice:
                    for _index in range(10):
                        QtTest.QTest.mouseClick(
                            window.about_btn,
                            QtCore.Qt.RightButton,
                        )
                    self.app.processEvents()
                    notice.assert_called_once_with()

                prompt = IntervalPrompt(window, seconds=1)
                with patch.object(QtWidgets.QMessageBox, "information") as information:
                    prompt._start()
                    QtTest.QTest.qWait(1050)
                    self.assertEqual(prompt.result(), QtWidgets.QDialog.Accepted)
                    information.assert_called_once()

                settings = MainSettingDialog(window, plugin_host=window.plugin_host)
                try:
                    tab_titles = [
                        settings.settingTabWidget.tabText(index)
                        for index in range(settings.settingTabWidget.count())
                    ]
                    self.assertIn("库存插件", tab_titles)
                    self.assertIn("Chrome 插件", tab_titles)
                    self.assertIn("切分音频插件", tab_titles)
                    self.assertNotIn("切分音频设置", tab_titles)
                    self.assertFalse(hasattr(settings, "inventory_manager_hotkey_edit"))
                    self.assertTrue(settings.chrome_hotkey_edit.isHidden())
                finally:
                    settings.deleteLater()
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
