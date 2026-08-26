import ctypes
import ctypes.wintypes
import sys

from PyQt5 import QtCore, QtGui


CHROME_NEXT_HOTKEY_CONFIG_KEY = 'chrome_next_global_hotkey'
DEFAULT_CHROME_NEXT_HOTKEY = 'Ctrl+Alt+N'

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 0xA118


SPECIAL_KEY_TO_VK = {
    int(QtCore.Qt.Key_Space): 0x20,
    int(QtCore.Qt.Key_Escape): 0x1B,
    int(QtCore.Qt.Key_Tab): 0x09,
    int(QtCore.Qt.Key_Backspace): 0x08,
    int(QtCore.Qt.Key_Return): 0x0D,
    int(QtCore.Qt.Key_Enter): 0x0D,
    int(QtCore.Qt.Key_Insert): 0x2D,
    int(QtCore.Qt.Key_Delete): 0x2E,
    int(QtCore.Qt.Key_Home): 0x24,
    int(QtCore.Qt.Key_End): 0x23,
    int(QtCore.Qt.Key_PageUp): 0x21,
    int(QtCore.Qt.Key_PageDown): 0x22,
    int(QtCore.Qt.Key_Left): 0x25,
    int(QtCore.Qt.Key_Up): 0x26,
    int(QtCore.Qt.Key_Right): 0x27,
    int(QtCore.Qt.Key_Down): 0x28,
}


def _key_to_virtual_key(qt_key):
    if ord('A') <= qt_key <= ord('Z'):
        return qt_key
    if ord('0') <= qt_key <= ord('9'):
        return qt_key

    first_function_key = int(QtCore.Qt.Key_F1)
    last_function_key = int(QtCore.Qt.Key_F24)
    if first_function_key <= qt_key <= last_function_key:
        return 0x70 + (qt_key - first_function_key)
    return SPECIAL_KEY_TO_VK.get(qt_key)


def parse_hotkey_sequence(sequence_text):
    """返回 (规范文本, Windows modifiers, virtual key)。"""
    sequence_text = str(sequence_text or '').strip()
    if not sequence_text:
        raise ValueError('快捷键不能为空。')

    sequence = QtGui.QKeySequence(
        sequence_text,
        QtGui.QKeySequence.PortableText,
    )
    active_keys = [
        int(sequence[index])
        for index in range(sequence.count())
        if int(sequence[index])
    ]
    if len(active_keys) != 1:
        raise ValueError('全局快捷键只能包含一个组合键，不能使用连续按键。')

    combined_key = active_keys[0]
    modifiers = 0
    if combined_key & int(QtCore.Qt.ControlModifier):
        modifiers |= MOD_CONTROL
    if combined_key & int(QtCore.Qt.AltModifier):
        modifiers |= MOD_ALT
    if combined_key & int(QtCore.Qt.ShiftModifier):
        modifiers |= MOD_SHIFT
    if combined_key & int(QtCore.Qt.MetaModifier):
        modifiers |= MOD_WIN
    if not modifiers:
        raise ValueError('快捷键至少需要 Ctrl、Alt、Shift 或 Win 中的一个修饰键。')

    qt_key = combined_key & 0x01FFFFFF
    virtual_key = _key_to_virtual_key(qt_key)
    if virtual_key is None:
        raise ValueError('这个按键暂不支持；请使用字母、数字、F1–F24 或常用功能键。')

    canonical_text = sequence.toString(QtGui.QKeySequence.PortableText)
    return canonical_text, modifiers | MOD_NOREPEAT, virtual_key


def normalize_hotkey_sequence(sequence_text):
    return parse_hotkey_sequence(sequence_text)[0]


class _NativeHotkeyFilter(QtCore.QAbstractNativeEventFilter):
    def __init__(self, hotkey_id, callback):
        super().__init__()
        self.hotkey_id = hotkey_id
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        try:
            event_name = bytes(event_type)
            if event_name not in (
                b'windows_generic_MSG',
                b'windows_dispatcher_MSG',
            ):
                return False, 0
            native_message = ctypes.wintypes.MSG.from_address(int(message))
            if (
                native_message.message == WM_HOTKEY
                and native_message.wParam == self.hotkey_id
            ):
                QtCore.QTimer.singleShot(0, self.callback)
        except (TypeError, ValueError, OSError):
            pass
        return False, 0


class GlobalHotkeyManager(QtCore.QObject):
    activated = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sequence = None
        self.last_error = ''
        self._registered = False
        self._modifiers = None
        self._virtual_key = None
        self._native_filter = _NativeHotkeyFilter(
            HOTKEY_ID,
            self.activated.emit,
        )
        application = QtCore.QCoreApplication.instance()
        if application is None:
            raise RuntimeError('创建全局快捷键前必须先创建 QApplication。')
        application.installNativeEventFilter(self._native_filter)

        self._user32 = None
        if sys.platform == 'win32':
            self._user32 = ctypes.WinDLL('user32', use_last_error=True)
            self._user32.RegisterHotKey.argtypes = (
                ctypes.wintypes.HWND,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.c_uint,
            )
            self._user32.RegisterHotKey.restype = ctypes.wintypes.BOOL
            self._user32.UnregisterHotKey.argtypes = (
                ctypes.wintypes.HWND,
                ctypes.c_int,
            )
            self._user32.UnregisterHotKey.restype = ctypes.wintypes.BOOL

    @property
    def is_registered(self):
        return self._registered

    def _register_native(self, modifiers, virtual_key):
        if self._user32 is None:
            return False, '全局快捷键只支持 Windows。'
        ctypes.set_last_error(0)
        registered = bool(self._user32.RegisterHotKey(
            None,
            HOTKEY_ID,
            modifiers,
            virtual_key,
        ))
        if registered:
            return True, ''
        error_code = ctypes.get_last_error()
        details = ctypes.FormatError(error_code).strip() if error_code else '未知错误'
        return False, f'Windows 注册失败：{details}（错误 {error_code}）'

    def register(self, sequence_text):
        canonical, modifiers, virtual_key = parse_hotkey_sequence(sequence_text)
        if self._registered and canonical == self.sequence:
            self.last_error = ''
            return True

        previous = (
            self.sequence,
            self._modifiers,
            self._virtual_key,
        ) if self._registered else None
        self.unregister()

        registered, error = self._register_native(modifiers, virtual_key)
        if registered:
            self.sequence = canonical
            self._modifiers = modifiers
            self._virtual_key = virtual_key
            self._registered = True
            self.last_error = ''
            return True

        self.last_error = error
        if previous is not None:
            restored, restore_error = self._register_native(
                previous[1],
                previous[2],
            )
            if restored:
                self.sequence = previous[0]
                self._modifiers = previous[1]
                self._virtual_key = previous[2]
                self._registered = True
            else:
                self.last_error += f'；原快捷键恢复失败：{restore_error}'
        return False

    def unregister(self):
        if self._registered and self._user32 is not None:
            self._user32.UnregisterHotKey(None, HOTKEY_ID)
        self.sequence = None
        self._modifiers = None
        self._virtual_key = None
        self._registered = False

    def close(self):
        self.unregister()
        application = QtCore.QCoreApplication.instance()
        if application is not None:
            application.removeNativeEventFilter(self._native_filter)
