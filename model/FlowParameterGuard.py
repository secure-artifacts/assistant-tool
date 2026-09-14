import ctypes
import os
import re
import threading
import time
from ctypes import wintypes

from PyQt5 import QtCore


FLOW_GUARD_CONFIG_KEY = "flow_parameter_guard"

FLOW_KEEP_ORIGINAL = "保持原样"
FLOW_GENERATION_MODES = ("图片", "视频")
FLOW_VIDEO_TYPES = ("帧", "素材")
FLOW_ASPECT_RATIOS = ("9:16", "16:9")
FLOW_MODELS = (
    "Omni 1.1 Flash",
    "Veo 3.1 - Lite",
    "Veo 3.1 - Fast",
    "Veo 3.1 - Quality",
)
FLOW_RESOLUTIONS = ("720p", "360p")
FLOW_DURATIONS = ("4 秒", "6 秒", "8 秒", "10 秒")
FLOW_OUTPUT_COUNTS = ("x1", "x2", "x3", "x4")

FLOW_VEO_CREDITS_PER_OUTPUT = {
    10: "Veo 3.1 - Lite",
    20: "Veo 3.1 - Fast",
    100: "Veo 3.1 - Quality",
}

FLOW_GUARD_TARGET_FIELDS = (
    ("模式", "generation_mode"),
    ("视频类型", "video_type"),
    ("宽高比", "aspect_ratio"),
    ("模型", "model"),
    ("视频分辨率", "resolution"),
    ("视频时长", "duration"),
    ("输出数量", "output_count"),
)

DEFAULT_FLOW_GUARD_SETTINGS = {
    "enabled": True,
    # New fields default to KEEP so upgrading an existing installation does
    # not unexpectedly change how an existing Flow project is generated.
    "generation_mode": FLOW_KEEP_ORIGINAL,
    "video_type": "帧",
    "aspect_ratio": "9:16",
    "model": FLOW_KEEP_ORIGINAL,
    "resolution": "720p",
    "duration": FLOW_KEEP_ORIGINAL,
    "output_count": FLOW_KEEP_ORIGINAL,
    # Safe by default: only correct controls after the user opens Flow's
    # settings panel.  Proactively opening/closing the panel can interfere
    # with a generation that is starting at the same time.
    "actively_open_settings": False,
    "poll_seconds": 2.0,
}


def _bool_value(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().casefold() in {"1", "true", "yes", "on", "是"}


def _choice(value, choices, default):
    text = str(value or "").strip()
    return text if text in choices else default


def _guard_choice(value, choices, default=FLOW_KEEP_ORIGINAL):
    return _choice(value, (FLOW_KEEP_ORIGINAL,) + tuple(choices), default)


def normalize_flow_guard_settings(value=None):
    source = value if isinstance(value, dict) else {}
    try:
        poll_seconds = float(source.get("poll_seconds", 2.0))
    except (TypeError, ValueError):
        poll_seconds = 2.0
    return {
        "enabled": _bool_value(source.get("enabled"), True),
        "generation_mode": _guard_choice(
            source.get("generation_mode"), FLOW_GENERATION_MODES
        ),
        "video_type": _guard_choice(
            source.get("video_type"), FLOW_VIDEO_TYPES, "帧"
        ),
        "aspect_ratio": _guard_choice(
            source.get("aspect_ratio"), FLOW_ASPECT_RATIOS, "9:16"
        ),
        "model": _guard_choice(source.get("model"), FLOW_MODELS),
        "resolution": _guard_choice(
            source.get("resolution"), FLOW_RESOLUTIONS, "720p"
        ),
        "duration": _guard_choice(source.get("duration"), FLOW_DURATIONS),
        "output_count": _guard_choice(
            source.get("output_count"), FLOW_OUTPUT_COUNTS
        ),
        # ``check_new_page`` was the old aggressive default.  Deliberately do
        # not inherit it: existing installations are migrated to passive mode.
        "actively_open_settings": _bool_value(
            source.get("actively_open_settings"), False
        ),
        "poll_seconds": min(30.0, max(0.5, poll_seconds)),
    }


def flow_guard_target_items(settings):
    """Return configured targets, excluding fields set to KEEP."""
    normalized = normalize_flow_guard_settings(settings)
    return tuple(
        (label, normalized[field])
        for label, field in FLOW_GUARD_TARGET_FIELDS
        if normalized[field] != FLOW_KEEP_ORIGINAL
    )


def format_flow_guard_targets(settings):
    items = flow_guard_target_items(settings)
    if not items:
        return "全部保持原样"
    return " / ".join(f"{label}={value}" for label, value in items)


def option_name_matches(name, expected):
    """Match Flow's accessible names, which may include explanatory text."""
    normalized_name = " ".join(str(name or "").split()).casefold()
    normalized_expected = " ".join(str(expected or "").split()).casefold()
    return bool(
        normalized_expected
        and (
            normalized_name == normalized_expected
            or normalized_name.startswith(normalized_expected + " ")
        )
    )


def is_flow_chrome_window(class_name, title):
    return (
        str(class_name or "") == "Chrome_WidgetWin_1"
        and "google flow" in str(title or "").casefold()
    )


def _enum_flow_windows():
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    windows = []
    foreground_hwnd = int(user32.GetForegroundWindow())
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )

    @callback_type
    def callback(hwnd, _lparam):
        # Only the Flow window the user is actually working in is inspected.
        # This keeps multiple Chrome profiles supported without repeatedly
        # walking large accessibility trees in background windows.
        if int(hwnd) != foreground_hwnd:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
        title_buffer = ctypes.create_unicode_buffer(1024)
        user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
        if is_flow_chrome_window(class_buffer.value, title_buffer.value):
            windows.append((int(hwnd), title_buffer.value))
        return True

    user32.EnumWindows(callback, 0)
    return windows


class _FlowUiaController:
    """Use Windows UI Automation so every Chrome profile shares one guard."""

    category_labels = {
        "模式": FLOW_GENERATION_MODES,
        "视频类型": FLOW_VIDEO_TYPES,
        "宽高比": FLOW_ASPECT_RATIOS,
        "视频分辨率": FLOW_RESOLUTIONS,
        "视频时长": FLOW_DURATIONS,
        "输出数量": FLOW_OUTPUT_COUNTS,
    }

    def __init__(self, settings):
        self.settings = normalize_flow_guard_settings(settings)
        self._checked_pages = set()
        self._load_uia()

    def _load_uia(self):
        try:
            from comtypes.client import CreateObject
            from comtypes.gen import UIAutomationClient as UIA
        except ImportError:
            from comtypes.client import CreateObject, GetModule

            GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as UIA
        self.UIA = UIA
        self.automation = CreateObject(
            UIA.CUIAutomation,
            interface=UIA.IUIAutomation,
        )
        self.true_condition = self.automation.CreateTrueCondition()
        self.radio_condition = self.automation.CreatePropertyCondition(
            UIA.UIA_ControlTypePropertyId,
            UIA.UIA_RadioButtonControlTypeId,
        )
        self.button_condition = self.automation.CreatePropertyCondition(
            UIA.UIA_ControlTypePropertyId,
            UIA.UIA_ButtonControlTypeId,
        )
        self.hyperlink_condition = self.automation.CreatePropertyCondition(
            UIA.UIA_ControlTypePropertyId,
            UIA.UIA_HyperlinkControlTypeId,
        )

    def _root(self, hwnd):
        return self.automation.ElementFromHandle(hwnd)

    def _elements(self, root, condition):
        try:
            collection = root.FindAll(self.UIA.TreeScope_Descendants, condition)
        except Exception:
            # Flow frequently rebuilds this panel. An element obtained a few
            # milliseconds earlier can already be a stale/null COM pointer.
            return []
        result = []
        try:
            length = collection.Length
        except Exception:
            return result
        for index in range(length):
            try:
                result.append(collection.GetElement(index))
            except Exception:
                continue
        return result

    @staticmethod
    def _name(element):
        try:
            return " ".join(str(element.CurrentName or "").split())
        except Exception:
            return ""

    def _selected_state(self, element):
        """Return True/False only when Chrome exposes a reliable state."""
        selection_pattern = self._pattern(
            element,
            "UIA_SelectionItemPatternId",
            "IUIAutomationSelectionItemPattern",
        )
        if selection_pattern is not None:
            try:
                return bool(selection_pattern.CurrentIsSelected)
            except Exception:
                pass

        toggle_pattern = self._pattern(
            element,
            "UIA_TogglePatternId",
            "IUIAutomationTogglePattern",
        )
        if toggle_pattern is not None:
            try:
                state = int(toggle_pattern.CurrentToggleState)
                if state in (0, 1):
                    return state == 1
            except Exception:
                pass

        legacy_pattern = self._pattern(
            element,
            "UIA_LegacyIAccessiblePatternId",
            "IUIAutomationLegacyIAccessiblePattern",
        )
        if legacy_pattern is not None:
            try:
                state = int(legacy_pattern.CurrentState)
                # MSAA STATE_SYSTEM_SELECTED / STATE_SYSTEM_CHECKED.
                return bool(state & (0x2 | 0x10))
            except Exception:
                pass

        try:
            aria = str(
                element.GetCurrentPropertyValue(
                    self.UIA.UIA_AriaPropertiesPropertyId
                )
                or ""
            ).replace(" ", "").casefold()
            if "checked=true" in aria or "selected=true" in aria:
                return True
            if "checked=false" in aria or "selected=false" in aria:
                return False
        except Exception:
            pass
        return None

    def _pattern(self, element, pattern_id_name, interface_name):
        """Return a supported UIA pattern without dereferencing null COM pointers."""
        try:
            pattern_id = getattr(self.UIA, pattern_id_name)
            interface = getattr(self.UIA, interface_name)
            pattern_unknown = element.GetCurrentPattern(pattern_id)
            if not pattern_unknown:
                return None
            pattern = pattern_unknown.QueryInterface(interface)
            return pattern if pattern else None
        except Exception:
            return None

    def _select(self, element):
        pattern = self._pattern(
            element,
            "UIA_SelectionItemPatternId",
            "IUIAutomationSelectionItemPattern",
        )
        if pattern is None:
            return False
        try:
            pattern.Select()
            return True
        except Exception:
            return False

    def _invoke(self, element):
        pattern = self._pattern(
            element,
            "UIA_InvokePatternId",
            "IUIAutomationInvokePattern",
        )
        if pattern is None:
            return False
        try:
            pattern.Invoke()
            return True
        except Exception:
            return False

    def _legacy_action(self, element):
        pattern = self._pattern(
            element,
            "UIA_LegacyIAccessiblePatternId",
            "IUIAutomationLegacyIAccessiblePattern",
        )
        if pattern is None:
            return False
        try:
            pattern.DoDefaultAction()
            return True
        except Exception:
            return False

    def _expand(self, element):
        pattern = self._pattern(
            element,
            "UIA_ExpandCollapsePatternId",
            "IUIAutomationExpandCollapsePattern",
        )
        if pattern is None:
            return False
        try:
            pattern.Expand()
            return True
        except Exception:
            return False

    def _collapse(self, element):
        pattern = self._pattern(
            element,
            "UIA_ExpandCollapsePatternId",
            "IUIAutomationExpandCollapsePattern",
        )
        if pattern is None:
            return False
        try:
            pattern.Collapse()
            return True
        except Exception:
            return False

    @staticmethod
    def _is_offscreen(element):
        try:
            return bool(element.CurrentIsOffscreen)
        except Exception:
            return False

    def _activate(self, element):
        """Activate a Chrome option regardless of its exposed UIA pattern."""
        for action in (self._select, self._invoke, self._legacy_action):
            if action(element):
                return True
        return False

    def _open_popup(self, element):
        return bool(
            self._invoke(element)
            or self._expand(element)
            or self._legacy_action(element)
        )

    def _close_popup(self, element):
        return bool(
            self._collapse(element)
            or self._invoke(element)
            or self._legacy_action(element)
        )

    def _find_settings_button(self, root):
        for element in self._elements(root, self.button_condition):
            if self._name(element) == "设置触发器":
                return element
        return None

    def _find_model_selector(self, root):
        for element in self._elements(root, self.button_condition):
            if self._name(element) == "选择模型系列":
                return element
        return None

    def _named_elements(self, root, expected):
        condition = self.automation.CreatePropertyCondition(
            self.UIA.UIA_NamePropertyId,
            expected,
        )
        return self._elements(root, condition)

    def _credit_cost(self, root):
        for element in self._elements(root, self.hyperlink_condition):
            match = re.search(
                r"(\d+)\s*(?:个点数|点数|credits?)",
                self._name(element),
                re.IGNORECASE,
            )
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _selected_option_value(options, category, labels):
        for option in options:
            if option["category"] != category or option["selected"] is not True:
                continue
            for label in labels:
                if option_name_matches(option["name"], label):
                    return label
        return ""

    def _infer_model_from_parameters(self, root):
        """Infer the closed model selector from model-specific Flow controls."""
        options = self._radio_options(root)
        categories = {option["category"] for option in options}
        # The video-only type controls protect against mistaking image models
        # for one of the video models below.
        if "视频类型" not in categories:
            return ""
        if "视频分辨率" in categories or "视频时长" in categories:
            return "Omni 1.1 Flash"

        output_count = self._selected_option_value(
            options,
            "输出数量",
            FLOW_OUTPUT_COUNTS,
        )
        if not output_count:
            return ""
        try:
            multiplier = int(output_count[1:])
        except (TypeError, ValueError):
            return ""
        total_credits = self._credit_cost(root)
        if total_credits is None or multiplier <= 0:
            return ""
        credits_per_output, remainder = divmod(total_credits, multiplier)
        if remainder:
            return ""
        return FLOW_VEO_CREDITS_PER_OUTPUT.get(credits_per_output, "")

    def _current_model(self, root, selector):
        selector_models = set()
        for element in self._elements(selector, self.true_condition):
            name = self._name(element)
            for model in FLOW_MODELS:
                if option_name_matches(name, model):
                    selector_models.add(model)
        if len(selector_models) == 1:
            return next(iter(selector_models))
        if len(selector_models) > 1:
            return ""

        # Some Chrome versions expose the visible model text as a sibling of
        # the selector rather than as its child. Only one visible model is a
        # reliable reading; several means the model menu is currently open.
        visible_models = []
        for model in FLOW_MODELS:
            if any(
                not self._is_offscreen(element)
                for element in self._named_elements(root, model)
            ):
                visible_models.append(model)
        if len(visible_models) == 1:
            return visible_models[0]
        if len(visible_models) > 1:
            return ""
        return self._infer_model_from_parameters(root)

    def _radio_options(self, root):
        options = []
        for element in self._elements(root, self.radio_condition):
            name = self._name(element)
            if not name:
                continue
            category = None
            for category_name, labels in self.category_labels.items():
                if any(option_name_matches(name, label) for label in labels):
                    category = category_name
                    break
            if category:
                options.append(
                    {
                        "category": category,
                        "name": name,
                        "element": element,
                        "selected": self._selected_state(element),
                    }
                )
        return options

    def _correct_radio_category(self, hwnd, category, expected):
        if expected == FLOW_KEEP_ORIGINAL:
            return False, None
        options = self._radio_options(self._root(hwnd))
        category_options = [
            option for option in options if option["category"] == category
        ]
        if not category_options:
            return False, None
        target = next(
            (
                option
                for option in category_options
                if option_name_matches(option["name"], expected)
            ),
            None,
        )
        if target is None:
            return True, None
        current = next(
            (
                option
                for option in category_options
                if option["selected"] is True
            ),
            None,
        )
        # Never write when Chrome does not expose a reliable current value.
        # This prevents a polling pass from repeatedly clicking a value that
        # was already correct.
        if current is None:
            return True, None
        if option_name_matches(current["name"], expected):
            return True, None
        if not self._activate(target["element"]):
            # The page was probably rebuilt between reading and selecting.
            # The next polling pass will retry with a fresh element.
            return True, None
        time.sleep(0.08)
        return True, {
            "parameter": category,
            "from": current["name"],
            "to": expected,
        }

    def _correct_model(self, hwnd):
        expected = self.settings["model"]
        if expected == FLOW_KEEP_ORIGINAL:
            return False, None
        root = self._root(hwnd)
        selector = self._find_model_selector(root)
        if selector is None:
            return False, None
        previous = self._current_model(root, selector)
        if not previous:
            # If the current model cannot be read (or the menu is open), do
            # not guess. A guard should never modify an uncertain value.
            return True, None
        if option_name_matches(previous, expected):
            return True, None

        if not self._open_popup(selector):
            return True, None
        time.sleep(0.12)
        root = self._root(hwnd)
        candidates = self._named_elements(root, expected)
        activated = False
        for candidate in candidates:
            if self._is_offscreen(candidate):
                continue
            if self._activate(candidate):
                activated = True
                break
        if not activated:
            # Close the menu again if this model is unavailable in the current
            # mode. Flow intentionally exposes different compatible options.
            try:
                close_selector = self._find_model_selector(self._root(hwnd))
                if close_selector is not None:
                    self._close_popup(close_selector)
            except Exception:
                pass
            return True, None
        time.sleep(0.08)
        return True, {
            "parameter": "模型",
            "from": previous or "未知",
            "to": expected,
        }

    def _correct_open_panel(self, hwnd):
        changes = []
        root = self._root(hwnd)
        panel_found = bool(self._radio_options(root) or self._find_model_selector(root))
        if not panel_found:
            return False, changes

        radio_targets_before_model = (
            ("模式", self.settings["generation_mode"]),
            ("视频类型", self.settings["video_type"]),
            ("宽高比", self.settings["aspect_ratio"]),
            ("输出数量", self.settings["output_count"]),
        )
        radio_targets_after_model = (
            ("视频分辨率", self.settings["resolution"]),
            ("视频时长", self.settings["duration"]),
        )
        for category, expected in radio_targets_before_model:
            _found, change = self._correct_radio_category(
                hwnd, category, expected
            )
            if change:
                changes.append(change)

        _found, change = self._correct_model(hwnd)
        if change:
            changes.append(change)

        for category, expected in radio_targets_after_model:
            _found, change = self._correct_radio_category(
                hwnd, category, expected
            )
            if change:
                changes.append(change)
        return panel_found, changes

    def _initial_check(self, hwnd):
        root = self._root(hwnd)
        button = self._find_settings_button(root)
        if button is None:
            return False, []
        if not self._activate(button):
            return False, []
        panel_found = False
        changes = []
        for _attempt in range(5):
            time.sleep(0.16)
            panel_found, changes = self._correct_open_panel(hwnd)
            if panel_found:
                break
        try:
            close_button = self._find_settings_button(self._root(hwnd))
            if close_button is not None:
                self._activate(close_button)
        except Exception:
            # The values were already changed; leaving the panel visible is
            # safer than sending Escape to whatever currently owns focus.
            pass
        return panel_found, changes

    def scan(self):
        results = []
        for hwnd, title in _enum_flow_windows():
            page_key = (hwnd, title)
            try:
                panel_found, changes = self._correct_open_panel(hwnd)
                if (
                    not panel_found
                    and self.settings["actively_open_settings"]
                    and page_key not in self._checked_pages
                ):
                    panel_found, changes = self._initial_check(hwnd)
                if panel_found:
                    self._checked_pages.add(page_key)
                if changes:
                    results.append(
                        {
                            "window": title,
                            "changes": changes,
                        }
                    )
            except Exception as error:
                results.append(
                    {
                        "window": title,
                        "error": str(error),
                    }
                )
        return results


class FlowParameterGuardThread(QtCore.QThread):
    status = QtCore.pyqtSignal(str)
    corrected = QtCore.pyqtSignal(object)
    error = QtCore.pyqtSignal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = normalize_flow_guard_settings(settings)
        self._wake_event = threading.Event()
        self._last_error_signature = ""
        self._last_error_time = 0.0

    def stop(self):
        self.requestInterruption()
        self._wake_event.set()

    def _wait(self):
        self._wake_event.wait(self.settings["poll_seconds"])
        self._wake_event.clear()

    def _emit_error_once(self, message):
        now = time.monotonic()
        signature = str(message)
        if (
            signature != self._last_error_signature
            or now - self._last_error_time >= 60.0
        ):
            self._last_error_signature = signature
            self._last_error_time = now
            self.error.emit(signature)

    def run(self):
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception as error:
            self.error.emit(f"Windows 控件接口初始化失败：{error}")
            return
        try:
            controller = _FlowUiaController(self.settings)
            self.status.emit(
                "运行中：{}".format(format_flow_guard_targets(self.settings))
            )
            while not self.isInterruptionRequested():
                try:
                    for result in controller.scan():
                        if result.get("error"):
                            self._emit_error_once(
                                "{}：{}".format(
                                    result.get("window", "Flow"),
                                    result["error"],
                                )
                            )
                        elif result.get("changes"):
                            self.corrected.emit(result)
                except Exception as error:
                    self._emit_error_once(f"扫描 Chrome 失败：{error}")
                self._wait()
        except Exception as error:
            self.error.emit(f"Flow 参数守卫启动失败：{error}")
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
