import sys, os, re, pexpect, shutil, datetime
from collections import deque
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton, QFileDialog, QLineEdit, QMessageBox,
    QListWidget, QListWidgetItem, QProgressBar, QHBoxLayout, QLabel, QInputDialog
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal

class ExtractThread(QThread):
    """
    子线程：处理一个压缩文件的解压操作，捕捉7z的输出，通过信号通知主线程更新进度/处理密码/冲突提示等。
    """
    progress_signal = pyqtSignal(int, int)      # 更新进度条：index, percent
    status_signal = pyqtSignal(int, str)        # 解压结果状态：index, "完成"/"失败"
    conflict_prompt = pyqtSignal(int, str)      # 请求处理文件覆盖提示
    password_status = pyqtSignal(int, str)      # index, message


    def __init__(self, index, file_path, output_dir, password):
        super().__init__()
        self.index = index
        self.file_path = file_path
        self.output_dir = output_dir
        self.password = password
        self.response = None
        self.waiting = False

    # 设置用户输入的密码
    def set_password(self, password):
        self.password = password
        self.waiting = False

    # 设置用户对覆盖提示的回应（y/n/a...）
    def set_response(self, response):
        self.response = response
        self.waiting = False

    def run(self):
        def get_7z_path():
            if getattr(sys, 'frozen', False):  # PyInstaller 打包环境
                return os.path.join(sys._MEIPASS, 'bin', '7z')
            else:
                return shutil.which('7z') or './7z'

        # 构造 7z 解压命令
        # 获取打包后的资源路径
        seven_zip_path = get_7z_path()


        cmd = f"{seven_zip_path} x '{self.file_path}' -o'{self.output_dir}' -bsp1"
        # if self.password:
        #     cmd += f" -p{self.password}"

        try:
            # 启动子进程并监听输出
            child = pexpect.spawn(cmd, encoding='utf-8', timeout=None)

            while True:
                # 捕获各类输出，顺序重要
                idx = child.expect([
                    r"(\d+)%",                               # 进度百分比
                    r"Enter password.*:",                    # 需要密码
                    r"\(Y\)es / \(N\)o / \(A\)lways.*\?",  # 文件冲突
                    r"Everything is Ok",
                    r"Wrong password",
                    r"Errors",
                    pexpect.EOF
                ], timeout=30)

                if idx == 0:
                    percent = int(child.match.group(1))
                    self.progress_signal.emit(self.index, percent)

                elif idx == 1:
                    # self.waiting = True
                    # self.password_status.emit(self.index, f"🔐 请输入【{self.file_path}】的压缩包密码")
                    # while self.waiting:
                    #     self.msleep(100)
                    
                    child.sendline(self.password)

                elif idx == 2:
                    self.waiting = True

                    # 获取整个冲突对话上下文，包括之前的描述部分
                    conflict_text = child.before + child.after

                    # 正则提取两个 Path 行（先提所有，后面判断是否有2个）
                    path_matches = re.findall(r'Path:\s+(.+)', conflict_text)

                    # 默认展示文字
                    local_path = archive_path = "未知"

                    if len(path_matches) >= 2:
                        local_path = path_matches[0].strip()
                        archive_path = path_matches[1].strip()

                    # 构造完整提示信息
                    prompt_message = (
                        f"<b>检测到文件冲突：</b><br><br>"
                        f"<span style='color:green;'><b>目标已存在文件：</b></span><br>{local_path}<br><br>"
                        f"<span style='color:orange;'><b>压缩包中的文件：</b></span><br>{archive_path}<br><br>"
                        f"<b>可选操作说明：</b><br>"
                        f"<table style='font-family: monospace; font-size: 13px;'>"
                        f"<tr><td style='color:#0099cc;'>• (Y)es</td>        <td>- 替换当前文件</td></tr>"
                        f"<tr><td style='color:#999999;'>• (N)o</td>         <td>- 跳过当前文件</td></tr>"
                        f"<tr><td style='color:#00aa00;'>• (A)lways</td>     <td>- 替换当前并自动替换后续所有冲突文件</td></tr>"
                        f"<tr><td style='color:#cc6600;'>• (S)kip all</td>   <td>- 跳过当前并自动跳过后续所有冲突文件</td></tr>"
                        f"<tr><td style='color:#9966cc;'>• A(u)to rename</td><td>- 自动为解压文件重命名，避免覆盖</td></tr>"
                        f"<tr><td style='color:#ff4444;'>• (Q)uit</td>       <td>- 退出解压过程</td></tr>"
                        f"</table>"
                    )

                    self.conflict_prompt.emit(self.index, prompt_message)

                    # 等待主线程的选择响应
                    while self.waiting:
                        self.msleep(100)

                    if self.response:
                        child.sendline(self.response)
                    else:
                        child.sendline('n')

                elif idx == 3:
                    self.progress_signal.emit(self.index, 100)
                    self.status_signal.emit(self.index, "✅ 完成")
                    break

                elif idx == 4:
                    child = pexpect.spawn(cmd, encoding='utf-8', timeout=None)

                elif idx == 6 or idx == 5:
                    self.status_signal.emit(self.index, f"❌ 异常 {str(e)}")
                    break

        except Exception as e:
            self.status_signal.emit(self.index, f"❌ 异常: {str(e)}")

class UnzipApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("批量解压 .7z 文件（串行执行）")
        self.resize(700, 500)

        self.file_items = []
        self.task_queue = deque()
        self.current_thread = None
        self.threads = []
        self.output_dir = ""

        layout = QVBoxLayout(self)

        # 密码输入
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("请输入密码（可选）")
        layout.addWidget(self.password_input)

        self.path_label = QLabel("未选择解压路径")
        self.path_label.setStyleSheet("color: lightgray;")
        layout.addWidget(self.path_label)

        # 按钮
        btn_layout = QHBoxLayout()
        self.select_files_btn = QPushButton("选择 .7z 文件")
        self.select_files_btn.clicked.connect(self.select_files)
        btn_layout.addWidget(self.select_files_btn)

        self.select_target_btn = QPushButton("选择解压目标路径")
        self.select_target_btn.clicked.connect(self.select_target)
        btn_layout.addWidget(self.select_target_btn)

        layout.addLayout(btn_layout)

        # 列表视图
        self.file_list = QListWidget()
        layout.addWidget(self.file_list)

        # 开始按钮
        self.start_btn = QPushButton("开始解压（逐个执行）")
        self.start_btn.clicked.connect(self.start_extract)
        layout.addWidget(self.start_btn)

        self.setLayout(layout)

    # 选择多个压缩文件，添加到 UI 中
    def select_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择 .7z 文件", "", "7z Files (*.7z)"
        )
        if files:
            self.file_list.clear()
            self.file_items.clear()
            for file_path in files:
                file_name = os.path.basename(file_path)

                # 自定义部件（文件名 + 进度条）
                item_widget = QWidget()
                vbox = QVBoxLayout(item_widget)
                vbox.setContentsMargins(0, 0, 0, 0)

                label = QLineEdit(file_name)
                label.setReadOnly(True)
                label.setFrame(False)
                vbox.addWidget(label)

                bar = QProgressBar()
                bar.setValue(0)
                bar.setFormat("%p%")
                vbox.addWidget(bar)

                list_item = QListWidgetItem()
                list_item.setSizeHint(item_widget.sizeHint())
                self.file_list.addItem(list_item)
                self.file_list.setItemWidget(list_item, item_widget)

                self.file_items.append((file_path, bar, label))

                self.start_btn.setEnabled(True)

    # 选择解压输出目录
    def select_target(self):
        folder = QFileDialog.getExistingDirectory(self, "选择解压目标路径")
        if folder:
            self.output_dir = folder
            self.path_label.setText(f"解压到：{folder}")

    # 构建任务队列并启动第一个解压任务
    def start_extract(self):
        if not self.output_dir:
            folder = QFileDialog.getExistingDirectory(self, "请选择解压目标路径")
            if not folder:
                return
            self.output_dir = folder

        password = self.password_input.text()

        # 禁用交互项
        self.select_files_btn.setEnabled(False)
        self.select_target_btn.setEnabled(False)
        self.password_input.setEnabled(False)
        self.start_btn.setEnabled(False)

        # 构建任务队列
        self.task_queue.clear()
        for idx, (file_path, bar, label) in enumerate(self.file_items):
            self.task_queue.append((idx, file_path, bar, password))

        self.run_next_task()

    # 启动一个解压任务线程
    def run_next_task(self):
        if not self.task_queue:
            # ✅ 全部完成，恢复交互
            self.select_files_btn.setEnabled(True)
            self.select_target_btn.setEnabled(True)
            self.password_input.setEnabled(True)
            return

        idx, file_path, bar, password = self.task_queue.popleft()

        # 启动线程执行解压
        thread = ExtractThread(idx, file_path, self.output_dir, password)
        self.current_thread = thread # ✅ 先保存引用，防止被 GC
        self.threads.append(thread)  # ✅ 把线程保存到列表中

        thread.progress_signal.connect(self.update_progress)
        thread.status_signal.connect(self.handle_task_done)
        thread.password_status.connect(self.handle_password_status)

        thread.conflict_prompt.connect(self.prompt_conflict)

        thread.start()

    # 更新进度条     
    def update_progress(self, index, percent):
        bar = self.file_items[index][1]
        bar.setValue(percent)

    # 一个文件解压完成后，标记和写log
    def handle_task_done(self, index, status):
        label = self.file_items[index][2]
        bar = self.file_items[index][1]
        # ✅ 日志记录
        self.write_log(label.text(), status)
        # 更新标记
        label.setText(f'{label.text()} {status}')
        bar.setFormat(status)
        # 运行下一个
        self.run_next_task()
        
    # 写log
    def write_log(self, name, status):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {name} {status}\n"

        # ✅ 日志文件保存在用户选择的输出目录下
        log_path = os.path.join(self.output_dir, "log.txt")
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(log_line)
        except Exception as e:
            print(f"❌ 写入日志失败: {e}")

    # 弹出密码输入框，获取用户输入
    def handle_password_status(self, index, message):
        text, ok = QInputDialog.getText(self, "密码输入", message)
        if ok and text:
            self.current_thread.set_password(text)
        else:
            self.current_thread.set_password(None)

    # 弹出冲突提示框，等待用户选择
    def prompt_conflict(self, index, message):
        box = QMessageBox(self)
        box.setWindowTitle("文件冲突")
        box.setText(message)
        box.setIcon(QMessageBox.Warning)
        box.setTextFormat(Qt.RichText)

        # 添加所有按钮
        yes_btn    = box.addButton("Yes", QMessageBox.YesRole)
        no_btn     = box.addButton("No", QMessageBox.NoRole)
        always_btn = box.addButton("Always", QMessageBox.ActionRole)
        skip_btn   = box.addButton("Skip all", QMessageBox.ActionRole)
        rename_btn = box.addButton("Auto Rename", QMessageBox.ActionRole)
        quit_btn   = box.addButton("Quit", QMessageBox.RejectRole)

        box.setDefaultButton(quit_btn)

        box.exec_()

        response = 'n'
        if box.clickedButton() == yes_btn:
            response = 'y'
        elif box.clickedButton() == no_btn:
            response = 'n'
        elif box.clickedButton() == always_btn:
            response = 'a'
        elif box.clickedButton() == skip_btn:
            response = 's'
        elif box.clickedButton() == rename_btn:
            response = 'u'
        elif box.clickedButton() == quit_btn:
            response = 'q'

        self.current_thread.set_response(response)

    # 关闭提示
    def closeEvent(self, event):
        reply = QMessageBox.question(
            self,
            "退出确认",
            "你确定要关闭程序吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            event.accept()
        else:
            event.ignore()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = UnzipApp()
    win.show()
    sys.exit(app.exec_())
