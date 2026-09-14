from PyQt5 import QtCore, QtWidgets


INTERNAL_CLIPBOARD_MIME = "application/x-davinci-helper-internal-copy"


def set_internal_clipboard_text(text):
    """Copy text while marking it as produced by this application."""
    mime_data = QtCore.QMimeData()
    mime_data.setText(str(text or ""))
    mime_data.setData(INTERNAL_CLIPBOARD_MIME, b"1")
    QtWidgets.QApplication.clipboard().setMimeData(mime_data)


def is_internal_clipboard_content(clipboard=None):
    clipboard = clipboard or QtWidgets.QApplication.clipboard()
    mime_data = clipboard.mimeData()
    return bool(mime_data and mime_data.hasFormat(INTERNAL_CLIPBOARD_MIME))
