# 应用插件接口

当前插件接口版本为 `1`。内置插件由主程序显式安装，暂不从任意目录自动执行第三方 Python 文件，避免未知代码在启动时获得业务配置和本地文件权限。

插件可以注册四类扩展：

- `main_menu`：显示在主窗口的“插件”菜单中，也支持可勾选的开关命令；
- `tools_menu`：追加到主窗口现有的“工具”菜单中；
- `task_context_menu`：显示在任务列表右键菜单中，回调会收到选中行；
- `PluginSettingsPage`：显示在“程序设置”中，由页面控制器负责加载、校验和保存配置。

插件生命周期为 `register(context) -> start() -> apply_settings(config) -> can_close() -> stop()`。主程序通过 `PluginContext` 提供日志、桌面通知、配置读写、选中任务行和安全的任务目录解析，不要求插件直接依赖 `MainDialog` 的内部字段。

新内置插件放在 `app_plugins/builtin/`，并在主窗口安装。插件 ID、命令 ID 和设置页 ID 必须稳定且唯一；需要更高接口版本时应声明 `required_api_version`，不能静默降级。

当前内置插件包括库存与素材管理、Chrome 启动器和切分音频。切分音频插件在任务右键菜单中处理所选任务目录，并在“工具”菜单提供支持文件/文件夹拖拽的批量窗口；参数统一保存到 `audio_splitter` 配置节。Chrome 插件只接管窗口生命周期、菜单入口和全局快捷键；继续使用历史配置键 `chrome_preset_websites`、`chrome_profile_groups`、`chrome_profile_iterator` 和 `chrome_next_global_hotkey`，升级时不得清空或另建平行配置。
