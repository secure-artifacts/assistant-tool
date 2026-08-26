import os
import sys

from app_paths import APP_ROOT

# Keep legacy relative paths stable in source runs and PyInstaller builds.
os.chdir(APP_ROOT)

# 1. 解决 OpenMP 冲突 (常见于 PyTorch/Whisper)
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

from globalValue import globalValue # 必须在PYQT5之前初始化，否则会出问题


from PyQt5 import QtWidgets



from PYUI.main_pyui import MainDialog



def main():
    globalValue.get_whisper_model()

    app = QtWidgets.QApplication(sys.argv)
    Dialog = MainDialog()
    Dialog.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

