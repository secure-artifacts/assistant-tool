import logging

from PyQt5 import QtGui, QtWidgets

from app_plugins.api import MAIN_MENU, PluginCommand, PluginSettingsPage
from model.GlobalHotkey import (
    CHROME_NEXT_HOTKEY_CONFIG_KEY,
    CHROME_NEXT_HOTKEY_ID,
    DEFAULT_CHROME_NEXT_HOTKEY,
    GlobalHotkeyManager,
    normalize_hotkey_sequence,
)
from PYUI.chrome_runner_pyui import ChromeRunnerDialog


class ChromeLauncherSettingsPage:
    """Settings owned by the Chrome launcher plugin.

    Profile groups, preset websites, and iterator cursors remain editable in
    ChromeRunnerDialog and deliberately retain their existing config keys.
    """

    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        title = QtWidgets.QLabel("Chrome 启动器", self.widget)
        title.setStyleSheet("font-weight:600;font-size:14px;")
        layout.addWidget(title)

        form = QtWidgets.QFormLayout()
        self.hotkey_edit = QtWidgets.QKeySequenceEdit(self.widget)
        form.addRow("启动下一个浏览器：", self.hotkey_edit)
        layout.addLayout(form)

        hint = QtWidgets.QLabel(
            "该快捷键在主程序运行时全局生效。Chrome Profile、预设网站、"
            "分组及各分组的迭代位置继续保存在原配置中，可从插件菜单打开"
            "Chrome 启动器维护。",
            self.widget,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)

    def load_config(self, config):
        value = config.get(
            CHROME_NEXT_HOTKEY_CONFIG_KEY,
            DEFAULT_CHROME_NEXT_HOTKEY,
        )
        try:
            value = normalize_hotkey_sequence(value)
        except ValueError:
            value = DEFAULT_CHROME_NEXT_HOTKEY
        self.hotkey_edit.setKeySequence(QtGui.QKeySequence(value))

    def normalized_hotkey(self):
        return normalize_hotkey_sequence(
            self.hotkey_edit.keySequence().toString(
                QtGui.QKeySequence.PortableText
            )
        )

    def hotkey_fields(self):
        return (("启动下一个浏览器", self.hotkey_edit, self.widget),)

    def validate(self):
        self.normalized_hotkey()

    def update_config(self, config):
        # Keep the historical key so existing installations migrate with zero
        # data conversion and older releases can still read the shortcut.
        config[CHROME_NEXT_HOTKEY_CONFIG_KEY] = self.normalized_hotkey()


class ChromeLauncherPlugin:
    plugin_id = "chrome_launcher"
    display_name = "Chrome 启动器"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.dialog = None
        self.hotkey_manager = None
        self.global_hotkey = DEFAULT_CHROME_NEXT_HOTKEY

    def register(self, context):
        self.context = context
        context.register_command(
            PluginCommand(
                command_id="open",
                title="Chrome 启动器",
                callback=lambda _rows: self.open_launcher(),
                locations=frozenset({MAIN_MENU}),
                tooltip="管理并启动 Chrome Profile、预设网站和迭代分组",
                order=30,
            )
        )
        context.register_command(
            PluginCommand(
                command_id="launch_next",
                title="启动下一个 Chrome",
                callback=lambda _rows: self.launch_next_profile(),
                locations=frozenset({MAIN_MENU}),
                tooltip="启动已保存迭代队列中的下一个 Chrome Profile",
                order=40,
            )
        )
        context.register_settings_page(
            PluginSettingsPage(
                page_id="settings",
                title="Chrome 插件",
                factory=ChromeLauncherSettingsPage,
                order=210,
            )
        )

    def start(self):
        config = self.context.load_config()
        try:
            self.global_hotkey = normalize_hotkey_sequence(
                config.get(
                    CHROME_NEXT_HOTKEY_CONFIG_KEY,
                    DEFAULT_CHROME_NEXT_HOTKEY,
                )
            )
        except ValueError:
            self.global_hotkey = DEFAULT_CHROME_NEXT_HOTKEY

        config_path = getattr(
            self.context.parent_widget,
            "config_name",
            "config.json",
        )
        self.dialog = ChromeRunnerDialog(
            self.context.parent_widget,
            config_path=str(config_path),
        )
        self.hotkey_manager = GlobalHotkeyManager(
            self.context.parent_widget,
            hotkey_id=CHROME_NEXT_HOTKEY_ID,
        )
        self.hotkey_manager.activated.connect(self.launch_next_profile)
        self._register_hotkey(self.global_hotkey, show_error=False)
        self._update_host_button_tooltips()

    def _register_hotkey(self, shortcut, show_error=True):
        try:
            shortcut = normalize_hotkey_sequence(shortcut)
        except ValueError as error:
            self.context.log(f"全局快捷键无效：{error}", logging.ERROR)
            if show_error:
                QtWidgets.QMessageBox.warning(
                    self.context.parent_widget,
                    "全局快捷键无效",
                    f"启动下一个浏览器：{error}",
                )
            return False

        if self.hotkey_manager.register(shortcut):
            self.global_hotkey = shortcut
            self.context.log(f"全局快捷键已启用：{shortcut}")
            self._update_host_button_tooltips()
            return True

        message = (
            f"{shortcut} 无法注册，可能已被其他程序占用。"
            f" {self.hotkey_manager.last_error}"
        )
        self.context.log(f"全局快捷键未启用：{message}", logging.ERROR)
        if show_error:
            QtWidgets.QMessageBox.warning(
                self.context.parent_widget,
                "全局快捷键注册失败",
                message,
            )
        return False

    def _update_host_button_tooltips(self):
        window = self.context.parent_widget
        open_button = getattr(window, "open_chrome_btn", None)
        if open_button is not None:
            open_button.setToolTip(
                "打开 Chrome 启动器；配置由 Chrome 插件管理。"
            )
        next_button = getattr(window, "launch_next_chrome_btn", None)
        if next_button is not None:
            next_button.setToolTip(
                "启动已保存迭代队列中的下一个 Chrome Profile。"
                f"系统全局快捷键：{self.global_hotkey}"
            )

    def open_launcher(self):
        if self.dialog is None:
            raise RuntimeError("Chrome 启动器尚未初始化")
        return self.dialog.exec_()

    def launch_next_profile(self):
        if self.dialog is None:
            raise RuntimeError("Chrome 启动器尚未初始化")
        profile = self.dialog.launch_next_profile()
        if profile:
            self.context.log(
                f"已按顺序启动 Chrome：{profile['name']} "
                f"({profile['directory']})"
            )
        return profile

    def apply_settings(self, config):
        return self._register_hotkey(
            config.get(
                CHROME_NEXT_HOTKEY_CONFIG_KEY,
                self.global_hotkey,
            )
        )

    def update_config(self, config):
        config[CHROME_NEXT_HOTKEY_CONFIG_KEY] = self.global_hotkey

    def stop(self):
        if self.hotkey_manager is not None:
            self.hotkey_manager.close()
            self.hotkey_manager = None
        if self.dialog is not None:
            self.dialog.close()
