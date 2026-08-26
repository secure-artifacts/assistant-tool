import hashlib
import json
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path


API_KEY_STATUSES_CONFIG_KEY = 'elevenlabs_api_key_statuses'
SUBSCRIPTION_URL = 'https://api.elevenlabs.io/v1/user/subscription'


def normalize_api_keys(value):
    """将单个 Key、Key 列表或分隔文本规范化为去重后的列表。"""
    if value is None:
        return []

    if isinstance(value, str):
        values = re.split(r'[\r\n,;]+', value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]

    keys = []
    seen = set()
    for item in values:
        key = str(item).strip()
        if key and key not in seen:
            keys.append(key)
            seen.add(key)
    return keys


def rotate_api_keys(api_keys, start_index=0):
    """从指定位置开始循环排列 Key，供每次任务轮换起始 Key。"""
    keys = normalize_api_keys(api_keys)
    if not keys:
        return []

    start_index %= len(keys)
    return keys[start_index:] + keys[:start_index]


def api_key_id(api_key):
    """返回不暴露 Key 内容的稳定标识。"""
    return hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:20]


def get_api_key_status(api_key_statuses, api_key):
    if not isinstance(api_key_statuses, dict):
        return {}
    status = api_key_statuses.get(api_key_id(api_key), {})
    return status if isinstance(status, dict) else {}


def prune_api_key_statuses(api_key_statuses, api_keys):
    """删除已经不在 Key 列表中的状态记录。"""
    if not isinstance(api_key_statuses, dict):
        return {}
    valid_ids = {api_key_id(key) for key in normalize_api_keys(api_keys)}
    return {
        key_id: status
        for key_id, status in api_key_statuses.items()
        if key_id in valid_ids and isinstance(status, dict)
    }


def _timestamp(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def format_unix_time(timestamp):
    timestamp = _timestamp(timestamp)
    if timestamp <= 0:
        return '未知时间'
    return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')


def describe_api_key_status(api_key_statuses, api_key, now=None):
    """生成适合显示在设置列表中的 Key 状态文字。"""
    now = int(time.time() if now is None else now)
    key_status = get_api_key_status(api_key_statuses, api_key)
    status = key_status.get('status', 'unknown')

    if status == 'available':
        return '可用'
    if status == 'exhausted':
        reset_unix = _timestamp(key_status.get('reset_unix'))
        if reset_unix > now:
            return f"额度已用完｜{format_unix_time(reset_unix)} 刷新"
        return '刷新时间已到｜等待重新测试'
    if status == 'invalid':
        return 'Key 无效｜已停止尝试'
    if status == 'cooldown':
        retry_after = _timestamp(key_status.get('retry_after_unix'))
        if retry_after > now:
            return f"暂时不可用｜{format_unix_time(retry_after)} 后重试"
        return '冷却结束｜等待重新测试'
    return '未检测'


def select_api_keys_for_attempt(api_keys, api_key_statuses, start_index=0,
                                now=None):
    """
    按已记录状态筛选 Key。

    有可用 Key 时轮换；全部额度耗尽时不盲目请求，而是在最早刷新时间到达后
    只让距离刷新时间最近的 Key 先恢复测试。
    """
    now = int(time.time() if now is None else now)
    keys = normalize_api_keys(api_keys)
    available = []
    refreshed = []
    exhausted = []
    cooling_down = []
    invalid = []

    for api_key in keys:
        key_status = get_api_key_status(api_key_statuses, api_key)
        status = key_status.get('status', 'unknown')

        if status == 'exhausted':
            reset_unix = _timestamp(key_status.get('reset_unix'))
            if reset_unix > now:
                exhausted.append((reset_unix, api_key))
            else:
                refreshed.append((reset_unix, api_key))
        elif status == 'invalid':
            invalid.append(api_key)
        elif status == 'cooldown':
            retry_after = _timestamp(key_status.get('retry_after_unix'))
            if retry_after > now:
                cooling_down.append((retry_after, api_key))
            else:
                available.append(api_key)
        else:
            available.append(api_key)

    # 已到刷新时间的 Key 按刷新时间先后测试。
    available.extend(
        api_key for _, api_key in sorted(refreshed, key=lambda item: item[0])
    )

    if available:
        selected = rotate_api_keys(available, start_index)
        next_index = (start_index + 1) % len(available)
        return selected, next_index, {'mode': 'available'}

    if exhausted:
        reset_unix, api_key = min(exhausted, key=lambda item: item[0])
        return [], start_index, {
            'mode': 'waiting_exhausted',
            'next_key': api_key,
            'next_retry_unix': reset_unix,
        }

    if cooling_down:
        retry_after, api_key = min(cooling_down, key=lambda item: item[0])
        return [], start_index, {
            'mode': 'waiting_cooldown',
            'next_key': api_key,
            'next_retry_unix': retry_after,
        }

    return [], start_index, {
        'mode': 'no_keys' if not keys else 'no_usable_keys',
        'invalid_count': len(invalid),
    }


def record_api_key_status(config_path, api_key, **updates):
    """将 Key 状态原子写入配置文件；状态索引不包含原始 Key。"""
    config_path = Path(config_path)
    if config_path.exists():
        with config_path.open('r', encoding='utf-8') as config_file:
            config = json.load(config_file)
    else:
        config = {}

    api_key_statuses = config.get(API_KEY_STATUSES_CONFIG_KEY)
    if not isinstance(api_key_statuses, dict):
        api_key_statuses = {}

    key_id = api_key_id(api_key)
    key_status = api_key_statuses.get(key_id)
    if not isinstance(key_status, dict):
        key_status = {}

    for name, value in updates.items():
        if value is None:
            key_status.pop(name, None)
        else:
            key_status[name] = value

    api_key_statuses[key_id] = key_status
    config[API_KEY_STATUSES_CONFIG_KEY] = api_key_statuses

    temp_path = config_path.with_name(f"{config_path.name}.tmp")
    with temp_path.open('w', encoding='utf-8') as config_file:
        json.dump(config, config_file, indent=4, ensure_ascii=False)
    os.replace(temp_path, config_path)


def get_subscription_snapshot(api_key, timeout=10):
    """读取 ElevenLabs 官方订阅接口返回的额度与精确刷新时间。"""
    request = urllib.request.Request(
        SUBSCRIPTION_URL,
        headers={
            'Accept': 'application/json',
            'xi-api-key': api_key,
        },
        method='GET',
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode('utf-8'))

    return {
        'character_count': data.get('character_count'),
        'character_limit': data.get('character_limit'),
        'reset_unix': _timestamp(data.get('next_character_count_reset_unix')),
    }
