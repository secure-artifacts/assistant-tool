import json

from faster_whisper import WhisperModel

from app_paths import APP_ROOT


class GlobalValue:
    def __init__(self):
        self.__whisper_model = None

    def videoSortingStationPath(self):
        config_path = APP_ROOT / "config.json"
        if not config_path.exists():
            return ""
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return ""
        return str(config.get("video_sorting_station_path") or "").strip()

    def get_whisper_model(self):
        if self.__whisper_model is None:

            # 强制使用 CPU，避免 CUDA 问题
            self.__whisper_model = WhisperModel(
                "base",
                device="cpu",  # 强制 CPU
                compute_type="int8"
            )

        return self.__whisper_model


globalValue = GlobalValue()
