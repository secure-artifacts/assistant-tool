import os
import sys

from app_paths import APP_ROOT

# Keep legacy relative paths stable in source runs and PyInstaller builds.
os.chdir(APP_ROOT)

# 1. 解决 OpenMP 冲突 (常见于 PyTorch/Whisper)
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

from model.AppLogger import (
    configure_application_logging,
    install_exception_hooks,
    shutdown_application_logging,
)

app_logger, app_log_file = configure_application_logging()
install_exception_hooks()
app_logger.info("程序启动，Python：%s", sys.version.replace("\n", " "))

from globalValue import globalValue # 必须在PYQT5之前初始化，否则会出问题


from PyQt5 import QtWidgets

from model.ComboBoxWheelGuard import ComboBoxWheelGuard


from PYUI.main_pyui import MainDialog



def main():
    globalValue.get_whisper_model()

    app = QtWidgets.QApplication(sys.argv)
    app.combo_box_wheel_guard = ComboBoxWheelGuard(app)
    app.installEventFilter(app.combo_box_wheel_guard)
    app.aboutToQuit.connect(shutdown_application_logging)
    Dialog = MainDialog()
    Dialog.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())

