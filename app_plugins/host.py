import logging
from collections import OrderedDict

from PyQt5 import QtWidgets

from app_plugins.api import (
    MAIN_MENU,
    TASK_CONTEXT_MENU,
    TOOLS_MENU,
    PluginCommand,
    PluginSettingsPage,
)


class PluginContext:
    """Stable services offered by the main application to one plugin."""

    def __init__(self, host, plugin_id):
        self._host = host
        self.plugin_id = plugin_id

    @property
    def parent_widget(self):
        return self._host.main_window

    def register_command(self, command):
        self._host.register_command(self.plugin_id, command)

    def register_settings_page(self, page):
        self._host.register_settings_page(self.plugin_id, page)

    def load_config(self):
        return self._host.main_window.load_config()

    def save_config(self):
        return self._host.main_window.saveCurrentConfig()

    def log(self, message, level=logging.INFO):
        self._host.main_window.appendLog(
            f"[插件/{self.plugin_id}] {message}",
            end="",
            level=level,
        )

    def notify(self, title, message, critical=False):
        self._host.main_window.showDesktopNotification(
            title,
            message,
            critical=critical,
        )

    def selected_task_rows(self):
        return self._host.main_window.selectedTaskRows()

    def task_targets(self, rows, require_loaded=True):
        return self._host.main_window.taskTargetsForRows(
            rows,
            require_loaded=require_loaded,
        )

    def update_command(
        self,
        command_id,
        title=None,
        tooltip=None,
        enabled=None,
        checked=None,
    ):
        self._host.update_command(
            self.plugin_id,
            command_id,
            title=title,
            tooltip=tooltip,
            enabled=enabled,
            checked=checked,
        )


class PluginHost:
    """Registers built-in plugins and connects them to the host UI."""

    api_version = 1

    def __init__(self, main_window):
        self.main_window = main_window
        self._plugins = OrderedDict()
        self._contexts = {}
        self._commands = OrderedDict()
        self._settings_pages = OrderedDict()
        self._settings_controllers = []
        self._main_actions = {}
        self._tool_actions = {}
        self._tools_separator = None
        self.menu = None
        self.tools_menu = None

    def install(self, plugin):
        plugin_id = str(getattr(plugin, "plugin_id", "") or "").strip()
        if not plugin_id:
            raise ValueError("插件缺少 plugin_id。")
        if plugin_id in self._plugins:
            raise ValueError(f"插件 ID 重复：{plugin_id}")
        required_api = int(getattr(plugin, "required_api_version", 1))
        if required_api > self.api_version:
            raise RuntimeError(
                f"插件 {plugin_id} 需要接口版本 {required_api}，"
                f"当前仅支持 {self.api_version}。"
            )
        context = PluginContext(self, plugin_id)
        existing_commands = set(self._commands)
        existing_pages = set(self._settings_pages)
        self._plugins[plugin_id] = plugin
        self._contexts[plugin_id] = context
        try:
            plugin.register(context)
        except Exception:
            self._plugins.pop(plugin_id, None)
            self._contexts.pop(plugin_id, None)
            for command_id in set(self._commands) - existing_commands:
                self._commands.pop(command_id, None)
            for page_id in set(self._settings_pages) - existing_pages:
                self._settings_pages.pop(page_id, None)
            raise
        if self.menu is not None:
            self._rebuild_main_menu()
        if self.tools_menu is not None:
            self._rebuild_tools_menu()
        return plugin

    def plugin(self, plugin_id):
        return self._plugins.get(plugin_id)

    def register_command(self, plugin_id, command):
        if not isinstance(command, PluginCommand):
            raise TypeError("register_command 只接受 PluginCommand。")
        command_id = str(command.command_id or "").strip()
        if not command_id:
            raise ValueError("插件命令缺少 command_id。")
        full_id = f"{plugin_id}.{command_id}"
        if full_id in self._commands:
            raise ValueError(f"插件命令 ID 重复：{full_id}")
        invalid_locations = set(command.locations) - {
            MAIN_MENU,
            TOOLS_MENU,
            TASK_CONTEXT_MENU,
        }
        if invalid_locations:
            raise ValueError(f"插件命令位置无效：{sorted(invalid_locations)}")
        self._commands[full_id] = (plugin_id, command)

    def register_settings_page(self, plugin_id, page):
        if not isinstance(page, PluginSettingsPage):
            raise TypeError("register_settings_page 只接受 PluginSettingsPage。")
        page_id = str(page.page_id or "").strip()
        if not page_id:
            raise ValueError("插件设置页缺少 page_id。")
        full_id = f"{plugin_id}.{page_id}"
        if full_id in self._settings_pages:
            raise ValueError(f"插件设置页 ID 重复：{full_id}")
        self._settings_pages[full_id] = (plugin_id, page)

    def attach_main_menu(self, menu_bar):
        if self.menu is None:
            self.menu = menu_bar.addMenu("插件")
            self.menu.setObjectName("plugins_menu")
        self._rebuild_main_menu()
        return self.menu

    def attach_tools_menu(self, tools_menu):
        self.tools_menu = tools_menu
        self._rebuild_tools_menu()
        return self.tools_menu

    def _sorted_commands(self, location):
        entries = [
            (full_id, plugin_id, command)
            for full_id, (plugin_id, command) in self._commands.items()
            if location in command.locations
        ]
        return sorted(entries, key=lambda item: (item[2].order, item[2].title, item[0]))

    def _rebuild_main_menu(self):
        if self.menu is None:
            return
        self.menu.clear()
        self._main_actions.clear()
        for full_id, plugin_id, command in self._sorted_commands(MAIN_MENU):
            action = self.menu.addAction(command.title)
            action.setObjectName(full_id.replace(".", "_"))
            action.setToolTip(command.tooltip)
            action.setCheckable(bool(command.checkable))
            if command.checkable:
                checked = command.checked() if callable(command.checked) else command.checked
                action.setChecked(bool(checked))
            action.triggered.connect(
                lambda checked=False, fid=full_id: self.invoke(fid, (), checked)
            )
            self._main_actions[full_id] = action
        if not self._main_actions:
            empty = self.menu.addAction("暂无可用插件")
            empty.setEnabled(False)

    def _rebuild_tools_menu(self):
        if self.tools_menu is None:
            return
        for action in self._tool_actions.values():
            self.tools_menu.removeAction(action)
            action.deleteLater()
        self._tool_actions.clear()
        if self._tools_separator is not None:
            self.tools_menu.removeAction(self._tools_separator)
            self._tools_separator.deleteLater()
            self._tools_separator = None

        entries = self._sorted_commands(TOOLS_MENU)
        if not entries:
            return
        if self.tools_menu.actions():
            self._tools_separator = self.tools_menu.addSeparator()
        for full_id, _plugin_id, command in entries:
            action = self.tools_menu.addAction(command.title)
            action.setObjectName(full_id.replace(".", "_"))
            action.setToolTip(command.tooltip)
            action.setCheckable(bool(command.checkable))
            if command.checkable:
                checked = command.checked() if callable(command.checked) else command.checked
                action.setChecked(bool(checked))
            action.triggered.connect(
                lambda checked=False, fid=full_id: self.invoke(fid, (), checked)
            )
            self._tool_actions[full_id] = action

    def populate_task_context_menu(self, menu, rows):
        actions = []
        context_rows = tuple(rows or ())
        for full_id, plugin_id, command in self._sorted_commands(TASK_CONTEXT_MENU):
            action = menu.addAction(command.title)
            action.setObjectName(full_id.replace(".", "_"))
            action.setToolTip(command.tooltip)
            if command.enabled is not None:
                try:
                    action.setEnabled(bool(command.enabled(context_rows)))
                except Exception as error:
                    action.setEnabled(False)
                    self._report_error(plugin_id, command.title, error)
            action.triggered.connect(
                lambda _checked=False, fid=full_id, selected=context_rows: self.invoke(
                    fid,
                    selected,
                )
            )
            actions.append(action)
        return actions

    def invoke(self, full_id, rows=(), checked=None):
        entry = self._commands.get(full_id)
        if entry is None:
            return None
        plugin_id, command = entry
        try:
            if command.checkable:
                return command.callback(tuple(rows or ()), bool(checked))
            return command.callback(tuple(rows or ()))
        except Exception as error:
            self._report_error(plugin_id, command.title, error)
            QtWidgets.QMessageBox.critical(
                self.main_window,
                "插件执行失败",
                f"{command.title} 执行失败：{error}\n\n详细信息已写入程序日志。",
            )
            return None

    def update_command(
        self,
        plugin_id,
        command_id,
        title=None,
        tooltip=None,
        enabled=None,
        checked=None,
    ):
        full_id = f"{plugin_id}.{command_id}"
        action = self._main_actions.get(full_id) or self._tool_actions.get(full_id)
        if action is None:
            return
        if title is not None:
            action.setText(str(title))
        if tooltip is not None:
            action.setToolTip(str(tooltip))
        if enabled is not None:
            action.setEnabled(bool(enabled))
        if checked is not None and action.isCheckable():
            action.blockSignals(True)
            action.setChecked(bool(checked))
            action.blockSignals(False)

    def create_settings_pages(self, dialog, tab_widget):
        controllers = []
        entries = sorted(
            self._settings_pages.items(),
            key=lambda item: (item[1][1].order, item[1][1].title, item[0]),
        )
        for full_id, (plugin_id, page) in entries:
            try:
                controller = page.factory(dialog)
                widget = getattr(controller, "widget", controller)
                if not isinstance(widget, QtWidgets.QWidget):
                    raise TypeError("设置页工厂必须返回 QWidget 或含 widget 的控制器。")
                tab_widget.addTab(widget, page.title)
                controllers.append((plugin_id, full_id, controller))
            except Exception as error:
                self._report_error(plugin_id, page.title, error)
        self._settings_controllers = controllers
        return list(controllers)

    def load_settings_pages(self, config):
        for plugin_id, full_id, controller in self._settings_controllers:
            loader = getattr(controller, "load_config", None)
            if loader is not None:
                try:
                    loader(config)
                except Exception as error:
                    self._report_error(plugin_id, full_id, error)

    def settings_hotkey_fields(self):
        fields = []
        for _plugin_id, _full_id, controller in self._settings_controllers:
            provider = getattr(controller, "hotkey_fields", None)
            if provider is not None:
                fields.extend(provider() or ())
        return fields

    def validate_settings_pages(self):
        for plugin_id, full_id, controller in self._settings_controllers:
            validator = getattr(controller, "validate", None)
            if validator is not None:
                try:
                    validator()
                except Exception as error:
                    widget = getattr(controller, "widget", None)
                    return plugin_id, full_id, widget, str(error)
        return None

    def update_settings_config(self, config):
        for plugin_id, full_id, controller in self._settings_controllers:
            updater = getattr(controller, "update_config", None)
            if updater is not None:
                try:
                    updater(config)
                except Exception as error:
                    self._report_error(plugin_id, full_id, error)
                    raise
        return config

    def start_all(self):
        for plugin_id, plugin in self._plugins.items():
            try:
                starter = getattr(plugin, "start", None)
                if starter is not None:
                    starter()
            except Exception as error:
                self._report_error(plugin_id, "启动", error)

    def apply_settings(self, config):
        results = []
        for plugin_id, plugin in self._plugins.items():
            try:
                callback = getattr(plugin, "apply_settings", None)
                result = True if callback is None else bool(callback(config))
            except Exception as error:
                self._report_error(plugin_id, "应用设置", error)
                result = False
            results.append((plugin_id, result))
        return results

    def update_runtime_config(self, config):
        for plugin_id, plugin in self._plugins.items():
            try:
                callback = getattr(plugin, "update_config", None)
                if callback is not None:
                    callback(config)
            except Exception as error:
                self._report_error(plugin_id, "保存配置", error)
                raise
        return config

    def can_close_all(self):
        for plugin_id, plugin in self._plugins.items():
            checker = getattr(plugin, "can_close", None)
            if checker is None:
                continue
            try:
                allowed, message = checker()
            except Exception as error:
                self._report_error(plugin_id, "退出检查", error)
                return False, "插件退出检查失败，请查看程序日志。"
            if not allowed:
                return False, str(message or "插件仍有任务正在运行。")
        return True, ""

    def stop_all(self):
        for plugin_id, plugin in reversed(self._plugins.items()):
            try:
                stopper = getattr(plugin, "stop", None)
                if stopper is not None:
                    stopper()
            except Exception as error:
                self._report_error(plugin_id, "停止", error)

    def _report_error(self, plugin_id, operation, error):
        self.main_window.appendLog(
            f"[插件/{plugin_id}] {operation}失败：{error}",
            end="",
            level=logging.ERROR,
        )
