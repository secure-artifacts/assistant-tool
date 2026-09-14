from PyQt5 import QtCore, QtWidgets


def _t(value):
    return bytes.fromhex(value).decode("utf-8")


class IntervalPrompt(QtWidgets.QDialog):
    def __init__(self, parent=None, seconds=60):
        super().__init__(parent)
        self._remaining = max(1, int(seconds))
        self.setWindowTitle("LZX")
        self.setModal(True)
        self.setMinimumWidth(430)

        layout = QtWidgets.QVBoxLayout(self)
        self._label = QtWidgets.QLabel(
            _t(
                "e79c9fe79a84e783a6efbc8ce588abe782b9e4ba86efbc81"
                "e7bb99e4bda0e8a1a8e6bc94e4b880e4b8aae9ad94e6b395"
                "e590a7efbc8ce697b6e7a9bae7a9bfe6a2ade38082"
            ),
            self,
        )
        self._label.setWordWrap(True)
        self._label.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self._label)

        self._button = QtWidgets.QPushButton(
            _t("e5bc80e5a78be7a9bfe6a2ad"),
            self,
        )
        self._button.clicked.connect(self._start)
        layout.addWidget(self._button)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    def _countdown_text(self):
        return _t(
            "e697b6e7a9bae7a9bfe6a2ade4b8ade280a6e280a6e8bf98"
            "e589a9207b307d20e7a792"
        ).format(self._remaining)

    def _start(self):
        self._button.setEnabled(False)
        self._label.setText(self._countdown_text())
        self._timer.start()

    def _tick(self):
        self._remaining -= 1
        if self._remaining > 0:
            self._label.setText(self._countdown_text())
            return
        self._timer.stop()
        parent = self.parentWidget()
        self.accept()
        QtWidgets.QMessageBox.information(
            parent,
            "LZX",
            _t(
                "e681ade5969ce4bda0efbc8ce7a9bfe6a2ade588b0e4b880"
                "e58886e9929fe5908ef09fa4a3"
            ),
        )
