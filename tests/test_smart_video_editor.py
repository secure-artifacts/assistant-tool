import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets

from PYUI.main_setting_pyui import MainSettingDialog
from PYUI.smart_video_editor_pyui import (
    SmartVideoPendingDialog,
    SmartVideoReviewDialog,
    SmartVideoSourceDialog,
)
from model.SmartVideoEditor import (
    analyze_smart_video_jobs,
    apply_clip_review,
    best_unit_window,
    build_script_word_records,
    build_srt_cues,
    detect_silence_ranges,
    discover_task_videos,
    export_smart_video_bundle,
    find_pause_removals,
    kept_ranges,
    normalize_smart_video_editor_settings,
    normalize_smart_video_pending_reviews,
    refine_trim_boundaries,
    render_task_problem_report,
    smart_video_export_blockers,
    set_smart_video_missing_review,
    source_time_in_kept_ranges,
    split_script_blocks,
    subtitle_text_for_export,
    text_units,
    validate_smart_video_bundle_for_export,
    update_smart_video_pending_reviews,
)
from model.SubtitleHelper import generate_srt_whisper_only
from model.TextHelper import smart_split_sentences


class _Word:
    def __init__(self, word, start, end, probability=0.95):
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability


class _Segment:
    def __init__(self, text, words):
        self.text = text
        self.words = words


class _Info:
    language = "sk"
    duration = 3.0


class _FakeWhisperModel:
    def __init__(self):
        self.calls = 0

    def transcribe(self, _path, **_kwargs):
        self.calls += 1
        segment = _Segment(
            "Moj bože prosím zdravie",
            [
                _Word("Moj", 0.4, 0.7),
                _Word("bože", 0.8, 1.1),
                _Word("prosím", 1.2, 1.6),
                _Word("zdravie", 2.1, 2.5),
            ],
        )
        return iter([segment]), _Info()


class _MappingWhisperModel:
    def transcribe(self, path, **_kwargs):
        text = Path(path).stem.replace("_", " ")
        words = []
        cursor = 0.25
        for value in text.split():
            words.append(_Word(value, cursor, cursor + 0.3))
            cursor += 0.4
        info = _Info()
        info.duration = cursor + 0.2
        return iter([_Segment(text, words)]), info


class _DictionaryWhisperModel:
    def __init__(self, values):
        self.values = values

    def transcribe(self, path, **_kwargs):
        text = self.values[Path(path).name]
        words = []
        cursor = 0.25
        for value in text.split():
            words.append(_Word(value, cursor, cursor + 0.3))
            cursor += 0.4
        info = _Info()
        info.duration = cursor + 0.2
        return iter([_Segment(text, words)]), info


class SmartVideoEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_text_comparison_ignores_case_and_punctuation(self):
        self.assertEqual(
            text_units("MÔJ Bože, prosím!"),
            text_units("môj bože prosím"),
        )
        self.assertEqual(text_units("你好，世界！"), ["你", "好", "世", "界"])

    def test_best_window_ignores_false_start_and_tail(self):
        expected = text_units("prosím zdravie a silu")
        observed = text_units("ehm znovu prosím zdravie a silu ďakujem")
        match = best_unit_window(expected, observed)
        self.assertGreater(match["score"], 0.95)
        self.assertEqual(observed[match["start"]:match["end"]], expected)

    def test_script_is_balanced_when_one_paragraph_has_many_clips(self):
        blocks = split_script_blocks(
            "one two three four five six seven eight", target_count=2
        )
        self.assertEqual(len(blocks), 2)
        self.assertEqual(" ".join(blocks), "one two three four five six seven eight")
        weighted = split_script_blocks(
            "one two three four five six seven eight",
            target_count=2,
            weights=[1, 3],
        )
        self.assertEqual(weighted[0], "one two")
        self.assertEqual(weighted[1], "three four five six seven eight")

    def test_pause_compression_keeps_configured_breath(self):
        settings = normalize_smart_video_editor_settings({
            "compress_internal_pauses": True,
            "internal_pause_mode": "experimental",
            "pause_threshold_ms": 1000,
            "retained_pause_ms": 300,
        })
        words = [
            {"text": "a", "start": 0.2, "end": 0.5},
            {"text": "b", "start": 2.0, "end": 2.3},
        ]
        removals = find_pause_removals(
            words, 0.0, 2.5, settings, [[0.5, 2.0]]
        )
        self.assertEqual(removals, [[0.65, 1.85]])
        self.assertEqual(kept_ranges(0.0, 2.5, removals), [[0.0, 0.65], [1.85, 2.5]])
        self.assertAlmostEqual(
            source_time_in_kept_ranges(2.3, [[0.0, 0.65], [1.85, 2.5]]),
            1.1,
        )
        legacy = normalize_smart_video_editor_settings({
            "compress_internal_pauses": True,
        })
        self.assertFalse(legacy["compress_internal_pauses"])
        self.assertEqual(
            find_pause_removals(words, 0.0, 2.5, settings), []
        )

    def test_db_silence_detection_places_cuts_inside_measured_quiet_audio(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "silence-tone.wav"
            subprocess.run([
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=if(between(t\,1\,2.5)\,0.5*sin(2*PI*440*t)\,0):s=48000:d=4",
                str(source),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            settings = normalize_smart_video_editor_settings({
                "silence_threshold_db": -35,
                "min_silence_ms": 350,
            })
            ranges, error = detect_silence_ranges(
                source, ffmpeg, 4.0, settings
            )
            self.assertFalse(error)
            self.assertGreaterEqual(len(ranges), 2)
            start, end, decisions, warnings = refine_trim_boundaries(
                1.05, 2.45, 4.0, ranges, settings
            )
            self.assertFalse(warnings)
            self.assertAlmostEqual(start, 0.88, delta=0.08)
            self.assertAlmostEqual(end, 2.72, delta=0.08)
            self.assertEqual(
                [item["kind"] for item in decisions],
                ["head_cut", "tail_cut"],
            )

    def test_missing_db_boundary_preserves_audio_and_reports_exact_side(self):
        settings = normalize_smart_video_editor_settings({})
        start, end, decisions, warnings = refine_trim_boundaries(
            1.0, 2.0, 4.0, [], settings
        )
        self.assertEqual((start, end), (0.0, 4.0))
        self.assertFalse(decisions)
        self.assertEqual(
            [item["kind"] for item in warnings],
            ["head_boundary_unconfirmed", "tail_boundary_unconfirmed"],
        )

    def test_source_discovery_excludes_generated_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "001.mp4").write_bytes(b"")
            (root / "nested").mkdir()
            (root / "nested" / "002.mov").write_bytes(b"")
            output = root / "智能剪辑结果"
            output.mkdir()
            (output / "001_智能剪辑.mp4").write_bytes(b"")
            files = discover_task_videos(root)
            self.assertEqual([path.name for path in files], ["001.mp4", "002.mov"])

    def test_analysis_uses_task_script_and_reuses_integer_fingerprint_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary)
            source = task_dir / "clip.mp4"
            source.write_bytes(b"fake")
            jobs = [{
                "task_id": "0801",
                "task_name": "test",
                "label": "0801 | test",
                "task_dir": str(task_dir),
                "script": "Moj bože prosím zdravie",
                "language": "sk",
                "sources": [str(source)],
            }]
            model = _FakeWhisperModel()
            safe_settings = {"silence_detection_enabled": False}
            first = analyze_smart_video_jobs(jobs, model, safe_settings)
            second = analyze_smart_video_jobs(jobs, model, safe_settings)
            self.assertEqual(model.calls, 1)
            self.assertEqual(first["summary"]["green_count"], 1)
            self.assertTrue(second["tasks"][0]["clips"][0]["from_cache"])
            report = task_dir / "智能剪辑结果" / "智能剪辑审核.json"
            self.assertTrue(report.is_file())
            text_report = task_dir / "智能剪辑结果" / "智能剪辑问题报告.txt"
            self.assertTrue(text_report.is_file())

    def test_unordered_files_are_sorted_by_spoken_script_position(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [
                root / "third_words.mp4",
                root / "first_words.mp4",
                root / "second_words.mp4",
            ]
            for source in sources:
                source.write_bytes(source.name.encode("utf-8"))
            result = analyze_smart_video_jobs([{
                "task_id": "T-order",
                "task_name": "unordered",
                "label": "T-order | unordered",
                "task_dir": str(root),
                "script": "first words\nsecond words\nthird words",
                "language": "en",
                "sources": [str(path) for path in sources],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            clips = result["tasks"][0]["clips"]
            self.assertEqual(
                [clip["expected_text"] for clip in clips],
                ["first words", "second words", "third words"],
            )
            self.assertEqual(
                [clip["file_name"] for clip in clips],
                ["first_words.mp4", "second_words.mp4", "third_words.mp4"],
            )

    def test_numbered_order_survives_a_weak_but_unique_content_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "1_20260913.mp4", root / "2.mp4", root / "3.mp4"]
            for source in sources:
                source.write_bytes(source.name.encode("utf-8"))
            script = "alpha bravo charlie\ndelta echo foxtrot\ngolf hotel india"
            model = _DictionaryWhisperModel({
                "1_20260913.mp4": "alpha bravcharlie",
                "2.mp4": "delta echo foxtrot",
                "3.mp4": "golf hotel india",
            })
            result = analyze_smart_video_jobs([{
                "task_id": "T-numbered",
                "task_name": "numbered",
                "label": "T-numbered | numbered",
                "task_dir": str(root),
                "script": script,
                "language": "en",
                "sources": [str(path) for path in sources],
            }], model, {"silence_detection_enabled": False})
            task = result["tasks"][0]
            self.assertEqual(task["ordering_mode"], "content_verified_filename")
            self.assertEqual(
                [clip["file_name"] for clip in task["clips"]],
                ["1_20260913.mp4", "2.mp4", "3.mp4"],
            )
            combined = " ".join(
                clip["expected_text"] for clip in task["clips"]
            )
            self.assertEqual(text_units(combined), text_units(script))

    def test_confident_content_order_overrides_incorrect_filename_numbers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "1.mp4", root / "2.mp4", root / "3.mp4"]
            for source in sources:
                source.write_bytes(source.name.encode("utf-8"))
            result = analyze_smart_video_jobs([{
                "task_id": "T-wrong-numbers",
                "task_name": "wrong-numbers",
                "label": "T-wrong-numbers | wrong-numbers",
                "task_dir": str(root),
                "script": "first spoken section\nsecond spoken section\nthird spoken section",
                "language": "en",
                "sources": [str(path) for path in sources],
            }], _DictionaryWhisperModel({
                "1.mp4": "third spoken section",
                "2.mp4": "first spoken section",
                "3.mp4": "second spoken section",
            }), {"silence_detection_enabled": False})
            task = result["tasks"][0]
            self.assertEqual(task["ordering_mode"], "content_override_filename")
            self.assertTrue(task["ordering_conflict"])
            self.assertEqual(
                [clip["file_name"] for clip in task["clips"]],
                ["2.mp4", "3.mp4", "1.mp4"],
            )
            self.assertTrue(all(
                any(
                    issue["kind"] == "filename_content_order_conflict"
                    for issue in clip["issues"]
                )
                for clip in task["clips"]
            ))

    def test_filename_order_is_only_a_visible_fallback_when_audio_is_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "2.mp4", root / "1.mp4"]
            for source in sources:
                source.write_bytes(source.name.encode("utf-8"))
            result = analyze_smart_video_jobs([{
                "task_id": "T-fallback",
                "task_name": "fallback",
                "label": "T-fallback | fallback",
                "task_dir": str(root),
                "script": "first section second section",
                "language": "en",
                "sources": [str(path) for path in sources],
            }], _DictionaryWhisperModel({"1.mp4": "", "2.mp4": ""}), {
                "silence_detection_enabled": False,
            })
            task = result["tasks"][0]
            self.assertEqual(task["ordering_mode"], "filename_fallback")
            self.assertEqual(
                [clip["file_name"] for clip in task["clips"]],
                ["1.mp4", "2.mp4"],
            )
            self.assertTrue(all(clip["status"] == "pink" for clip in task["clips"]))

    def test_srt_keeps_authoritative_first_and_last_words_missed_by_asr(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clip.mp4"
            source.write_bytes(b"clip")
            script = "First, middle words, last!"
            result = analyze_smart_video_jobs([{
                "task_id": "T-edges",
                "task_name": "edges",
                "label": "T-edges | edges",
                "task_dir": str(root),
                "script": script,
                "language": "en",
                "sources": [str(source)],
            }], _DictionaryWhisperModel({"clip.mp4": "middle words"}), {
                "silence_detection_enabled": False,
            })
            task = result["tasks"][0]
            cues = build_srt_cues(task, result["settings"])
            self.assertEqual(
                text_units(" ".join(cue["text"] for cue in cues)),
                text_units(script),
            )
            self.assertEqual(
                " ".join(cue["text"] for cue in cues),
                script,
            )
            self.assertEqual(task["clips"][0]["script_word_start"], 0)
            self.assertEqual(task["clips"][0]["script_word_end"], 3)
            self.assertEqual(result["summary"]["missing_count"], 0)
            self.assertEqual(result["summary"]["unverified_count"], 2)
            self.assertFalse(result["summary"]["export_blocked"])

    def test_unanchored_leading_word_stays_with_following_numbered_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "1.mp4", root / "2.mp4"]
            for source in sources:
                source.write_bytes(source.name.encode("utf-8"))
            result = analyze_smart_video_jobs([{
                "task_id": "T-numbered-boundary",
                "task_name": "numbered-boundary",
                "label": "T-numbered-boundary | numbered-boundary",
                "task_dir": str(root),
                "script": "alpha beta gamma delta epsilon vaša prosba bude hotová",
                "language": "sk",
                "sources": [str(path) for path in sources],
            }], _DictionaryWhisperModel({
                "1.mp4": "alpha beta gamma delta epsilon",
                "2.mp4": "váša prosba bude hotová",
            }), {"silence_detection_enabled": False})
            clips = result["tasks"][0]["clips"]
            self.assertEqual(
                clips[0]["expected_text"], "alpha beta gamma delta epsilon"
            )
            self.assertEqual(clips[1]["expected_text"], "vaša prosba bude hotová")

    def test_srt_fills_missing_timing_inside_an_authoritative_range(self):
        lines, words = build_script_word_records("first middle last")
        task = {
            "script_lines": lines,
            "script_words": words,
            "clips": [{
                "source_index": 0,
                "export_order": 1,
                "included": True,
                "script_word_start": 0,
                "script_word_end": 2,
                "trim_start": 0.0,
                "trim_end": 2.0,
                "kept_ranges": [[0.0, 2.0]],
                "word_timeline": [{
                    "script_word_index": 1,
                    "line_index": 0,
                    "start": 0.7,
                    "end": 1.2,
                }],
                "words": [],
            }],
        }
        cues = build_srt_cues(
            task,
            normalize_smart_video_editor_settings({"srt_max_words_per_block": 1}),
        )
        self.assertEqual([cue["text"] for cue in cues], ["first", "middle", "last"])

    def test_duplicate_take_is_not_given_an_invented_second_position(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "alpha_beta.mp4"
            second = root / "gamma_delta.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            result = analyze_smart_video_jobs([{
                "task_id": "T-duplicate",
                "task_name": "duplicate",
                "label": "T-duplicate | duplicate",
                "task_dir": str(root),
                "script": "alpha beta\ngamma delta",
                "language": "en",
                "sources": [str(first), str(first), str(second)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            clips = result["tasks"][0]["clips"]
            unmatched = [clip for clip in clips if clip["script_word_start"] < 0]
            self.assertEqual(len(unmatched), 1)
            self.assertEqual(unmatched[0]["status"], "pink")
            self.assertTrue(result["summary"]["needs_review"])

    def test_cjk_phrase_token_maps_to_individual_script_character_times(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "你好世界.mp4"
            source.write_bytes(b"cjk")
            result = analyze_smart_video_jobs([{
                "task_id": "T-cjk",
                "task_name": "cjk",
                "label": "T-cjk | cjk",
                "task_dir": str(root),
                "script": "你好，世界！",
                "language": "zh",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            clip = result["tasks"][0]["clips"][0]
            self.assertEqual(clip["expected_text"], "你好，世界！")
            self.assertEqual(len(clip["word_timeline"]), 4)
            self.assertTrue(all(item["anchor"] for item in clip["word_timeline"]))
            self.assertEqual(clip["status"], "green")

    def test_word_error_reports_exact_time_expected_and_recognized_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "alpha_delta_gamma.mp4"
            source.write_bytes(b"mismatch")
            result = analyze_smart_video_jobs([{
                "task_id": "T-word-error",
                "task_name": "word-error",
                "label": "T-word-error | word-error",
                "task_dir": str(root),
                "script": "alpha beta gamma",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            clip = result["tasks"][0]["clips"][0]
            word_issues = [
                issue for issue in clip["issues"]
                if issue["kind"] == "word_difference"
            ]
            self.assertEqual(len(word_issues), 1)
            issue = word_issues[0]
            self.assertEqual(issue["expected"], "beta")
            self.assertEqual(issue["recognized"], "delta")
            self.assertAlmostEqual(issue["start"], 0.65, places=2)
            self.assertAlmostEqual(issue["end"], 0.95, places=2)
            self.assertEqual(clip["status"], "orange")

    def test_srt_chunks_use_real_word_anchors_and_keep_punctuation(self):
        lines, words = build_script_word_records("Hello, world!")
        task = {
            "script_lines": lines,
            "script_words": words,
            "clips": [{
                "source_index": 0,
                "export_order": 1,
                "included": True,
                "script_word_start": 0,
                "script_word_end": 1,
                "kept_ranges": [[0.0, 2.0]],
                "word_timeline": [
                    {"script_word_index": 0, "line_index": 0, "start": 0.2, "end": 0.55},
                    {"script_word_index": 1, "line_index": 0, "start": 1.15, "end": 1.60},
                ],
            }],
        }
        settings = normalize_smart_video_editor_settings({
            "srt_max_words_per_block": 1,
        })
        cues = build_srt_cues(task, settings)
        self.assertEqual([item["text"] for item in cues], ["Hello,", "world!"])
        self.assertEqual(
            [(round(item["start"], 2), round(item["end"], 2)) for item in cues],
            [(0.2, 0.55), (1.15, 1.6)],
        )

    def test_uncovered_script_is_reported_instead_of_forced_into_a_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_words.mp4"
            source.write_bytes(b"first")
            result = analyze_smart_video_jobs([{
                "task_id": "T-missing",
                "task_name": "missing",
                "label": "T-missing | missing",
                "task_dir": str(root),
                "script": "first words\nsecond sentence is absent",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            self.assertTrue(result["summary"]["needs_review"])
            self.assertEqual(result["summary"]["missing_count"], 1)
            self.assertTrue(result["summary"]["export_blocked"])
            self.assertIn(
                "second sentence is absent",
                result["tasks"][0]["missing_blocks"][0]["text"],
            )
            block = result["tasks"][0]["missing_blocks"][0]
            self.assertTrue(block["blocks_export"])
            self.assertEqual(block["previous_clip"], source.name)
            self.assertEqual(
                result["tasks"][0]["clips"][0]["expected_text"],
                "first words",
            )
            blockers = smart_video_export_blockers(result)
            self.assertEqual(len(blockers), 1)
            self.assertIn("second sentence is absent", blockers[0]["text"])
            with self.assertRaisesRegex(ValueError, "禁止生成"):
                validate_smart_video_bundle_for_export(result)

    def test_export_core_refuses_missing_segment_before_starting_ffmpeg(self):
        lines, words = build_script_word_records("first line\nmissing full line")
        bundle = {
            "settings": {},
            "tasks": [{
                "task_id": "T-blocked",
                "label": "T-blocked | missing",
                "script_lines": lines,
                "script_words": words,
                "clips": [{
                    "file_name": "first.mp4",
                    "included": True,
                    "script_word_start": 0,
                    "script_word_end": 1,
                }],
            }],
        }
        with mock.patch("model.SmartVideoEditor._resolve_ffmpeg") as resolve:
            with self.assertRaisesRegex(ValueError, "missing full line"):
                export_smart_video_bundle(bundle)
        resolve.assert_not_called()

    def test_short_complete_sentence_is_a_blocker_but_edge_word_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_sentence.mp4"
            source.write_bytes(b"first")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-short-sentence",
                "label": "T-short-sentence",
                "task_dir": str(root),
                "script": "First sentence. Go now! Final sentence.",
                "language": "en",
                "sources": [str(source)],
            }], _DictionaryWhisperModel({
                "first_sentence.mp4": "First sentence Final sentence",
            }), {"silence_detection_enabled": False})
            blockers = smart_video_export_blockers(bundle)
            self.assertTrue(any("Go now" in block["text"] for block in blockers))

    def test_mistokenized_opening_sentence_is_not_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "1_20260914113802.mp4"
            source.write_bytes(b"first")
            script = (
                "Nevynechaj to! Táto modlitba už zmenila život mnohých ľudí. "
                "Mnohí povedali, že keď ju prečítali a modlili sa, "
                "skutočne pocítili zmenu."
            )
            recognized = (
                "Neví nechajto, táto modlitba už zmennila život mnohých ľudí. "
                "Mnohý povedali, že keď ju prečítali a modlili sa "
                "skutočne pocítili zmenu."
            )
            bundle = analyze_smart_video_jobs([{
                "task_id": "212",
                "label": "212 | regression",
                "task_dir": str(root),
                "script": script,
                "language": "sk",
                "sources": [str(source)],
            }], _DictionaryWhisperModel({source.name: recognized}), {
                "silence_detection_enabled": False,
            })

            task = bundle["tasks"][0]
            clip = task["clips"][0]
            self.assertEqual(task["missing_blocks"], [])
            self.assertFalse(bundle["summary"]["export_blocked"])
            self.assertEqual(clip["script_word_start"], 0)
            self.assertEqual(text_units(clip["expected_text"]), text_units(script))
            self.assertTrue(any(
                issue.get("kind") == "word_difference"
                and "Nevynechaj to" in issue.get("expected", "")
                and "Neví nechajto" in issue.get("recognized", "")
                for issue in clip["issues"]
            ), clip["issues"])
            self.assertEqual(clip["status"], "pink")

    def test_wrong_words_stay_severe_review_without_becoming_a_missing_segment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "wrong_words.mp4"
            source.write_bytes(b"wrong")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-wrong-words",
                "label": "T-wrong-words",
                "task_dir": str(root),
                "script": "alpha beta gamma delta epsilon zeta eta theta",
                "language": "en",
                "sources": [str(source)],
            }], _DictionaryWhisperModel({
                "wrong_words.mp4": "alpha one two three epsilon zeta eta theta",
            }), {"silence_detection_enabled": False})
            clip = bundle["tasks"][0]["clips"][0]
            self.assertEqual(clip["status"], "pink")
            self.assertFalse(smart_video_export_blockers(bundle))

    def test_review_dialog_disables_export_and_lists_exact_missing_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_words.mp4"
            source.write_bytes(b"first")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-dialog-missing",
                "task_name": "dialog-missing",
                "label": "T-dialog-missing | dialog-missing",
                "task_dir": str(root),
                "script": "first words\nsecond sentence is absent",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            dialog = SmartVideoReviewDialog(bundle)
            try:
                self.assertFalse(dialog.export_button.isEnabled())
                self.assertFalse(dialog.blocker_banner.isHidden())
                self.assertEqual(dialog.missing_tree.topLevelItemCount(), 1)
                row = dialog.missing_tree.topLevelItem(0)
                self.assertEqual(row.text(0), "未处理")
                self.assertIn("second sentence is absent", row.text(3))
                self.assertIn("first_words.mp4", row.text(4))
            finally:
                dialog.close()

    def test_human_approval_allows_export_and_expires_after_edit_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_words.mp4"
            source.write_bytes(b"first")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-approved",
                "task_name": "approved",
                "label": "T-approved | approved",
                "task_dir": str(root),
                "script": "first words\nsecond sentence is absent",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            task = bundle["tasks"][0]
            set_smart_video_missing_review(task, "approved")
            self.assertFalse(smart_video_export_blockers(bundle))
            self.assertEqual(subtitle_text_for_export(task), task["script"])

            task["clips"][0]["trim_end"] -= 0.1
            self.assertTrue(smart_video_export_blockers(bundle))

    def test_review_dialog_can_approve_or_skip_a_missing_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_words.mp4"
            source.write_bytes(b"first")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-review-action",
                "label": "T-review-action",
                "task_dir": str(root),
                "script": "first words\nsecond sentence is absent",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            dialog = SmartVideoReviewDialog(bundle)
            try:
                dialog.missing_tree.setCurrentItem(
                    dialog.missing_tree.topLevelItem(0)
                )
                with mock.patch.object(
                    QtWidgets.QMessageBox,
                    "question",
                    return_value=QtWidgets.QMessageBox.Yes,
                ):
                    dialog._set_missing_decision("approved")
                self.assertTrue(dialog.export_button.isEnabled())
                self.assertEqual(
                    dialog.missing_tree.topLevelItem(0).text(0), "人工通过"
                )

                dialog.missing_tree.setCurrentItem(
                    dialog.missing_tree.topLevelItem(0)
                )
                dialog._set_missing_decision("skipped")
                self.assertTrue(dialog.export_button.isEnabled())
                self.assertEqual(dialog.export_button.text(), "导出其余任务")
            finally:
                dialog.close()

    def test_skipped_missing_task_does_not_block_other_exports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first_words.mp4"
            second = root / "gamma_delta.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            bundle = analyze_smart_video_jobs([
                {
                    "task_id": "T-skip",
                    "label": "T-skip",
                    "task_dir": str(root / "skip"),
                    "script": "first words\nsecond sentence is absent",
                    "language": "en",
                    "sources": [str(first)],
                },
                {
                    "task_id": "T-normal",
                    "label": "T-normal",
                    "task_dir": str(root / "normal"),
                    "script": "gamma delta",
                    "language": "en",
                    "sources": [str(second)],
                },
            ], _MappingWhisperModel(), {"silence_detection_enabled": False})
            set_smart_video_missing_review(bundle["tasks"][0], "skipped")
            with mock.patch(
                "model.SmartVideoEditor._resolve_ffmpeg", return_value="ffmpeg"
            ), mock.patch(
                "model.SmartVideoEditor._export_task",
                return_value={"task_id": "T-normal", "status": "completed"},
            ) as export_task:
                result = export_smart_video_bundle(bundle)
            self.assertEqual(len(result["skipped"]), 1)
            self.assertEqual(result["skipped"][0]["task_id"], "T-skip")
            self.assertEqual([item["task_id"] for item in result["completed"]], ["T-normal"])
            self.assertEqual(export_task.call_count, 1)

    def test_all_skipped_tasks_do_not_require_ffmpeg(self):
        lines, words = build_script_word_records("first words\nmissing full line")
        task = {
            "task_id": "T-all-skipped",
            "label": "T-all-skipped",
            "task_dir": "C:/missing-task",
            "script": "first words\nmissing full line",
            "script_lines": lines,
            "script_words": words,
            "clips": [{
                "source": "C:/first.mp4",
                "file_name": "first.mp4",
                "included": True,
                "export_order": 1,
                "trim_start": 0.0,
                "trim_end": 1.0,
                "script_word_start": 0,
                "script_word_end": 1,
                "word_timeline": [
                    {"script_word_index": 0, "anchor": True},
                    {"script_word_index": 1, "anchor": True},
                ],
            }],
        }
        set_smart_video_missing_review(task, "skipped")
        bundle = {"settings": {}, "tasks": [task]}
        with mock.patch("model.SmartVideoEditor._resolve_ffmpeg") as resolve:
            result = export_smart_video_bundle(bundle)
        resolve.assert_not_called()
        self.assertFalse(result["completed"])
        self.assertEqual(len(result["skipped"]), 1)

    def test_pending_review_records_are_persistent_and_reopenable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "first_words.mp4"
            source.write_bytes(b"first")
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-pending",
                "label": "T-pending",
                "task_dir": str(root),
                "script": "first words\nsecond sentence is absent",
                "language": "en",
                "sources": [str(source)],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            records = update_smart_video_pending_reviews([], bundle)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "pending")
            self.assertEqual(
                normalize_smart_video_pending_reviews(records), records
            )
            dialog = SmartVideoPendingDialog(records)
            try:
                self.assertEqual(dialog.tree.topLevelItemCount(), 1)
                self.assertIn("second sentence is absent", dialog.tree.topLevelItem(0).text(3))
            finally:
                dialog.close()

            set_smart_video_missing_review(bundle["tasks"][0], "approved")
            self.assertEqual(update_smart_video_pending_reviews(records, bundle), [])

    def test_excluding_unique_clip_immediately_blocks_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "first_words.mp4", root / "gamma_delta.mp4"]
            for source in sources:
                source.write_bytes(source.name.encode("utf-8"))
            bundle = analyze_smart_video_jobs([{
                "task_id": "T-exclude",
                "task_name": "exclude",
                "label": "T-exclude | exclude",
                "task_dir": str(root),
                "script": "first words\ngamma delta",
                "language": "en",
                "sources": [str(source) for source in sources],
            }], _MappingWhisperModel(), {"silence_detection_enabled": False})
            dialog = SmartVideoReviewDialog(bundle)
            try:
                self.assertTrue(dialog.export_button.isEnabled())
                dialog.table.item(1, dialog.COL_INCLUDE).setCheckState(
                    QtCore.Qt.Unchecked
                )
                self.app.processEvents()
                self.assertFalse(dialog.export_button.isEnabled())
                self.assertIn("gamma delta", dialog.missing_tree.topLevelItem(0).text(3))
            finally:
                dialog.close()

    def test_reviewed_trim_rebuilds_ranges_and_srt_uses_authoritative_text(self):
        clip = {
            "file_name": "clip.mp4",
            "included": True,
            "original_duration": 5.0,
            "trim_start": 0.0,
            "trim_end": 5.0,
            "pause_removals": [[1.0, 2.0]],
            "cues": [{"text": "wrong", "start": 0.5, "end": 4.5}],
        }
        apply_clip_review(clip, True, 0.2, 4.8, "correct task words")
        self.assertEqual(clip["kept_ranges"], [[0.2, 1.0], [2.0, 4.8]])
        task = {"clips": [clip]}
        cues = build_srt_cues(
            task,
            normalize_smart_video_editor_settings({"srt_max_words_per_block": 2}),
        )
        self.assertEqual([cue["text"] for cue in cues], ["correct task words"])

    def test_final_alignment_text_uses_included_reviewed_clips_in_export_order(self):
        task = {
            "script": "stale complete task text",
            "clips": [
                {
                    "source_index": 0,
                    "export_order": 2,
                    "included": True,
                    "expected_text": "second corrected part",
                },
                {
                    "source_index": 1,
                    "export_order": 1,
                    "included": True,
                    "expected_text": "first part",
                },
                {
                    "source_index": 2,
                    "export_order": 3,
                    "included": False,
                    "expected_text": "excluded bad take",
                },
            ],
        }
        self.assertEqual(
            subtitle_text_for_export(task),
            "first part\nsecond corrected part",
        )

    def test_final_alignment_preserves_full_script_when_old_clip_ranges_lost_edges(self):
        task = {
            "script": "Tí z vás full middle Vaša final words.",
            "clips": [
                {
                    "source_index": 0,
                    "export_order": 1,
                    "included": True,
                    "expected_text": "full middle",
                },
                {
                    "source_index": 1,
                    "export_order": 2,
                    "included": True,
                    "expected_text": "final words.",
                },
            ],
        }
        self.assertEqual(
            subtitle_text_for_export(task),
            "Tí z vás full middle Vaša final words.",
        )

    def test_proven_subtitle_path_reuses_loaded_model_for_forced_alignment(self):
        class FakeAlignmentResult:
            def merge_all_segments(self):
                return self

            def split_by_length(self, **_kwargs):
                return self

            def to_srt_vtt(self, output_path, **_kwargs):
                Path(output_path).write_text(
                    "1\n00:00:00,100 --> 00:00:01,200\ncorrect words\n",
                    encoding="utf-8",
                )

        loaded_model = object()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "aligned.srt"
            with mock.patch(
                "stable_whisper.alignment.align",
                return_value=FakeAlignmentResult(),
            ) as align_mock, mock.patch(
                "stable_whisper.load_faster_whisper"
            ) as load_mock:
                generate_srt_whisper_only(
                    "final.mp4",
                    "correct words",
                    str(output),
                    language="en",
                    max_words_per_block=3,
                    model=loaded_model,
                )
            load_mock.assert_not_called()
            self.assertIs(align_mock.call_args.args[0], loaded_model)
            self.assertEqual(align_mock.call_args.args[1], "final.mp4")
            self.assertEqual(align_mock.call_args.kwargs["text"], "correct words")
            self.assertTrue(output.is_file())

    def test_subtitle_sentence_split_preserves_original_terminal_period(self):
        self.assertEqual(
            smart_split_sentences("First sentence. Final sentence."),
            "First sentence.\nFinal sentence.",
        )
        self.assertEqual(
            smart_split_sentences("First sentence. Final sentence"),
            "First sentence.\nFinal sentence",
        )

    def test_program_settings_exposes_smart_editor_without_touching_subtitle_values(self):
        dialog = MainSettingDialog()
        try:
            self.assertGreaterEqual(
                dialog.settingTabWidget.indexOf(dialog.smart_video_editor_tab), 0
            )
            dialog.smart_lead_padding_spinbox.setValue(180)
            dialog.smart_silence_db_spinbox.setValue(-42)
            settings = dialog._get_smart_video_editor_settings()
            self.assertEqual(settings["lead_padding_ms"], 180)
            self.assertEqual(settings["silence_threshold_db"], -42)
        finally:
            dialog.close()

    def test_program_settings_exposes_output_filename_length(self):
        dialog = MainSettingDialog()
        try:
            dialog._load_task_result_config({})
            self.assertEqual(
                dialog.task_result_filename_max_length_spinbox.value(),
                50,
            )
            dialog.task_result_filename_max_length_spinbox.setValue(72)
            self.assertEqual(
                dialog._get_task_result_config()[
                    "task_output_filename_max_length"
                ],
                72,
            )
        finally:
            dialog.close()

    def test_source_and_review_dialogs_keep_user_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "clip.mp4"
            source.write_bytes(b"fake")
            jobs = [{
                "task_id": "1",
                "label": "1 | demo",
                "task_dir": temporary,
                "script": "correct words",
                "sources": [str(source)],
            }]
            source_dialog = SmartVideoSourceDialog(jobs)
            try:
                source_dialog.accept()
                self.assertEqual(source_dialog.selected_jobs[0]["sources"], [str(source)])
            finally:
                source_dialog.close()

            bundle = {
                "settings": normalize_smart_video_editor_settings({}),
                "summary": {
                    "clip_count": 1,
                    "green_count": 0,
                    "orange_count": 1,
                    "pink_count": 0,
                },
                "tasks": [{
                    "label": "1 | demo",
                    "report_path": str(Path(temporary) / "report.json"),
                    "clips": [{
                        "source": str(source),
                        "file_name": source.name,
                        "status": "orange",
                        "issue_reason": "check",
                        "similarity": 0.65,
                        "trim_start": 0.1,
                        "trim_end": 2.0,
                        "original_duration": 2.2,
                        "pause_removals": [],
                        "removed_seconds": 0.0,
                        "kept_ranges": [[0.1, 2.0]],
                        "expected_text": "correct words",
                        "recognized_text": "correct word",
                        "issues": [{
                            "severity": "orange",
                            "kind": "word_difference",
                            "start": 0.8,
                            "end": 1.1,
                            "line_start": 1,
                            "line_end": 1,
                            "title": "单词不一致",
                            "expected": "words",
                            "recognized": "word",
                            "detail": "正确文案与视频在此处出现替换或错读。",
                        }],
                        "cues": [{
                            "text": "correct words",
                            "start": 0.2,
                            "end": 1.8,
                        }],
                        "included": True,
                    }],
                }],
            }
            review_dialog = SmartVideoReviewDialog(bundle)
            try:
                reviewed = review_dialog.collect()
                self.assertTrue(reviewed["tasks"][0]["clips"][0]["included"])
                self.assertFalse(review_dialog.table.isColumnHidden(
                    review_dialog.COL_ISSUES
                ))
                self.assertTrue(review_dialog.table.isColumnHidden(
                    review_dialog.COL_EXPECTED
                ))
                self.assertEqual(review_dialog.issue_tree.topLevelItemCount(), 1)
                self.assertIn(
                    "0.800-1.100s",
                    review_dialog.issue_tree.topLevelItem(0).text(1),
                )
            finally:
                review_dialog.close()

    def test_readable_problem_report_contains_time_and_word_difference(self):
        task = {
            "label": "T | demo",
            "clips": [{
                "export_order": 1,
                "source_index": 0,
                "file_name": "clip.mp4",
                "status": "orange",
                "trim_start": 0.0,
                "trim_end": 2.0,
                "original_duration": 2.2,
                "expected_text": "correct words",
                "recognized_text": "correct word",
                "issues": [{
                    "severity": "orange",
                    "title": "单词不一致",
                    "start": 0.8,
                    "end": 1.1,
                    "expected": "words",
                    "recognized": "word",
                    "detail": "此处错读。",
                }],
            }],
            "missing_blocks": [],
        }
        report = render_task_problem_report(task, {})
        self.assertIn("0.800-1.100 秒", report)
        self.assertIn("应为「words」", report)
        self.assertIn("识别为「word」", report)

    def test_ffmpeg_export_creates_trimmed_video_and_matching_srt(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            subprocess.run([
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:r=25:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=2",
                "-shortest",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(source),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            settings = normalize_smart_video_editor_settings({
                "ffmpeg_path": ffmpeg,
                "output_folder_name": "out",
                "existing_output": "overwrite",
                "srt_max_words_per_block": 3,
                "srt_include_line_breaks": True,
                "srt_block_gap_ms": 0,
            })
            bundle = {
                "settings": settings,
                "tasks": [{
                    "task_id": "T1",
                    "task_name": "demo",
                    "label": "T1 | demo",
                    "output_dir": str(root / "out"),
                    "script_lines": ["authoritative task text"],
                    "script_words": build_script_word_records(
                        "authoritative task text"
                    )[1],
                    "clips": [{
                        "source": str(source),
                        "file_name": source.name,
                        "included": True,
                        "kept_ranges": [[0.0, 0.5], [1.5, 2.0]],
                        "removed_seconds": 1.0,
                        "source_index": 0,
                        "export_order": 1,
                        "script_word_start": 0,
                        "script_word_end": 2,
                        "word_timeline": [
                            {"script_word_index": 0, "line_index": 0, "start": 0.1, "end": 0.3},
                            {"script_word_index": 1, "line_index": 0, "start": 0.3, "end": 1.7},
                            {"script_word_index": 2, "line_index": 0, "start": 1.7, "end": 1.9},
                        ],
                    }],
                }],
            }
            alignment_calls = []

            def fake_generate_srt(audio_path, text, output_path, **kwargs):
                alignment_calls.append((audio_path, text, output_path, kwargs))
                Path(output_path).write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\n"
                    f"{text}\n",
                    encoding="utf-8",
                )

            with mock.patch(
                "model.SubtitleHelper.generate_srt_whisper_only",
                side_effect=fake_generate_srt,
            ):
                result = export_smart_video_bundle(bundle, settings)
            self.assertFalse(result["failed"])
            item = result["completed"][0]
            self.assertTrue(Path(item["video"]).is_file())
            self.assertEqual(item["subtitle_alignment"], "final_media_stable_whisper")
            srt_text = Path(item["srt"]).read_text(encoding="utf-8-sig")
            self.assertIn("authoritative task text", srt_text)
            self.assertEqual(len(alignment_calls), 1)
            aligned_media, aligned_text, _output_path, aligned_options = alignment_calls[0]
            self.assertEqual(Path(aligned_media), Path(item["video"]))
            self.assertEqual(aligned_text, "authoritative task text")
            self.assertTrue(aligned_options["include_line_breaks"])
            self.assertEqual(aligned_options["max_words_per_block"], 3)
            self.assertEqual(aligned_options["block_gap_ms"], 0)
            ffprobe = shutil.which("ffprobe")
            if ffprobe:
                probe = subprocess.run([
                    ffprobe,
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=start_time,has_b_frames",
                    "-of", "json",
                    item["video"],
                ], check=True, capture_output=True, text=True)
                timing = json.loads(probe.stdout)["streams"][0]
                self.assertAlmostEqual(float(timing["start_time"]), 0.0, places=3)
                self.assertEqual(int(timing["has_b_frames"]), 0)


if __name__ == "__main__":
    unittest.main()
