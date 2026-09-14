from PyQt5.QtWidgets import QLineEdit, QApplication
from PyQt5.QtCore import Qt, pyqtSignal
import sys
import os


class DragDropLineEdit(QLineEdit):
    """
    支持拖放文件夹路径的 QLineEdit 扩展组件
    """
    # 自定义信号，当路径改变时发出
    pathDropped = pyqtSignal(str)

    def __init__(self, parent=None, accept_files=False):
        super().__init__(parent)

        # 是否接受文件路径（默认只接受文件夹）
        self.accept_files = accept_files

        # 启用拖放功能
        self.setAcceptDrops(True)

        # 设置提示文本
        self.setPlaceholderText("拖放文件夹到这里..." if not accept_files else "拖放文件或文件夹到这里...")

    def dragEnterEvent(self, event):
        """处理拖动进入事件"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            # 改变边框样式提示用户
            self.setStyleSheet("QLineEdit { border: 2px dashed #4CAF50; }")
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        """处理拖动离开事件"""
        # 恢复原始样式
        self.setStyleSheet("")
        event.accept()

    def dropEvent(self, event):
        """处理放下事件"""
        # 恢复原始样式
        self.setStyleSheet("")

        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls:
                # 获取第一个 URL 的本地路径
                file_path = urls[0].toLocalFile()

                # 检查是否为文件夹
                if os.path.isdir(file_path):
                    self.setText(file_path)
                    self.pathDropped.emit(file_path)
                    event.acceptProposedAction()
                # 如果允许文件，也接受文件路径
                elif self.accept_files and os.path.isfile(file_path):
                    self.setText(file_path)
                    self.pathDropped.emit(file_path)
                    event.acceptProposedAction()
                else:
                    event.ignore()
                    # 可以在这里添加错误提示
                    print("只接受文件夹路径" if not self.accept_files else "路径无效")
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        """处理拖动移动事件"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()


# 使用示例
if __name__ == '__main__':
    app = QApplication(sys.argv)

    # 创建主窗口
    from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel

    window = QWidget()
    window.setWindowTitle('拖放文件夹路径示例')
    window.setGeometry(100, 100, 500, 200)

    layout = QVBoxLayout()

    # 添加说明标签
    label1 = QLabel('只接受文件夹:')
    layout.addWidget(label1)

    # 只接受文件夹的输入框
    folder_input = DragDropLineEdit()
    folder_input.pathDropped.connect(lambda path: print(f"文件夹路径: {path}"))
    layout.addWidget(folder_input)

    # 添加间距
    layout.addSpacing(20)

    label2 = QLabel('接受文件和文件夹:')
    layout.addWidget(label2)

    # 接受文件和文件夹的输入框
    file_input = DragDropLineEdit(accept_files=True)
    file_input.pathDropped.connect(lambda path: print(f"路径: {path}"))
    layout.addWidget(file_input)

    window.setLayout(layout)
    window.show()

    sys.exit(app.exec_())