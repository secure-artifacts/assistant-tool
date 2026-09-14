import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets

from app_plugins.api import (
    MAIN_MENU,
    TASK_CONTEXT_MENU,
    TOOLS_MENU,
    PluginCommand,
    PluginSettingsPage,
)
from app_plugins.builtin.audio_splitter import (
    AUDIO_SPLITTER_CONFIG_KEY,
    AudioSplitterPlugin,
    AudioSplitterSettingsPage,
)
from app_plugins.builtin.chrome_launcher import (
    ChromeLauncherPlugin,
    ChromeLauncherSettingsPage,
)
from app_plugins.builtin.inventory import InventoryPlugin, InventorySettingsPage
from app_plugins.host import PluginHost
from model.GlobalHotkey import (
    CHROME_NEXT_HOTKEY_CONFIG_KEY,
    INVENTORY_MANAGER_HOTKEY_CONFIG_KEY,
)


class FakeMainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.logs = []
        self.config = {}

    def appendLog(self, message, end="", level=None):
        self.logs.append(str(message))

    def load_config(self):
        return dict(self.config)

    def saveCurrentConfig(self):
        return True

    def showDesktopNotification(self, title, message, critical=False):
        pass

    def selectedTaskRows(self):
        return [2]

    def taskTargetsForRows(self, rows, require_loaded=True):
        return [{"row": row} for row in rows]


class DemoSettingsPage:
    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        self.loaded = None

    def load_config(self, config):
        self.loaded = dict(config)

    def update_config(self, config):
        config["demo_setting"] = 7


class DemoPlugin:
    plugin_id = "demo"
    required_api_version = 1

    def __init__(self):
        self.invocations = []
        self.monitor_enabled = False

    def register(self, context):
        context.register_command(
            PluginCommand(
                "open",
                "演示插件",
                lambda rows: self.invocations.append(("open", rows)),
                frozenset({MAIN_MENU}),
            )
        )
        context.register_command(
            PluginCommand(
                "monitor",
                "演示监听",
                self.toggle_monitor,
                frozenset({MAIN_MENU}),
                order=200,
                checkable=True,
                checked=lambda: self.monitor_enabled,
            )
        )
        context.register_command(
            PluginCommand(
                "task",
                "演示任务命令",
                lambda rows: self.invocations.append(("task", rows)),
                frozenset({TASK_CONTEXT_MENU}),
            )
        )
        context.register_settings_page(
            PluginSettingsPage("settings", "演示设置", DemoSettingsPage)
        )

    def toggle_monitor(self, rows, checked):
        self.monitor_enabled = checked
        self.invocations.append(("monitor", rows, checked))


class PluginHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.main = FakeMainWindow()
        self.host = PluginHost(self.main)

    def tearDown(self):
        self.main.deleteLater()

    def test_commands_register_in_main_and_task_menus(self):
        plugin = self.host.install(DemoPlugin())
        menu_bar = QtWidgets.QMenuBar(self.main)
        plugin_menu = self.host.attach_main_menu(menu_bar)

        self.assertEqual(plugin_menu.title(), "插件")
        self.assertEqual(plugin_menu.actions()[0].text(), "演示插件")
        self.assertEqual(plugin_menu.actions()[1].text(), "演示监听")
        plugin_menu.actions()[0].trigger()
        plugin_menu.actions()[1].trigger()

        task_menu = QtWidgets.QMenu(self.main)
        actions = self.host.populate_task_context_menu(task_menu, [1, 4])
        self.assertEqual(actions[0].text(), "演示任务命令")
        actions[0].trigger()
        self.assertEqual(
            plugin.invocations,
            [("open", ()), ("monitor", (), True), ("task", (1, 4))],
        )

    def test_settings_page_loads_and_updates_config(self):
        self.host.install(DemoPlugin())
        dialog = QtWidgets.QDialog(self.main)
        tabs = QtWidgets.QTabWidget(dialog)
        controllers = self.host.create_settings_pages(dialog, tabs)
        self.host.load_settings_pages({"source": "loaded"})
        config = {}
        self.host.update_settings_config(config)

        self.assertEqual(tabs.tabText(0), "演示设置")
        self.assertEqual(controllers[0][2].loaded["source"], "loaded")
        self.assertEqual(config["demo_setting"], 7)

    def test_rejects_plugins_requiring_newer_api(self):
        plugin = DemoPlugin()
        plugin.required_api_version = self.host.api_version + 1
        with self.assertRaises(RuntimeError):
            self.host.install(plugin)

    def test_inventory_plugin_registers_all_supported_entry_points(self):
        self.host.install(InventoryPlugin())
        menu_bar = QtWidgets.QMenuBar(self.main)
        plugin_menu = self.host.attach_main_menu(menu_bar)
        task_menu = QtWidgets.QMenu(self.main)
        task_actions = self.host.populate_task_context_menu(task_menu, [0])
        dialog = QtWidgets.QDialog(self.main)
        tabs = QtWidgets.QTabWidget(dialog)
        self.host.create_settings_pages(dialog, tabs)

        self.assertEqual(plugin_menu.actions()[0].text(), "库存与素材管理器")
        self.assertEqual(task_actions[0].text(), "分配库存图片…")
        self.assertEqual(tabs.tabText(0), "库存插件")

    def test_chrome_plugin_registers_launcher_next_command_and_settings(self):
        self.host.install(ChromeLauncherPlugin())
        menu_bar = QtWidgets.QMenuBar(self.main)
        plugin_menu = self.host.attach_main_menu(menu_bar)
        dialog = QtWidgets.QDialog(self.main)
        tabs = QtWidgets.QTabWidget(dialog)
        self.host.create_settings_pages(dialog, tabs)

        self.assertEqual(
            [action.text() for action in plugin_menu.actions()],
            ["Chrome 启动器", "启动下一个 Chrome"],
        )
        self.assertEqual(tabs.tabText(0), "Chrome 插件")

    def test_audio_splitter_registers_tools_task_and_settings_entries(self):
        self.host.install(AudioSplitterPlugin())
        tools_menu = QtWidgets.QMenu("工具", self.main)
        core_action = tools_menu.addAction("宿主工具")
        self.host.attach_tools_menu(tools_menu)
        task_menu = QtWidgets.QMenu(self.main)
        task_actions = self.host.populate_task_context_menu(task_menu, [1, 3])
        dialog = QtWidgets.QDialog(self.main)
        tabs = QtWidgets.QTabWidget(dialog)
        self.host.create_settings_pages(dialog, tabs)

        self.assertIs(tools_menu.actions()[0], core_action)
        self.assertEqual(tools_menu.actions()[-1].text(), "批量切分音频…")
        self.assertEqual(task_actions[0].text(), "切分任务音频…")
        self.assertEqual(tabs.tabText(0), "切分音频插件")


class InventorySettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_preserves_legacy_hotkey_config_key(self):
        page = InventorySettingsPage()
        page.load_config({
            INVENTORY_MANAGER_HOTKEY_CONFIG_KEY: "Ctrl+Shift+I",
            "clipboard_google_drive_monitor_enabled": True,
        })
        config = {}
        page.update_config(config)
        self.assertEqual(
            config[INVENTORY_MANAGER_HOTKEY_CONFIG_KEY],
            "Ctrl+Shift+I",
        )
        self.assertTrue(config["clipboard_google_drive_monitor_enabled"])


class ChromeLauncherSettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_reuses_hotkey_key_without_touching_existing_chrome_data(self):
        original = {
            "chrome_preset_websites": ["https://example.test"],
            "chrome_profile_groups": {
                "version": 2,
                "groups": [{"id": "a", "name": "工作", "profile_directories": ["Profile 1"]}],
            },
            "chrome_profile_iterator": {
                "profile_directories": ["Profile 1"],
                "next_profile_directory": "Profile 1",
            },
        }
        config = dict(original)
        page = ChromeLauncherSettingsPage()
        page.load_config({CHROME_NEXT_HOTKEY_CONFIG_KEY: "Ctrl+Shift+N"})
        page.update_config(config)

        self.assertEqual(config[CHROME_NEXT_HOTKEY_CONFIG_KEY], "Ctrl+Shift+N")
        for key, value in original.items():
            self.assertEqual(config[key], value)


class AudioSplitterSettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_round_trips_all_split_parameters_in_plugin_config(self):
        page = AudioSplitterSettingsPage()
        page.load_config({
            AUDIO_SPLITTER_CONFIG_KEY: {
                "max_length_seconds": 45,
                "tolerance_seconds": 75,
                "min_silence_ms": 850,
                "silence_offset_db": 19.5,
                "extensions": [".wav", ".mp3"],
                "recursive": False,
                "include_source_name": True,
            }
        })
        config = {}
        page.update_config(config)

        self.assertEqual(config[AUDIO_SPLITTER_CONFIG_KEY], {
            "max_length_seconds": 45,
            "tolerance_seconds": 75,
            "min_silence_ms": 850,
            "silence_offset_db": 19.5,
            "extensions": [".wav", ".mp3"],
            "recursive": False,
            "include_source_name": True,
        })


if __name__ == "__main__":
    unittest.main()
