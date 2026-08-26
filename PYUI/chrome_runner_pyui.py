import json
import os
import random
import subprocess
import time

from PyQt5 import QtWidgets, QtGui
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QListWidgetItem, QMessageBox, QInputDialog

from QTUI.chrome_runner_ui import Ui_ChromeRunnerDialog

class ChromeRunnerDialog(QtWidgets.QDialog,Ui_ChromeRunnerDialog):
    iterator_config_key = 'chrome_profile_iterator'
    websites_config_key = 'chrome_preset_websites'
    default_websites = []

    def __init__(self, parent=None, config_path='config.json'):
        super(ChromeRunnerDialog, self).__init__(parent)
        self.setupUi(self)

        self.profiles = []
        self.config_path = os.path.abspath(config_path)
        self.next_profile_index = 0
        self.iterator_profile_directories = []
        self._saved_next_profile_directory = None
        self.chrome_path = self.find_chrome_path()
        self.user_data_dir = self.get_chrome_user_data_dir()
        self.load_iterator_state()
        self.load_websites()

        self.run_btn.clicked.connect(self.launch_selected_profiles)
        self.run_next_btn.clicked.connect(self.start_selected_iterator)
        self.reset_iterator_btn.clicked.connect(self.reset_iterator)
        self.add_web_btn.clicked.connect(self.add_website)
        self.edit_web_btn.clicked.connect(self.edit_website)
        self.delete_web_btn.clicked.connect(self.delete_websites)
        self.web_list_widget.itemDoubleClicked.connect(self.edit_website)

        self.load_profiles()
        self.refresh_btn.clicked.connect(self.load_profiles)


    def find_chrome_path(self):
        """查找 Chrome 可执行文件路径"""
        possible_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]

        for path in possible_paths:
            if os.path.exists(path):
                return path
        return None

    def get_chrome_user_data_dir(self):
        """获取 Chrome 用户数据目录"""
        return os.path.join(os.environ['LOCALAPPDATA'], 'Google', 'Chrome', 'User Data')

    def get_profile_sort_key(self, profile_dir):
        """Local State 不可用时，按 Chrome Profile 目录顺序排序。"""
        if profile_dir == 'Default':
            return (0, 0)
        if profile_dir.startswith('Profile '):
            try:
                return (1, int(profile_dir.split(' ')[1]))
            except (ValueError, IndexError):
                pass
        return (2, profile_dir.lower())

    def load_profiles(self):
        """加载所有 Chrome profiles"""
        self.chrome_list_widget.clear()
        self.profiles = []

        if not os.path.exists(self.user_data_dir):
            self.status_label.setText('错误: Chrome 用户数据目录不存在')
            return

        # 读取 Local State 文件获取 profile 信息
        local_state_path = os.path.join(self.user_data_dir, 'Local State')

        if os.path.exists(local_state_path):
            try:
                with open(local_state_path, 'r', encoding='utf-8') as f:
                    local_state = json.load(f)

                profile_info = local_state.get('profile', {}).get('info_cache', {})

                for profile_dir, info in profile_info.items():
                    profile_name = info.get('name', profile_dir)
                    profile_path = os.path.join(self.user_data_dir, profile_dir)

                    if os.path.exists(profile_path):
                        self.profiles.append({
                            'name': profile_name,
                            'directory': profile_dir,
                            'path': profile_path
                        })

                # 按 Profile 名称排序（字母顺序）
                self.profiles.sort(key=lambda p: p['name'].lower())

                # 添加到列表
                for profile in self.profiles:
                    item = QListWidgetItem(f"{profile['name']} ({profile['directory']})")
                    item.setData(Qt.UserRole, profile['directory'])
                    self.chrome_list_widget.addItem(item)

                self.restore_iterator_position()
                self.update_iterator_status(f'已加载 {len(self.profiles)} 个 profiles')

            except Exception as e:
                self.status_label.setText(f'错误: {str(e)}')
        else:
            # 如果 Local State 不存在，尝试扫描目录
            self.scan_profile_directories()

    def scan_profile_directories(self):
        """扫描 Chrome 用户数据目录查找 profiles"""
        for item in os.listdir(self.user_data_dir):
            item_path = os.path.join(self.user_data_dir, item)

            # 查找 Default 和 Profile * 目录
            if os.path.isdir(item_path) and (item == 'Default' or item.startswith('Profile ')):
                self.profiles.append({
                    'name': item,
                    'directory': item,
                    'path': item_path
                })

        # 按 Profile ID 排序
        self.profiles.sort(key=lambda p: self.get_profile_sort_key(p['directory']))

        # 添加到列表
        for profile in self.profiles:
            list_item = QListWidgetItem(profile['name'])
            list_item.setData(Qt.UserRole, profile['directory'])
            self.chrome_list_widget.addItem(list_item)

        self.restore_iterator_position()
        self.update_iterator_status(f'已扫描 {len(self.profiles)} 个 profiles')

    def read_config(self):
        if not os.path.exists(self.config_path):
            return {}
        try:
            with open(self.config_path, 'r', encoding='utf-8') as config_file:
                config = json.load(config_file)
                return config if isinstance(config, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def write_config(self, config):
        """原子写入配置，避免中途退出产生半个 JSON 文件。"""
        temp_path = f'{self.config_path}.chrome-runner.tmp'
        try:
            with open(temp_path, 'w', encoding='utf-8') as config_file:
                json.dump(config, config_file, indent=4, ensure_ascii=False)
            os.replace(temp_path, self.config_path)
            return True
        except OSError as error:
            print(f'保存 Chrome 配置失败: {error}')
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            return False

    @staticmethod
    def normalize_website_url(url):
        url = str(url or '').strip()
        if url and '://' not in url:
            url = f'https://{url}'
        return url

    def set_websites(self, websites):
        self.web_list_widget.clear()
        seen = set()
        for website in websites:
            website = self.normalize_website_url(website)
            if website and website not in seen:
                self.web_list_widget.addItem(website)
                seen.add(website)

    def load_websites(self):
        config = self.read_config()
        websites = config.get(self.websites_config_key)
        should_initialize_config = (
            self.websites_config_key not in config
            or not isinstance(websites, list)
        )
        if should_initialize_config:
            websites = self.default_websites
        self.set_websites(websites)
        if should_initialize_config:
            config[self.websites_config_key] = self.get_web_list()
            if not self.write_config(config):
                QMessageBox.critical(self, '错误', '初始化预设网站配置失败！')

    def save_websites(self):
        config = self.read_config()
        config[self.websites_config_key] = self.get_web_list()
        saved = self.write_config(config)
        if not saved:
            QMessageBox.critical(self, '错误', '预设网站写入配置文件失败！')
        return saved

    def add_website(self):
        website, accepted = QInputDialog.getText(
            self,
            '添加预设网站',
            '网站地址：',
        )
        if not accepted:
            return
        website = self.normalize_website_url(website)
        if not website:
            return
        if website in self.get_web_list():
            QMessageBox.information(self, '提示', '这个网址已经在列表中。')
            return
        self.web_list_widget.addItem(website)
        if self.save_websites():
            self.update_iterator_status('已添加预设网站')

    def edit_website(self, item=None):
        if item is None:
            item = self.web_list_widget.currentItem()
        if item is None:
            QMessageBox.warning(self, '警告', '请先选择一个要修改的网址！')
            return

        old_website = item.data(Qt.DisplayRole)
        website, accepted = QInputDialog.getText(
            self,
            '修改预设网站',
            '网站地址：',
            QtWidgets.QLineEdit.Normal,
            old_website,
        )
        if not accepted:
            return
        website = self.normalize_website_url(website)
        if not website:
            QMessageBox.warning(self, '警告', '网址不能为空！')
            return
        if website != old_website and website in self.get_web_list():
            QMessageBox.information(self, '提示', '这个网址已经在列表中。')
            return
        item.setText(website)
        if self.save_websites():
            self.update_iterator_status('已修改预设网站')

    def delete_websites(self):
        selected_items = self.web_list_widget.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, '警告', '请先选择要删除的网址！')
            return
        answer = QMessageBox.question(
            self,
            '删除预设网站',
            f'确定删除选中的 {len(selected_items)} 个网址吗？',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        rows = sorted(
            (self.web_list_widget.row(item) for item in selected_items),
            reverse=True,
        )
        for row in rows:
            self.web_list_widget.takeItem(row)
        if self.save_websites():
            self.update_iterator_status(f'已删除 {len(rows)} 个预设网站')

    def load_iterator_state(self):
        state = self.read_config().get(self.iterator_config_key, {})
        if not isinstance(state, dict):
            state = {}
        selected_directories = state.get('selected_profile_directories', [])
        if not isinstance(selected_directories, list):
            selected_directories = []
        self.iterator_profile_directories = list(dict.fromkeys(
            str(directory)
            for directory in selected_directories
            if directory
        ))
        try:
            self.next_profile_index = max(0, int(state.get('next_index', 0)))
        except (TypeError, ValueError):
            self.next_profile_index = 0
        next_directory = state.get('next_profile_directory')
        self._saved_next_profile_directory = (
            str(next_directory) if next_directory else None
        )

    def save_iterator_state(self):
        config = self.read_config()
        next_directory = None
        iterator_profiles = self.get_iterator_profiles()
        if iterator_profiles:
            self.next_profile_index %= len(iterator_profiles)
            next_directory = iterator_profiles[self.next_profile_index]['directory']

        config[self.iterator_config_key] = {
            'next_index': self.next_profile_index,
            'next_profile_directory': next_directory,
            'selected_profile_directories': self.iterator_profile_directories,
        }
        if self.write_config(config):
            self._saved_next_profile_directory = next_directory

    def restore_iterator_position(self):
        available_directories = {
            profile['directory'] for profile in self.profiles
        }
        selected_set = {
            directory for directory in self.iterator_profile_directories
            if directory in available_directories
        }
        # 选中的迭代队列始终按当前可见列表顺序排列。
        self.iterator_profile_directories = [
            profile['directory']
            for profile in self.profiles
            if profile['directory'] in selected_set
        ]

        for row in range(self.chrome_list_widget.count()):
            item = self.chrome_list_widget.item(row)
            item.setSelected(item.data(Qt.UserRole) in selected_set)

        iterator_profiles = self.get_iterator_profiles()
        if not iterator_profiles:
            self.next_profile_index = 0
            return

        if self._saved_next_profile_directory:
            for index, profile in enumerate(iterator_profiles):
                if profile['directory'] == self._saved_next_profile_directory:
                    self.next_profile_index = index
                    break
            else:
                self.next_profile_index %= len(iterator_profiles)
        else:
            self.next_profile_index %= len(iterator_profiles)

    def get_selected_profile_directories(self):
        """按列表显示顺序返回当前选中的 Profile 目录。"""
        selected_directories = []
        for row in range(self.chrome_list_widget.count()):
            item = self.chrome_list_widget.item(row)
            if item.isSelected():
                selected_directories.append(item.data(Qt.UserRole))
        return selected_directories

    def get_iterator_profiles(self):
        selected_set = set(self.iterator_profile_directories)
        return [
            profile for profile in self.profiles
            if profile['directory'] in selected_set
        ]

    def update_iterator_status(self, message=None):
        for row in range(self.chrome_list_widget.count()):
            item = self.chrome_list_widget.item(row)
            item.setBackground(QtGui.QBrush())
            item.setToolTip('')

        iterator_profiles = self.get_iterator_profiles()
        if not iterator_profiles:
            prompt = '未设置迭代列表：请先选择 Profile，再点击“按选中项迭代启动”'
            self.status_label.setText(f'{message}；{prompt}' if message else prompt)
            return

        self.next_profile_index %= len(iterator_profiles)
        next_profile = iterator_profiles[self.next_profile_index]
        next_item = None
        for row in range(self.chrome_list_widget.count()):
            item = self.chrome_list_widget.item(row)
            if item.data(Qt.UserRole) == next_profile['directory']:
                next_item = item
                break
        if next_item is not None:
            next_item.setBackground(QtGui.QBrush(QtGui.QColor('#D9F2D9')))
            next_item.setToolTip('下一个将启动此 Profile')
            self.chrome_list_widget.scrollToItem(next_item)

        next_text = (
            f"已选 {len(iterator_profiles)} 个；下一个：{next_profile['name']} "
            f"({self.next_profile_index + 1}/{len(iterator_profiles)})"
        )
        self.status_label.setText(f'{message}；{next_text}' if message else next_text)

    def reset_iterator(self):
        if not self.get_iterator_profiles():
            QMessageBox.warning(
                self,
                '警告',
                '尚未设置迭代列表，请先选择 Profile 并点击“按选中项迭代启动”！'
            )
            return
        self.next_profile_index = 0
        self.save_iterator_state()
        self.update_iterator_status('迭代进度已重置')

    def select_all(self):
        """全选所有 profiles"""
        for i in range(self.chrome_list_widget.count()):
            self.chrome_list_widget.item(i).setSelected(True)

    def deselect_all(self):
        """取消全选"""
        self.chrome_list_widget.clearSelection()

    def launch_selected_profiles(self):
        """启动选中的 profiles"""
        if not self.chrome_path:
            QMessageBox.warning(self, '错误', 'Chrome 可执行文件未找到！')
            return

        selected_items = self.chrome_list_widget.selectedItems()

        if not selected_items:
            QMessageBox.warning(self, '警告', '请至少选择一个 profile！')
            return

        launched_count = 0

        for item in selected_items:
            profile_dir = item.data(Qt.UserRole)
            profile = next(
                (profile for profile in self.profiles
                 if profile['directory'] == profile_dir),
                {'name': profile_dir, 'directory': profile_dir},
            )
            success, error = self.launch_profile(profile)
            if success:
                launched_count += 1
                time.sleep(random.random())
            else:
                QMessageBox.critical(self, '错误', f'启动失败: {error}')

        self.update_iterator_status(f'已批量启动 {launched_count} 个 profiles')

    def launch_profile(self, profile):
        if not self.chrome_path:
            return False, 'Chrome 可执行文件未找到'
        try:
            cmd = [
                self.chrome_path,
                f"--profile-directory={profile['directory']}",
                '--new-window',
                '--start-maximized',
                *self.get_web_list(),
            ]
            subprocess.Popen(cmd)
            return True, None
        except Exception as error:
            return False, str(error)

    def start_selected_iterator(self):
        """用窗口中当前选中项建立/更新迭代队列，并启动队列下一项。"""
        selected_directories = self.get_selected_profile_directories()
        if not selected_directories:
            QMessageBox.warning(
                self,
                '警告',
                '请先在 Chrome 列表中选择要迭代启动的 Profile！'
            )
            return None

        if selected_directories != self.iterator_profile_directories:
            self.iterator_profile_directories = selected_directories
            self.next_profile_index = 0
            self._saved_next_profile_directory = selected_directories[0]
            self.save_iterator_state()
        return self.launch_next_profile()

    def launch_next_profile(self):
        """只在已保存的选中队列中启动下一项，并持久化位置。"""
        iterator_profiles = self.get_iterator_profiles()
        if not iterator_profiles:
            QMessageBox.warning(
                self,
                '警告',
                '没有已保存的迭代列表。请先打开 Chrome 窗口，选择 Profile，'
                '再点击“按选中项迭代启动”！'
            )
            return None

        self.next_profile_index %= len(iterator_profiles)
        current_index = self.next_profile_index
        profile = iterator_profiles[current_index]
        success, error = self.launch_profile(profile)
        if not success:
            QMessageBox.critical(self, '错误', f'启动失败: {error}')
            self.update_iterator_status('启动失败，迭代位置未推进')
            return None

        self.next_profile_index = (current_index + 1) % len(iterator_profiles)
        wrapped = self.next_profile_index == 0
        self.save_iterator_state()
        message = f"已启动：{profile['name']}"
        if wrapped:
            message += '；已到列表末尾，将从头继续'
        self.update_iterator_status(message)
        return profile


    def get_web_list(self):
        result = []
        for row in range(self.web_list_widget.count()):
            item = self.web_list_widget.item(row)
            result.append(item.data(Qt.DisplayRole))
        return result
