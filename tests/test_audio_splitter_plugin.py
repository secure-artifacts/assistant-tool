import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydub import AudioSegment

from app_plugins.builtin.audio_splitter import (
    collect_audio_files,
    merge_split_points,
    normalize_audio_splitter_settings,
    split_audio_file,
)


class AudioSplitterCoreTests(unittest.TestCase):
    def test_normalizes_limits_extensions_and_legacy_defaults(self):
        settings = normalize_audio_splitter_settings({
            "max_length_seconds": 45,
            "tolerance_seconds": 20,
            "extensions": "MP3, wav; .MP3",
        })
        self.assertEqual(settings["max_length_seconds"], 45)
        self.assertEqual(settings["tolerance_seconds"], 45)
        self.assertEqual(settings["extensions"], [".mp3", ".wav"])

    def test_collects_supported_files_recursively_without_generated_chunks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "nested"
            nested.mkdir()
            wanted = root / "voice.mp3"
            nested_wanted = nested / "take.wav"
            generated = root / "片段1.mp3"
            generated_named = root / "voice_片段2.mp3"
            ignored = root / "notes.txt"
            for path in (wanted, nested_wanted, generated, generated_named, ignored):
                path.touch()

            result = collect_audio_files([root, wanted], {
                "extensions": [".mp3", ".wav"],
                "recursive": True,
            })

        self.assertEqual({path.name for path in result}, {"voice.mp3", "take.wav"})

    def test_short_audio_is_skipped_without_exporting(self):
        fake_audio = AudioSegment.silent(duration=3000)
        with patch(
            "app_plugins.builtin.audio_splitter.AudioSegment.from_file",
            return_value=fake_audio,
        ):
            result = split_audio_file("voice.mp3", {
                "max_length_seconds": 30,
                "tolerance_seconds": 30,
            })
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["outputs"], [])

    def test_long_audio_falls_back_to_max_length_chunks(self):
        fake_audio = AudioSegment.silent(duration=65000)
        with patch(
            "app_plugins.builtin.audio_splitter.AudioSegment.from_file",
            return_value=fake_audio,
        ), patch(
            "app_plugins.builtin.audio_splitter.silence.detect_silence",
            return_value=[],
        ), patch.object(AudioSegment, "export") as export:
            result = split_audio_file("voice.mp3", {
                "max_length_seconds": 30,
                "tolerance_seconds": 30,
                "include_source_name": True,
            })
        self.assertEqual(result["status"], "split")
        self.assertEqual(
            [Path(path).name for path in result["outputs"]],
            ["voice_片段1.mp3", "voice_片段2.mp3", "voice_片段3.mp3"],
        )
        self.assertEqual(export.call_count, 3)

    def test_merge_points_keeps_segments_within_requested_range(self):
        self.assertEqual(
            merge_split_points([0, 8000, 17000, 26000, 35000], 20000),
            [0, 17000, 35000],
        )


if __name__ == "__main__":
    unittest.main()
