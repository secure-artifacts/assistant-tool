import random
import asyncio
import math
import os
import random
import re
import time
from pathlib import Path

import edge_tts
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from pydub import AudioSegment, silence

from model.ApiKeyHelper import (
    get_subscription_snapshot,
    normalize_api_keys,
    record_api_key_status,
)
from model.TextHelper import smart_split_sentences

from elevenlabs import VoiceSettings


# ElevenLabs API key 将由调用方传入，符合单一职责原则


def _mask_api_key(api_key):
    if len(api_key) <= 10:
        return '*' * len(api_key)
    return f"{api_key[:6]}...{api_key[-4:]}"


def _api_error_details(error):
    body = getattr(error, 'body', None)
    if isinstance(body, dict):
        detail = body.get('detail', body)
    else:
        detail = {}
    if not isinstance(detail, dict):
        detail = {}

    return {
        'type': str(detail.get('type', '')).lower(),
        'code': str(detail.get('code', '')).lower(),
        'status': str(detail.get('status', '')).lower(),
        'message': str(detail.get('message') or error)[:500],
        'status_code': getattr(error, 'status_code', None),
    }


def _classify_api_error(error):
    details = _api_error_details(error)
    combined = ' '.join(
        str(details[name]).lower()
        for name in ('type', 'code', 'status', 'message')
    )

    if (
        details['status_code'] == 402
        or details['type'] == 'payment_required'
        or any(marker in combined for marker in (
            'quota_exceeded', 'insufficient_credits',
            'insufficient quota', 'credit quota',
        ))
    ):
        return 'quota', details

    if (
        details['status_code'] == 401
        or details['type'] == 'authentication_error'
        or any(marker in combined for marker in (
            'invalid_api_key', 'missing_api_key', 'invalid api key',
        ))
    ):
        return 'invalid', details

    if (
        details['status_code'] == 429
        or details['type'] == 'rate_limit_error'
        or any(marker in combined for marker in (
            'rate_limit_exceeded', 'concurrent_limit_exceeded',
            'system_busy',
        ))
    ):
        return 'rate_limit', details

    if (
        isinstance(details['status_code'], int)
        and details['status_code'] >= 500
    ) or any(marker in combined for marker in (
        'service_unavailable', 'internal_error', 'maintenance',
    )):
        return 'temporary', details

    return 'request', details


def _record_status(config_path, api_key, **updates):
    if not config_path:
        return
    try:
        record_api_key_status(config_path, api_key, **updates)
    except Exception as e:
        print(f"记录 API Key 状态失败: {e}")


def _record_quota_exhausted(config_path, api_key, details):
    now = int(time.time())
    reset_unix = 0
    reset_source = 'subscription_api'
    character_count = None
    character_limit = None

    try:
        subscription = get_subscription_snapshot(api_key)
        reset_unix = subscription.get('reset_unix', 0)
        character_count = subscription.get('character_count')
        character_limit = subscription.get('character_limit')
    except Exception as e:
        # 如果状态接口临时不可用，隔天只探测一次，避免把 Key 错封一个月。
        reset_unix = now + 24 * 60 * 60
        reset_source = 'fallback_daily_probe'
        print(f"未能读取精确额度刷新时间，将在 24 小时后探测: {e}")

    if reset_unix <= now:
        reset_unix = now + 24 * 60 * 60
        reset_source = 'fallback_daily_probe'

    _record_status(
        config_path,
        api_key,
        status='exhausted',
        reset_unix=reset_unix,
        reset_source=reset_source,
        character_count=character_count,
        character_limit=character_limit,
        last_failure_unix=now,
        last_error_code=details.get('code') or details.get('status') or 'quota_exceeded',
        last_error_message=details.get('message', ''),
        retry_after_unix=None,
    )
    return reset_unix


def _save_audio_with_api_keys(api_keys, filename, convert_audio,
                              api_key_status_config='config.json'):
    """依次尝试 Key，并仅在音频完整生成后替换目标文件。"""
    temp_filename = f"{filename}.part"

    for index, api_key in enumerate(api_keys, 1):
        try:
            if os.path.exists(temp_filename):
                os.remove(temp_filename)

            print(
                f"正在尝试 ElevenLabs API Key "
                f"{index}/{len(api_keys)} ({_mask_api_key(api_key)})"
            )
            elevenlabs_client = ElevenLabs(api_key=api_key)
            audio = convert_audio(elevenlabs_client)

            has_audio = False
            with open(temp_filename, "wb") as f:
                for chunk in audio:
                    if chunk:
                        f.write(chunk)
                        has_audio = True

            if not has_audio:
                raise RuntimeError("ElevenLabs 返回了空音频")

            os.replace(temp_filename, filename)
            _record_status(
                api_key_status_config,
                api_key,
                status='available',
                last_success_unix=int(time.time()),
                last_error_code=None,
                last_error_message=None,
                reset_unix=None,
                reset_source=None,
                retry_after_unix=None,
            )
            print(f"Audio saved to {filename}")
            return True
        except Exception as e:
            if os.path.exists(temp_filename):
                os.remove(temp_filename)

            error_type, details = _classify_api_error(e)
            now = int(time.time())

            if error_type == 'quota':
                reset_unix = _record_quota_exhausted(
                    api_key_status_config, api_key, details
                )
                print(
                    f"API Key {_mask_api_key(api_key)} 额度已用完，"
                    f"刷新时间 {time.strftime('%Y-%m-%d %H:%M', time.localtime(reset_unix))}；"
                    f"尝试下一个 Key"
                )
                continue

            if error_type == 'invalid':
                _record_status(
                    api_key_status_config,
                    api_key,
                    status='invalid',
                    last_failure_unix=now,
                    last_error_code=details.get('code') or details.get('status') or 'invalid_api_key',
                    last_error_message=details.get('message', ''),
                    retry_after_unix=None,
                )
                print(
                    f"API Key {_mask_api_key(api_key)} 无效，已停止后续尝试；"
                    f"尝试下一个 Key"
                )
                continue

            if error_type == 'rate_limit':
                retry_after_unix = now + 5 * 60
                _record_status(
                    api_key_status_config,
                    api_key,
                    status='cooldown',
                    retry_after_unix=retry_after_unix,
                    last_failure_unix=now,
                    last_error_code=details.get('code') or details.get('status') or 'rate_limit',
                    last_error_message=details.get('message', ''),
                )
                print(
                    f"API Key {_mask_api_key(api_key)} 暂时限流，冷却 5 分钟；"
                    f"尝试下一个 Key"
                )
                continue

            if error_type == 'temporary':
                _record_status(
                    api_key_status_config,
                    api_key,
                    status='cooldown',
                    retry_after_unix=now + 5 * 60,
                    last_failure_unix=now,
                    last_error_code=details.get('code') or details.get('status') or 'temporary_error',
                    last_error_message=details.get('message', ''),
                )

            # 参数、模型、Voice 或服务故障并非换 Key 能解决，避免遍历全部 Key。
            print(
                f"音频生成失败（{_mask_api_key(api_key)}）: {e}；"
                f"本次不再反复尝试其他 Key"
            )
            break

    print("错误: 没有可用的 ElevenLabs API Key 完成音频生成")
    return False


def CreateAudio(text, filename, voice_id=None, model_id=None, speed=1.0,
                pitch=1.0, api_key=None, api_keys=None,
                api_key_status_config='config.json'):
    """
    创建 ElevenLabs 语音文件

    Args:
        text: 要转换的文本
        filename: 输出文件名
        voice_id: 声音ID
        model_id: 模型ID，默认为 "eleven_flash_v2_5"
        speed: 语速 (0.25-4.0，1.0为正常速度)
        pitch: 音调 (0.5-2.0，1.0为正常音调)
        api_key: 兼容旧调用方式的单个 ElevenLabs API key
        api_keys: 按顺序尝试的 ElevenLabs API key 列表
    """
    keys = normalize_api_keys(api_keys)
    if not keys:
        keys = normalize_api_keys(api_key)
    if not keys:
        print("错误: 未提供 ElevenLabs API key")
        return False

    text = smart_split_sentences(text)
    if voice_id is None:
        print("声音类型未传入")
        return None

    if model_id is None:
        model_id = "eleven_flash_v2_5"

    # 创建语音设置对象
    from elevenlabs import VoiceSettings

    voice_settings = VoiceSettings(
        stability=0.5,  # 稳定性 (0-1)
        similarity_boost=0.75,  # 相似度增强 (0-1)
        style=0.0,  # 风格 (0-1)
        use_speaker_boost=True,  # 使用说话者增强
        speed=speed,  # 语速 (0.25-4.0，1.0为正常速度)
        pitch=pitch  # 音调 (0.5-2.0，1.0为正常音调)
    )

    def convert_audio(elevenlabs_client):
        return elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id=model_id,
            output_format="mp3_44100_128",
            voice_settings=voice_settings
        )

    return _save_audio_with_api_keys(
        keys,
        filename,
        convert_audio,
        api_key_status_config=api_key_status_config,
    )


def CreateAudio3(
        text: str,
        filename: str,
        voice_id: str = None,
        stability: float = 0.35,  # v3: Creative(低)=更有表现力, Robust(高)=更稳定
        api_key: str = None,
        api_keys=None,
        api_key_status_config='config.json'
):
    """
    使用 ElevenLabs eleven_v3 模型，自动注入 Audio Tags 实现情感表演。
    斯洛伐克语直接传入即可，v3 原生支持。

    stability 建议:
      0.25~0.45 → Creative (表现力强，推荐叙事/对话)
      0.50~0.65 → Natural  (均衡)
      0.70+     → Robust   (稳定但表现力弱)
    """

    keys = normalize_api_keys(api_keys)
    if not keys:
        keys = normalize_api_keys(api_key)
    if not keys:
        print("错误: 未提供 ElevenLabs API key")
        return False

    if voice_id is None:
        print("声音类型未传入")
        return None

    # v3 的核心参数只有 stability，speed/pitch 已被 tag 系统取代
    voice_settings = VoiceSettings(
        stability=stability,
        similarity_boost=0.75,
        style=0.5,
        use_speaker_boost=True,
    )

    def convert_audio(elevenlabs_client):
        return elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id="eleven_v3",  # 最新模型，固定
            output_format="mp3_44100_128",
            voice_settings=voice_settings,
        )

    return _save_audio_with_api_keys(
        keys,
        filename,
        convert_audio,
        api_key_status_config=api_key_status_config,
    )


def CreateTTSAudio(text, voice_type, filename, rate="+0%", pitch="+0Hz"):
    # --- 新增步骤：简单的自动断行 ---
    # 逻辑：找到 . ! ? 以及中文的 。 ！？，在它们后面强制加一个换行符 \n
    # 这样 TTS 读到这里会有一个自然的停顿
    text = re.sub(r'([.!?。！？])', r'\1\n', text)

    # 1. 定义声音字典
    voice_id_dict = {
        "男声": ["sk-SK-LukasNeural", "Adam Multilingual"],
        "女声": ["sk-SK-ViktoriaNeural"],
    }

    if voice_type not in voice_id_dict:
        print("声音类型未传入")
        return None

    # 2. 获取对应的 Voice ID
    voice_id = voice_id_dict[voice_type][0]

    # 3. 定义异步生成函数
    async def _generate_audio():
        # Communicate 会把 \n 当作自然的短停顿处理
        communicate = edge_tts.Communicate(text, voice_id, rate=rate, pitch=pitch)
        await communicate.save(filename)

    # 4. 运行异步任务
    try:
        asyncio.run(_generate_audio())
        print(f"成功生成语音文件: {filename}")
    except Exception as e:
        print(f"生成失败: {e}")


def mergeSplitPoints(p_list, max_len):
    if not p_list or len(p_list) < 2:
        return p_list

    result = [p_list[0]]
    p_begin = p_list[0]
    p_end = p_list[1]

    for p in p_list[1:]:
        # 如果超过最大段长 -> 切一个段
        if p - p_begin > max_len:
            result.append(p_end)
            p_begin = p_end  # 重新开始计算新段起点
        p_end = p

    # 保证最后一个点也加入
    if result[-1] != p_list[-1]:
        result.append(p_list[-1])

    return result


def splitAudio(input_file: Path, max_length, ultra_tolerance=None):
    if ultra_tolerance is None:
        ultra_tolerance = max_length

    output_dir = str(input_file.parent.absolute())
    audio = AudioSegment.from_file(input_file)

    # Detect silent ranges
    silent_ranges = silence.detect_silence(
        audio,
        min_silence_len=600,  # silence longer than 0.6s
        silence_thresh=audio.dBFS - 16
    )
    if audio.duration_seconds < max_length / 1000:
        print(f"{input_file}无需切割。")
        return

    if audio.duration_seconds < ultra_tolerance / 1000:
        print(f"{input_file} 时长：{audio.duration_seconds}，可以容忍。")
        return

    # Convert to split points
    split_points = [0]
    for start, end in silent_ranges:
        if end - split_points[-1] > max_length:
            split_points.append(split_points[-1] + max_length)
        split_points.append(end)
    split_points.append(len(audio))

    split_points = mergeSplitPoints(split_points, max_length)

    index = 0

    # Export chunks
    for i in range(len(split_points) - 1):
        start = split_points[i]
        end = split_points[i + 1]
        chunk = audio[start:end]

        # Final safety cut if still too long
        for sub_i in range(math.ceil(len(chunk) / max_length)):
            index += 1
            sub = chunk[sub_i * max_length:(sub_i + 1) * max_length]
            file = f"{output_dir}/片段{index}.mp3"
            # 关键：指定高质量参数
            sub.export(
                file,
                format="mp3",
                bitrate="320k",  # 最高质量
                parameters=["-q:a", "0"]  # FFmpeg 质量参数：0 最高
            )
            print("Saved:", file)

