import threading
import time

from PyQt5.QtCore import QThread, pyqtSignal


DEFAULT_MUSIC_DUCKER_SETTINGS = {
    'trigger_apps': ['resolve.exe'],
    'music_apps': [
        'spotify.exe',
        'cloudmusic.exe',
        'neteasecloudmusic.exe',
        'qqmusic.exe',
        'applemusic.exe',
        'msedge.exe',
        'chrome.exe',
        'firefox.exe',
    ],
    'duck_to_percent': 8,
    'peak_threshold': 0.006,
    'release_seconds': 30.0,
    'fade_down_seconds': 1.0,
    'fade_up_seconds': 60.0,
    'check_interval_ms': 200,
}


def _clamp_number(value, default, minimum, maximum):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float(default)
    return max(minimum, min(maximum, value))


def _normalize_app_names(value, default):
    if value is None:
        value = default
    if isinstance(value, str):
        for separator in (',', '，', ';', '；'):
            value = value.replace(separator, '\n')
        value = value.splitlines()
    if not isinstance(value, (list, tuple, set)):
        value = default

    result = []
    seen = set()
    for name in value:
        name = str(name or '').strip().lower()
        if not name:
            continue
        if '.' not in name:
            name = f'{name}.exe'
        if name not in seen:
            result.append(name)
            seen.add(name)
    return result


def normalize_music_ducker_settings(settings=None):
    settings = settings if isinstance(settings, dict) else {}
    defaults = DEFAULT_MUSIC_DUCKER_SETTINGS
    return {
        'trigger_apps': _normalize_app_names(
            settings.get('trigger_apps'), defaults['trigger_apps']
        ),
        'music_apps': _normalize_app_names(
            settings.get('music_apps'), defaults['music_apps']
        ),
        'duck_to_percent': int(round(_clamp_number(
            settings.get('duck_to_percent'), defaults['duck_to_percent'], 0, 100
        ))),
        'peak_threshold': _clamp_number(
            settings.get('peak_threshold'), defaults['peak_threshold'], 0, 1
        ),
        'release_seconds': _clamp_number(
            settings.get('release_seconds'), defaults['release_seconds'], 0, 600
        ),
        'fade_down_seconds': _clamp_number(
            settings.get('fade_down_seconds'), defaults['fade_down_seconds'], 0, 600
        ),
        'fade_up_seconds': _clamp_number(
            settings.get('fade_up_seconds'), defaults['fade_up_seconds'], 0, 600
        ),
        'check_interval_ms': int(round(_clamp_number(
            settings.get('check_interval_ms'), defaults['check_interval_ms'], 50, 5000
        ))),
    }


def load_audio_dependencies():
    """延迟加载 Windows 音频依赖，缺失时不影响主程序启动。"""
    try:
        import psutil
        from pycaw.pycaw import (
            AudioUtilities,
            IAudioMeterInformation,
            ISimpleAudioVolume,
        )
    except ImportError as error:
        raise RuntimeError(
            '缺少音乐压制依赖，请在当前 Python 环境安装：pip install psutil pycaw'
        ) from error
    return psutil, AudioUtilities, ISimpleAudioVolume, IAudioMeterInformation


def iter_audio_sessions(dependencies):
    """遍历有效音频会话，跳过已退出或无权限访问的进程。"""
    psutil, audio_utilities, volume_interface, meter_interface = dependencies
    try:
        sessions = audio_utilities.GetAllSessions()
    except Exception:
        return

    for session in sessions:
        try:
            process = session.Process
            if not process:
                continue
            try:
                pid = process.pid
                name = process.name().lower()
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
                ProcessLookupError,
                OSError,
            ):
                continue
            try:
                volume = session._ctl.QueryInterface(volume_interface)
                meter = session._ctl.QueryInterface(meter_interface)
            except Exception:
                continue
            yield name, pid, volume, meter
        except Exception:
            continue


def get_peak_for_apps(app_names, dependencies):
    peaks = []
    for name, _pid, _volume, meter in iter_audio_sessions(dependencies):
        if name in app_names:
            try:
                peaks.append(meter.GetPeakValue())
            except Exception:
                pass
    return max(peaks) if peaks else 0.0


def get_music_sessions(dependencies, app_names):
    sessions = []
    for name, pid, volume, _meter in iter_audio_sessions(dependencies):
        if name in app_names:
            sessions.append((name, pid, volume))
    return sessions


def move_towards(current, target, max_delta):
    if abs(target - current) <= max_delta:
        return target
    if target > current:
        return current + max_delta
    return current - max_delta


def restore_music_volumes(dependencies, original_volumes, app_names, report):
    restored = 0
    for name, pid, volume in get_music_sessions(dependencies, app_names):
        key = (name, pid)
        if key not in original_volumes:
            continue
        try:
            volume.SetMasterVolume(original_volumes[key], None)
            restored += 1
        except Exception:
            pass
    if restored:
        report(f'音乐压制已停止，已恢复 {restored} 个音频会话的音量。')
    else:
        report('音乐压制已停止。')


def run_music_ducker(stop_event, report, settings=None):
    """运行压制循环，直到 stop_event 被设置；退出时恢复原始音量。"""
    settings = normalize_music_ducker_settings(settings)
    trigger_apps = set(settings['trigger_apps'])
    music_apps = set(settings['music_apps'])
    duck_to = settings['duck_to_percent'] / 100.0
    peak_threshold = settings['peak_threshold']
    release_seconds = settings['release_seconds']
    fade_down_seconds = settings['fade_down_seconds']
    fade_up_seconds = settings['fade_up_seconds']
    check_interval = settings['check_interval_ms'] / 1000.0
    dependencies = None
    original_volumes = {}
    ducking = False
    last_trigger_time = 0.0
    last_loop_time = time.time()

    comtypes_module = None
    com_initialized = False
    try:
        try:
            import comtypes

            comtypes.CoInitialize()
            comtypes_module = comtypes
            com_initialized = True
        except Exception:
            # 某些 pycaw/comtypes 版本会自行初始化 COM，继续尝试运行。
            pass

        dependencies = load_audio_dependencies()
        report('音乐压制已开启：检测到达芬奇出声时会自动压低音乐。')
        while not stop_event.is_set():
            now = time.time()
            delta_time = max(now - last_loop_time, check_interval)
            last_loop_time = now

            resolve_peak = get_peak_for_apps(trigger_apps, dependencies)
            if resolve_peak >= peak_threshold:
                last_trigger_time = now
                if not ducking:
                    report(
                        f'检测到达芬奇出声（峰值 {resolve_peak:.4f}），开始压低音乐。'
                    )
                    original_volumes.clear()
                    for name, pid, volume in get_music_sessions(
                        dependencies, music_apps
                    ):
                        try:
                            original_volumes[(name, pid)] = volume.GetMasterVolume()
                        except Exception:
                            pass
                    ducking = True

            if ducking:
                should_duck = (now - last_trigger_time) < release_seconds
                fade_seconds = max(
                    fade_down_seconds if should_duck else fade_up_seconds,
                    0.01,
                )
                max_delta = delta_time / fade_seconds
                all_restored = True

                for name, pid, volume in get_music_sessions(
                    dependencies, music_apps
                ):
                    key = (name, pid)
                    try:
                        current = volume.GetMasterVolume()
                    except Exception:
                        continue

                    if key not in original_volumes:
                        original_volumes[key] = current
                    if should_duck:
                        target = duck_to
                        all_restored = False
                    else:
                        target = original_volumes.get(key, current)

                    new_volume = move_towards(current, target, max_delta)
                    try:
                        volume.SetMasterVolume(new_volume, None)
                    except Exception:
                        continue
                    if not should_duck and abs(new_volume - target) > 0.01:
                        all_restored = False

                if not should_duck and all_restored:
                    report('达芬奇已安静，音乐音量恢复完成。')
                    ducking = False
                    original_volumes.clear()

            stop_event.wait(check_interval)
    finally:
        if dependencies is not None:
            restore_music_volumes(
                dependencies,
                original_volumes,
                music_apps,
                report,
            )
        if com_initialized and comtypes_module is not None:
            try:
                comtypes_module.CoUninitialize()
            except Exception:
                pass


class MusicDuckerThread(QThread):
    message = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = normalize_music_ducker_settings(settings)
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            run_music_ducker(
                self._stop_event,
                self.message.emit,
                self.settings,
            )
        except Exception as error:
            self.error.emit(str(error))
