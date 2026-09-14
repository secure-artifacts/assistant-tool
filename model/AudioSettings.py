import copy
import json


SUPPORTED_AUDIO_MODELS = ("edge", "elevenlabs", "elevenlabs3")
KNOWN_PROFILE_FIELDS = frozenset(
    {
        "model",
        "sex",
        "speed",
        "pitch",
        "stability",
        "voices",
        "voice_ids",
    }
)


class AudioSettingsError(ValueError):
    pass


def normalize_audio_settings(value):
    if not isinstance(value, dict):
        return {}
    return {
        str(name).strip(): copy.deepcopy(profile)
        for name, profile in value.items()
        if str(name).strip() and isinstance(profile, dict)
    }


def _format_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return "{:g}".format(number)


def voice_lines_from_profile(profile):
    profile = profile if isinstance(profile, dict) else {}
    model = str(profile.get("model") or "").strip()
    if model == "elevenlabs":
        entries = profile.get("voices") or profile.get("voice_ids") or []
        lines = []
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                voice_id = str(entry.get("id") or "").strip()
                speed = _format_number(entry.get("speed"))
            else:
                voice_id = str(entry or "").strip()
                speed = ""
            if voice_id:
                lines.append(
                    "{} | {}".format(voice_id, speed) if speed else voice_id
                )
        return "\n".join(lines)

    entries = profile.get("voice_ids") or []
    if not isinstance(entries, list):
        return ""
    return "\n".join(str(value).strip() for value in entries if str(value).strip())


def parse_voice_lines(text, model):
    result = []
    for line_number, raw_line in enumerate(str(text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if model == "elevenlabs":
            parts = [part.strip() for part in line.split("|", 1)]
            voice_id = parts[0]
            if not voice_id:
                raise AudioSettingsError("第 {} 行缺少 Voice ID".format(line_number))
            entry = {"id": voice_id}
            if len(parts) == 2 and parts[1]:
                try:
                    speed = float(parts[1])
                except ValueError as error:
                    raise AudioSettingsError(
                        "第 {} 行的语速不是数字".format(line_number)
                    ) from error
                if speed <= 0:
                    raise AudioSettingsError("语速必须大于 0")
                entry["speed"] = speed
            result.append(entry)
        else:
            result.append(line)
    return result


def extra_settings_json(profile):
    profile = profile if isinstance(profile, dict) else {}
    extra = {
        key: copy.deepcopy(value)
        for key, value in profile.items()
        if key not in KNOWN_PROFILE_FIELDS
    }
    return json.dumps(extra, ensure_ascii=False, indent=2)


def parse_extra_settings(text):
    source = str(text or "").strip()
    if not source:
        return {}
    try:
        value = json.loads(source)
    except json.JSONDecodeError as error:
        raise AudioSettingsError(
            "其他参数 JSON 第 {} 行格式错误：{}".format(error.lineno, error.msg)
        ) from error
    if not isinstance(value, dict):
        raise AudioSettingsError("其他参数必须是 JSON 对象")
    overlap = sorted(KNOWN_PROFILE_FIELDS.intersection(value))
    if overlap:
        raise AudioSettingsError(
            "这些常用参数请在上方填写，不要放入其他参数：{}".format(
                "、".join(overlap)
            )
        )
    return value


def build_audio_profile(
    model,
    sex="",
    speed=1.0,
    pitch="+0Hz",
    stability=0.35,
    voice_lines="",
    extra_json="",
    original_profile=None,
):
    model = str(model or "").strip()
    if model not in SUPPORTED_AUDIO_MODELS:
        raise AudioSettingsError("不支持的音频模型：{}".format(model or "空"))

    profile = parse_extra_settings(extra_json)
    profile["model"] = model
    sex = str(sex or "").strip()
    if sex:
        profile["sex"] = sex

    if model == "edge":
        speed = float(speed)
        if speed <= 0:
            raise AudioSettingsError("语速必须大于 0")
        profile["speed"] = speed
        profile["pitch"] = str(pitch or "+0Hz").strip() or "+0Hz"
        return profile

    voices = parse_voice_lines(voice_lines, model)
    if not voices:
        raise AudioSettingsError("请至少填写一个 Voice ID")

    if model == "elevenlabs":
        speed = float(speed)
        if speed <= 0:
            raise AudioSettingsError("语速必须大于 0")
        original_voice_extras = {}
        original_profile = (
            original_profile if isinstance(original_profile, dict) else {}
        )
        for entry in original_profile.get("voices") or []:
            if not isinstance(entry, dict):
                continue
            voice_id = str(entry.get("id") or "").strip()
            if voice_id:
                original_voice_extras[voice_id] = {
                    key: copy.deepcopy(value)
                    for key, value in entry.items()
                    if key not in {"id", "speed"}
                }
        voices = [
            dict(original_voice_extras.get(entry["id"], {}), **entry)
            for entry in voices
        ]
        profile["speed"] = speed
        profile["voices"] = voices
    else:
        stability = float(stability)
        if not 0 <= stability <= 1:
            raise AudioSettingsError("稳定性必须在 0 到 1 之间")
        profile["stability"] = stability
        profile["voice_ids"] = voices
    return profile


def audio_profile_voice_count(profile):
    profile = profile if isinstance(profile, dict) else {}
    model = str(profile.get("model") or "").strip()
    if model == "elevenlabs":
        entries = profile.get("voices") or profile.get("voice_ids") or []
    else:
        entries = profile.get("voice_ids") or []
    return len(entries) if isinstance(entries, list) else 0
