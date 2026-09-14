import os
import sys

from PyQt5.QtCore import QDir, Qt
from PyQt5.QtWidgets import QTreeView, QApplication, QMessageBox, QMenu, QFileSystemModel


class FileExplorerTreeView(QTreeView):
    def __init__(self, parent=None):
        super(FileExplorerTreeView, self).__init__(parent)
        # 创建文件系统模型
        self.model = QFileSystemModel()
        self.model.setRootPath('')

        # 设置过滤器（显示所有文件和文件夹）
        self.model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot | QDir.AllDirs)

        # 创建树视图
        self.setModel(self.model)

        # 设置树视图属性
        self.setAnimated(True)
        self.setIndentation(20)
        self.setSortingEnabled(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

        # 只显示名称列，隐藏其他列（大小、类型、修改日期）
        self.setColumnWidth(0, 300)
        for i in range(1, 4):
            self.hideColumn(i)

        # 双击事件
        self.doubleClicked.connect(self.on_double_click)

        # 默认加载用户主目录
        home_path = QDir.homePath()
        self.dir_path = home_path
        self.load_directory(self.dir_path)

        self.right_menu_action_list = []


    def load_directory(self, path):
        """加载指定目录"""
        if os.path.exists(path):
            index = self.model.index(str(path))
            self.setRootIndex(index)
            self.expand(index)
        else:
            QMessageBox.warning(self, '错误', f'路径不存在: {path}')


    def refresh_view(self):
        """刷新当前视图"""
        if self.dir_path:
            self.load_directory(self.dir_path)

    def on_double_click(self, index):
        """双击事件处理"""
        file_path = self.model.filePath(index)
        if os.path.isfile(file_path):
            # 这里可以添加打开文件的逻辑
            QMessageBox.information(
                self,
                '文件信息',
                f'文件路径: {file_path}\n文件大小: {os.path.getsize(file_path)} 字节'
            )

    def add_context_menu(self,text,slot):
        self.right_menu_action_list.append((text, slot))


    def show_context_menu(self, position):
        """显示右键菜单"""
        index = self.indexAt(position)
        if not index.isValid():
            return

        file_path = self.model.filePath(index)

        menu = QMenu()

        # 复制路径
        copy_path_action = menu.addAction('复制路径')
        # 在文件管理器中显示
        show_in_explorer = menu.addAction('在文件管理器中显示')
        # 属性
        properties_action = menu.addAction('属性')

        if self.right_menu_action_list:
            menu.addSeparator()
            for text, slot in self.right_menu_action_list:
                if text == "-":
                    menu.addSeparator()
                else:
                    menu.addAction(text,lambda : slot(file_path))


        # 显示菜单并获取选中的动作
        action = menu.exec_(self.viewport().mapToGlobal(position))

        # 处理菜单动作
        if action == copy_path_action:
            clipboard = QApplication.clipboard()
            clipboard.setText(file_path)

        elif action == show_in_explorer:
            if sys.platform == 'win32':
                os.startfile(os.path.dirname(file_path))
            elif sys.platform == 'darwin':
                os.system(f'open "{os.path.dirname(file_path)}"')
            else:
                os.system(f'xdg-open "{os.path.dirname(file_path)}"')

        elif action == properties_action:
            self.show_properties(file_path)


    def show_properties(self, file_path):
        """显示文件/文件夹属性"""
        if os.path.isfile(file_path):
            size = os.path.getsize(file_path)
            info = f'路径: {file_path}\n类型: 文件\n大小: {size} 字节'
        else:
            info = f'路径: {file_path}\n类型: 文件夹'

        QMessageBox.information(self, '属性', info)