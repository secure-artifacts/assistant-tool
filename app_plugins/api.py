from dataclasses import dataclass
from typing import Callable, FrozenSet


MAIN_MENU = "main_menu"
TOOLS_MENU = "tools_menu"
TASK_CONTEXT_MENU = "task_context_menu"


@dataclass(frozen=True)
class PluginCommand:
    """A command exposed by a plugin at one or more host UI locations."""

    command_id: str
    title: str
    callback: Callable
    locations: FrozenSet[str]
    tooltip: str = ""
    order: int = 100
    enabled: Callable = None
    checkable: bool = False
    checked: Callable = None


@dataclass(frozen=True)
class PluginSettingsPage:
    """A settings page factory registered by a plugin."""

    page_id: str
    title: str
    factory: Callable
    order: int = 100
