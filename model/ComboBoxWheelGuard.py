from PyQt5 import QtCore, QtWidgets


class ComboBoxWheelGuard(QtCore.QObject):
    """Prevent closed combo boxes from changing while a page is scrolled."""

    def eventFilter(self, watched, event):
        if (
            event.type() != QtCore.QEvent.Wheel
            or not isinstance(watched, QtWidgets.QComboBox)
        ):
            return False

        parent = watched.parentWidget()
        while parent is not None:
            if isinstance(parent, QtWidgets.QAbstractScrollArea):
                scroll_bar = parent.verticalScrollBar()
                if scroll_bar is not None and scroll_bar.maximum() > scroll_bar.minimum():
                    pixel_delta = event.pixelDelta().y()
                    angle_delta = event.angleDelta().y()
                    if pixel_delta:
                        distance = pixel_delta
                    elif angle_delta:
                        distance = round(
                            angle_delta / 120 * max(1, scroll_bar.singleStep()) * 3
                        )
                    else:
                        distance = 0
                    if distance:
                        scroll_bar.setValue(scroll_bar.value() - distance)
                break
            parent = parent.parentWidget()

        event.accept()
        return True
