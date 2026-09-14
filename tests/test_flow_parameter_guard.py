import unittest

from model.FlowParameterGuard import (
    FLOW_KEEP_ORIGINAL,
    _FlowUiaController,
    flow_guard_target_items,
    format_flow_guard_targets,
    is_flow_chrome_window,
    normalize_flow_guard_settings,
    option_name_matches,
)


class FlowParameterGuardTests(unittest.TestCase):
    def test_defaults_match_requested_flow_video_settings(self):
        settings = normalize_flow_guard_settings({})
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["generation_mode"], FLOW_KEEP_ORIGINAL)
        self.assertEqual(settings["video_type"], "帧")
        self.assertEqual(settings["aspect_ratio"], "9:16")
        self.assertEqual(settings["model"], FLOW_KEEP_ORIGINAL)
        self.assertEqual(settings["resolution"], "720p")
        self.assertEqual(settings["duration"], FLOW_KEEP_ORIGINAL)
        self.assertEqual(settings["output_count"], FLOW_KEEP_ORIGINAL)
        self.assertFalse(settings["actively_open_settings"])

    def test_legacy_aggressive_default_is_migrated_to_passive_mode(self):
        settings = normalize_flow_guard_settings({"check_new_page": True})
        self.assertFalse(settings["actively_open_settings"])

    def test_invalid_values_fall_back_and_poll_interval_is_bounded(self):
        settings = normalize_flow_guard_settings(
            {
                "enabled": "否",
                "generation_mode": "文字",
                "video_type": "未知",
                "aspect_ratio": "正方形",
                "model": "Veo 99",
                "resolution": "1080p",
                "duration": "30 秒",
                "output_count": "x9",
                "poll_seconds": 0,
            }
        )
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["generation_mode"], FLOW_KEEP_ORIGINAL)
        self.assertEqual(settings["video_type"], "帧")
        self.assertEqual(settings["aspect_ratio"], "9:16")
        self.assertEqual(settings["model"], FLOW_KEEP_ORIGINAL)
        self.assertEqual(settings["resolution"], "720p")
        self.assertEqual(settings["duration"], FLOW_KEEP_ORIGINAL)
        self.assertEqual(settings["output_count"], FLOW_KEEP_ORIGINAL)
        self.assertEqual(settings["poll_seconds"], 0.5)

    def test_all_current_video_options_are_accepted(self):
        settings = normalize_flow_guard_settings(
            {
                "generation_mode": "视频",
                "video_type": "素材",
                "aspect_ratio": "16:9",
                "model": "Veo 3.1 - Quality",
                "resolution": "360p",
                "duration": "10 秒",
                "output_count": "x4",
            }
        )
        self.assertEqual(settings["generation_mode"], "视频")
        self.assertEqual(settings["video_type"], "素材")
        self.assertEqual(settings["aspect_ratio"], "16:9")
        self.assertEqual(settings["model"], "Veo 3.1 - Quality")
        self.assertEqual(settings["resolution"], "360p")
        self.assertEqual(settings["duration"], "10 秒")
        self.assertEqual(settings["output_count"], "x4")

    def test_keep_original_targets_are_excluded_from_summary(self):
        settings = normalize_flow_guard_settings(
            {
                "generation_mode": FLOW_KEEP_ORIGINAL,
                "video_type": FLOW_KEEP_ORIGINAL,
                "aspect_ratio": "16:9",
                "model": FLOW_KEEP_ORIGINAL,
                "resolution": FLOW_KEEP_ORIGINAL,
                "duration": "8 秒",
                "output_count": FLOW_KEEP_ORIGINAL,
            }
        )
        self.assertEqual(
            flow_guard_target_items(settings),
            (("宽高比", "16:9"), ("视频时长", "8 秒")),
        )
        self.assertEqual(
            format_flow_guard_targets(settings),
            "宽高比=16:9 / 视频时长=8 秒",
        )

    def test_every_target_can_keep_its_current_flow_value(self):
        settings = normalize_flow_guard_settings(
            {
                "generation_mode": FLOW_KEEP_ORIGINAL,
                "video_type": FLOW_KEEP_ORIGINAL,
                "aspect_ratio": FLOW_KEEP_ORIGINAL,
                "model": FLOW_KEEP_ORIGINAL,
                "resolution": FLOW_KEEP_ORIGINAL,
                "duration": FLOW_KEEP_ORIGINAL,
                "output_count": FLOW_KEEP_ORIGINAL,
            }
        )
        self.assertEqual(flow_guard_target_items(settings), ())
        self.assertEqual(format_flow_guard_targets(settings), "全部保持原样")

    def test_accessible_name_may_include_help_text(self):
        self.assertTrue(option_name_matches("720p", "720p"))
        self.assertTrue(option_name_matches("360p 360p 分辨率的清晰度较低", "360p"))
        self.assertTrue(option_name_matches("Veo 3.1 - Fast", "veo 3.1 - fast"))
        self.assertTrue(option_name_matches("10 秒 视频时长", "10 秒"))
        self.assertFalse(option_name_matches("16:9", "9:16"))

    def test_only_google_flow_chrome_windows_are_scanned(self):
        self.assertTrue(
            is_flow_chrome_window(
                "Chrome_WidgetWin_1",
                "Google Flow - 项目 - Google Chrome",
            )
        )
        self.assertFalse(
            is_flow_chrome_window(
                "Chrome_WidgetWin_1",
                "Google 表格 - Google Chrome",
            )
        )
        self.assertFalse(
            is_flow_chrome_window(
                "ApplicationFrameWindow",
                "Google Flow - 项目",
            )
        )

    def test_null_com_patterns_are_ignored_instead_of_crashing_scan(self):
        class FakeUia:
            UIA_SelectionItemPatternId = 1
            IUIAutomationSelectionItemPattern = object()
            UIA_InvokePatternId = 2
            IUIAutomationInvokePattern = object()
            UIA_LegacyIAccessiblePatternId = 3
            IUIAutomationLegacyIAccessiblePattern = object()
            UIA_ExpandCollapsePatternId = 4
            IUIAutomationExpandCollapsePattern = object()

        class NullComPointer:
            def QueryInterface(self, _interface):
                raise ValueError("NULL COM pointer access")

        class FakeElement:
            def GetCurrentPattern(self, _pattern_id):
                return NullComPointer()

        controller = object.__new__(_FlowUiaController)
        controller.UIA = FakeUia
        element = FakeElement()

        self.assertFalse(controller._activate(element))
        self.assertFalse(controller._open_popup(element))
        self.assertFalse(controller._close_popup(element))

    def test_radio_is_changed_only_after_a_different_current_value_is_known(self):
        controller = object.__new__(_FlowUiaController)
        controller._root = lambda _hwnd: object()
        activations = []
        controller._activate = lambda element: activations.append(element) or True

        controller._radio_options = lambda _root: [
            {
                "category": "宽高比",
                "name": "9:16",
                "element": "portrait",
                "selected": None,
            },
            {
                "category": "宽高比",
                "name": "16:9",
                "element": "landscape",
                "selected": None,
            },
        ]
        _found, change = controller._correct_radio_category(
            1, "宽高比", "9:16"
        )
        self.assertIsNone(change)
        self.assertEqual(activations, [])

        controller._radio_options = lambda _root: [
            {
                "category": "宽高比",
                "name": "9:16",
                "element": "portrait",
                "selected": True,
            },
            {
                "category": "宽高比",
                "name": "16:9",
                "element": "landscape",
                "selected": False,
            },
        ]
        _found, change = controller._correct_radio_category(
            1, "宽高比", "9:16"
        )
        self.assertIsNone(change)
        self.assertEqual(activations, [])

        controller._radio_options = lambda _root: [
            {
                "category": "宽高比",
                "name": "9:16",
                "element": "portrait",
                "selected": False,
            },
            {
                "category": "宽高比",
                "name": "16:9",
                "element": "landscape",
                "selected": True,
            },
        ]
        _found, change = controller._correct_radio_category(
            1, "宽高比", "9:16"
        )
        self.assertEqual(activations, ["portrait"])
        self.assertEqual(change["from"], "16:9")
        self.assertEqual(change["to"], "9:16")

    def test_unknown_current_model_does_not_open_or_modify_model_menu(self):
        controller = object.__new__(_FlowUiaController)
        controller.settings = {"model": "Omni 1.1 Flash"}
        controller._root = lambda _hwnd: object()
        controller._find_model_selector = lambda _root: object()
        controller._current_model = lambda _root, _selector: ""
        opened = []
        controller._open_popup = lambda _selector: opened.append(True) or True

        found, change = controller._correct_model(1)

        self.assertTrue(found)
        self.assertIsNone(change)
        self.assertEqual(opened, [])

    def test_model_is_inferred_from_its_flow_parameter_layout(self):
        controller = object.__new__(_FlowUiaController)

        def option(category, name, selected=False):
            return {
                "category": category,
                "name": name,
                "element": object(),
                "selected": selected,
            }

        omni_options = [
            option("视频类型", "帧", True),
            option("视频分辨率", "720p", True),
            option("视频时长", "8 秒", True),
            option("输出数量", "x1", True),
        ]
        controller._radio_options = lambda _root: omni_options
        controller._credit_cost = lambda _root: 12
        self.assertEqual(
            controller._infer_model_from_parameters(object()),
            "Omni 1.1 Flash",
        )

        veo_options = [
            option("视频类型", "帧", True),
            option("输出数量", "x2", True),
        ]
        controller._radio_options = lambda _root: veo_options
        for total_credits, expected_model in (
            (20, "Veo 3.1 - Lite"),
            (40, "Veo 3.1 - Fast"),
            (200, "Veo 3.1 - Quality"),
        ):
            controller._credit_cost = lambda _root, value=total_credits: value
            self.assertEqual(
                controller._infer_model_from_parameters(object()),
                expected_model,
            )

    def test_model_inference_refuses_ambiguous_non_video_layout(self):
        controller = object.__new__(_FlowUiaController)
        controller._radio_options = lambda _root: [
            {
                "category": "输出数量",
                "name": "x1",
                "element": object(),
                "selected": True,
            }
        ]
        controller._credit_cost = lambda _root: 20

        self.assertEqual(controller._infer_model_from_parameters(object()), "")

    def test_closed_model_selector_uses_parameter_inference(self):
        controller = object.__new__(_FlowUiaController)
        controller.true_condition = object()
        controller._elements = lambda _root, _condition: []
        controller._named_elements = lambda _root, _name: []
        controller._infer_model_from_parameters = (
            lambda _root: "Veo 3.1 - Fast"
        )

        self.assertEqual(
            controller._current_model(object(), object()),
            "Veo 3.1 - Fast",
        )


if __name__ == "__main__":
    unittest.main()
