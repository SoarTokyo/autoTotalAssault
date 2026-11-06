# SPDX-License-Identifier: GPL-3.0-or-later
"""简单中文录制界面；通过独立子进程录制，避免阻塞 Qt 主线程。"""
from dataclasses import replace
from datetime import datetime
import json
import importlib.util
import os
from pathlib import Path
import sys

from PySide6.QtCore import QProcess, QProcessEnvironment, QTemporaryDir, QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget, QTabWidget,
)

from .config import Config
from .paths import resolve_project_path, portable_path
from .naming import recording_name


ROOT = Path(__file__).resolve().parents[2]
STATES = {'initializing':'正在初始化输入监听与视频组件……','armed': '等待游戏窗口切到前台', 'recording': '正在录制',
          'restarting':'正在清空本段内容并重启录制……',
          'paused': '录制已暂停（不会暂停游戏）', 'resume_pending': '等待切回游戏窗口继续录制',
          'unfocused': '游戏已失去焦点，暂不记录输入或画面', 'stopping': '正在停止并保存视频',
          'completed': '录制完成', 'aborted': '录制已中止', 'failed': '录制失败'}


def configure_font(app):
    # 显式加载系统中文字体，也使无屏幕渲染测试显示中文而不是方框。
    font_root = Path(os.environ.get('WINDIR') or os.environ.get('SystemRoot') or '.') / 'Fonts'
    family = 'Microsoft YaHei UI'
    for name in ('msyh.ttc', 'msyhbd.ttc'):
        path = font_root / name
        if path.exists():
            ident = QFontDatabase.addApplicationFont(str(path))
            families = QFontDatabase.applicationFontFamilies(ident)
            if families and name == 'msyh.ttc':
                family = families[0]
    app.setFont(QFont(family, 10))


class RecorderWindow(QMainWindow):
    def __init__(self, window_provider=None, preference_path=None, key_settings_path=None,hotkey_state_reader=None):
        super().__init__()
        enable_start_hotkey=window_provider is None or hotkey_state_reader is not None
        self.start_hotkey_watcher=None
        self.key_settings_path=Path(key_settings_path) if key_settings_path else (ROOT/'.local/recorder.json' if window_provider is None else None)
        self.preference_path=Path(preference_path) if preference_path else (ROOT/'.local/gui_preferences.json' if window_provider is None else None)
        self.preferred_window_title=None
        if self.preference_path and self.preference_path.exists():
            try:
                self.preferred_window_title=json.loads(self.preference_path.read_text(encoding='utf-8')).get('game_window_title')
            except (OSError,ValueError):
                pass
        if window_provider is None:
            from .windows import list_windows
            window_provider = list_windows
        self.window_provider = window_provider
        self.setWindowTitle('蔚蓝档案 · 战斗录制与技能轴复现')
        self.resize(1120, 900)
        self._recording_busy=False
        self._analysis_busy=False
        self._replay_busy=False
        self.default_config = Config.load(ROOT / 'examples' / 'recorder.json').for_recording()
        self.base_config=self.default_config
        key_settings_warning=None
        if self.key_settings_path and self.key_settings_path.exists():
            try:
                from .keybindings import validate_bindings
                original=Config.load(self.key_settings_path)
                migrated=original.without_game_exit_hotkey()
                self.base_config=validate_bindings(migrated).for_recording()
                if migrated!=original:
                    from .keybindings_gui import save_config
                    try:
                        save_config(self.key_settings_path,self.base_config)
                        key_settings_warning='旧配置的 F10 录制热键已迁移，F10 留给游戏退出。'
                    except OSError as error:
                        key_settings_warning=f'F10 录制热键已在本次运行迁移，但配置未能保存：{error}'
            except (OSError,ValueError) as error:key_settings_warning=f'本地键位配置未加载，已使用默认值：{error}'
        self.temp = QTemporaryDir()
        self.session_path = None
        self.final_state = None
        self.recording_log_path=None
        self.recorded_frames=0
        self.recording_started=False
        self.recording_forced_exit=False
        self.pending_close = False
        self.stdout_pending = b''
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(ROOT))
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert('PYTHONIOENCODING', 'utf-8')
        environment.insert('PYTHONUNBUFFERED', '1')
        self.process.setProcessEnvironment(environment)
        self.process.readyReadStandardOutput.connect(self.read_stdout)
        self.process.readyReadStandardError.connect(self.read_stderr)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        self.recording_exit_timer=QTimer(self)
        self.recording_exit_timer.setSingleShot(True)
        self.recording_exit_timer.timeout.connect(self.terminate_finished_recorder)
        self.recording_kill_timer=QTimer(self)
        self.recording_kill_timer.setSingleShot(True)
        self.recording_kill_timer.timeout.connect(self.kill_finished_recorder)
        self.analysis_process = QProcess(self)
        self.analysis_process.setWorkingDirectory(str(ROOT))
        self.analysis_process.setProcessEnvironment(environment)
        self.analysis_process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.analysis_process.readyReadStandardOutput.connect(self.read_analysis)
        self.analysis_process.finished.connect(self.analysis_finished)
        self.analysis_process.errorOccurred.connect(self.analysis_error)
        self.analysis_pending = b''
        self.analysis_output = None
        self.analysis_script_path = None
        self.analysis_log_path = None
        self.analysis_stage=None
        self.analysis_request=None
        self.team_assets_result=None

        central = QWidget()
        self.setCentralWidget(central)
        outer=QVBoxLayout(central)
        self.tabs=QTabWidget()
        self.tabs.tabBar().setExpanding(True)
        self.tabs.setStyleSheet('QTabBar::tab { font-size: 18px; font-weight: 600; padding: 14px 32px; }')
        outer.addWidget(self.tabs)
        recording_panel=QWidget()
        recording_panel.setMinimumWidth(500)
        self.tabs.addTab(recording_panel,'战斗录制')
        layout = QVBoxLayout(recording_panel)
        title = QLabel('战斗录制')
        title.setStyleSheet('font-size: 24px; font-weight: 600; margin: 8px 0;')
        layout.addWidget(title)
        hint = QLabel('选择游戏窗口 → 修改录制文件名 → 切回游戏按 F8 开始；也可点击“开始录制”。\n'
                      '操作完成后，停止并保存→自动分析已有录制→选择刚才录制的文件；技能轴按录制文件名自动保存到 scripts。\n'
                      '只记录目标窗口；切换“技能轴编辑与复现”标签页编辑并复现。任务运行期间锁定另一页。')
        hint.setWordWrap(True)
        self.recording_hint=hint
        layout.addWidget(hint)

        group = QGroupBox('录制设置')
        form = QFormLayout(group)
        self.windows = QComboBox()
        self.windows.currentIndexChanged.connect(self.update_selection)
        self.windows.setMinimumContentsLength(18)
        self.windows.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.refresh = QPushButton('刷新窗口')
        self.refresh.clicked.connect(self.refresh_windows)
        row = QHBoxLayout()
        row.addWidget(self.windows, 1)
        row.addWidget(self.refresh)
        form.addRow('游戏窗口', row)

        self.output = QLineEdit('recordings')
        self.output.setToolTip('相对路径以项目目录为基准，例如 recordings；也可选择外部目录。')
        self.browse = QPushButton('选择目录')
        self.browse.clicked.connect(self.choose_output)
        row = QHBoxLayout()
        row.addWidget(self.output, 1)
        row.addWidget(self.browse)
        form.addRow('保存目录', row)
        self.recording_name=QLineEdit()
        self.recording_name.setPlaceholderText('例如：总力战·礼服日奈第一轴（留空自动命名）')
        self.recording_name.setToolTip('支持中文，用作本次录制的文件夹名称；内部文件名由程序管理。重启录制会清空当前未结束的会话并沿用此名称，不覆盖已经结束的历史录制。')
        form.addRow('录制文件名',self.recording_name)

        video_row=QHBoxLayout()
        video_row.addWidget(QLabel('连续视频'),1)
        video_row.addWidget(QLabel('录制帧率'))
        self.video_fps=QComboBox()
        for fps in (5,10,20,30):self.video_fps.addItem(f'{fps} FPS',fps)
        selected_fps=self.base_config.video_fps if self.base_config.video_fps in (5,10,20,30) else 30
        self.video_fps.setCurrentIndex(self.video_fps.findData(selected_fps))
        self.video_fps.currentIndexChanged.connect(self.save_video_fps)
        video_row.addWidget(self.video_fps)
        form.addRow('视频录制',video_row)
        self.key_settings_button = QPushButton('键位设置…')
        self.key_settings_button.clicked.connect(self.open_key_settings)
        video_row.addWidget(self.key_settings_button)
        layout.addWidget(group)
        self.settings = group

        row = QHBoxLayout()
        self.start_button = QPushButton('开始录制')
        self.start_button.setMinimumHeight(38)
        self.start_button.clicked.connect(self.start_recording)
        self.stop_button = QPushButton('停止并保存')
        self.stop_button.clicked.connect(lambda: self.send_control('stop'))
        self.abort_button = QPushButton('重启录制')
        self.abort_button.setToolTip('丢弃本次尚未结束的全部输入、视频和日志，保留同名会话并直接继续采集；不会重开游戏战斗。')
        self.abort_button.clicked.connect(self.restart_recording)
        self.open_button = QPushButton('打开保存目录')
        self.open_button.clicked.connect(self.open_folder)
        for button in (self.start_button, self.stop_button, self.abort_button, self.open_button):
            row.addWidget(button)
        layout.addLayout(row)
        analysis_row = QHBoxLayout()
        self.analyze_button = QPushButton('自动分析已有录制…')
        self.analyze_button.clicked.connect(self.start_analysis)
        self.analysis_folder_button = QPushButton('打开分析结果')
        self.analysis_folder_button.setEnabled(False)
        self.analysis_folder_button.clicked.connect(self.open_analysis)
        analysis_row.addWidget(self.analyze_button)
        analysis_row.addWidget(self.analysis_folder_button)
        layout.addLayout(analysis_row)
        self.state_label = QLabel('就绪')
        self.state_label.setStyleSheet('font-size: 16px; font-weight: 600; margin-top: 6px;')
        layout.addWidget(self.state_label)
        self.hotkeys = QLabel()
        self.hotkeys.setWordWrap(True)
        layout.addWidget(self.hotkeys)
        self.session_label = QLabel('尚未创建录制会话')
        self.session_label.setWordWrap(True)
        self.session_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.session_label)
        self.recording_summary=QLabel('尚未采集：0 帧画面，0 条输入')
        layout.addWidget(self.recording_summary)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1500)
        self.log.setPlaceholderText('录制状态和错误会显示在这里。')
        if key_settings_warning:self.log.appendPlainText(key_settings_warning)
        layout.addWidget(self.log, 1)
        from .replay_gui import ReplayDialog
        self.replay_panel=ReplayDialog(self,None,self.base_config.keymap.copy(),embedded=True)
        self.replay_panel.setMinimumWidth(650)
        self.replay_panel.before_start=self.prepare_replay
        self.replay_panel.busyChanged.connect(self.replay_busy_changed)
        self.tabs.addTab(self.replay_panel,'技能轴编辑与复现')
        self.set_busy(False)
        self.update_hotkeys()
        self.refresh_windows()
        self.window_refresh_timer=QTimer(self)
        self.window_refresh_timer.setInterval(2000)
        self.window_refresh_timer.timeout.connect(self.refresh_windows_when_idle)
        self.window_refresh_timer.start()
        if enable_start_hotkey:
            try:
                from .gui_hotkey import StartHotkeyWatcher
                self.start_hotkey_watcher=StartHotkeyWatcher(self.base_config.start_key,self,state_reader=hotkey_state_reader)
                self.start_hotkey_watcher.pressed.connect(self.on_start_hotkey)
                self.state_label.setText('热键已待命，切回游戏即可开始录制')
            except Exception as error:
                self.log.appendPlainText(f'开始热键待命失败，请使用“开始录制”按钮：{error}')
        self.tabs.currentChanged.connect(lambda _:self.update_hotkeys())

    def update_hotkeys(self):
        config = self.base_config
        prefix='录制页已待命：游戏在前台时可直接按开始热键。\n' if not self._recording_busy else ''
        if self._analysis_busy or self._replay_busy:prefix='当前任务进行中，录制开始热键暂不可用。\n'
        self.hotkeys.setText(prefix+f'游戏内热键：{config.start_key.upper()} 开始，{config.stop_key.upper()} 停止，'
                             f'{config.abort_key.upper()} 重启并丢弃本段内容。失焦后仍可重启或停止。')
        lines=self.recording_hint.text().split('\n')
        lines[0]=f'选择游戏窗口 → 修改录制文件名 → 切回游戏按 {config.start_key.upper()} 开始；也可点击“开始录制”。'
        self.recording_hint.setText('\n'.join(lines))

    def is_game_foreground(self):
        from .windows import Window
        hwnd=self.windows.currentData()
        return hwnd is not None and Window(hwnd).snapshot().focused

    def on_start_hotkey(self):
        if (self.pending_close or not self.isVisible() or self.tabs.currentIndex()!=0 or QApplication.activeModalWidget() is not None or
                self._recording_busy or self._analysis_busy or self._replay_busy or
                self.process.state()!=QProcess.ProcessState.NotRunning or self.analysis_process.state()!=QProcess.ProcessState.NotRunning):return
        try:
            if not self.is_game_foreground():return
        except Exception as error:
            self.log.appendPlainText(f'开始热键未执行，目标窗口不可用：{error}');return
        self.start_recording()

    def set_busy(self, busy):
        self._recording_busy=busy
        self.stop_button.setEnabled(busy)
        self.recording_state='armed' if busy else 'idle'
        self.abort_button.setText('重启录制')
        self.abort_button.setEnabled(False)
        self.sync_controls()
        self.update_hotkeys()

    def sync_controls(self):
        busy=self._recording_busy or self._analysis_busy or self._replay_busy
        self.settings.setEnabled(not busy)
        self.start_button.setEnabled(not busy and self.windows.currentData() is not None)
        self.analyze_button.setEnabled(not busy)
        self.replay_panel.start_button.setEnabled(not busy)
        self.tabs.setTabEnabled(0,not self._replay_busy)
        self.tabs.setTabEnabled(1,not (self._recording_busy or self._analysis_busy))
        if busy:self.tabs.setCurrentIndex(1 if self._replay_busy else 0)

    def replay_busy_changed(self,value):
        self._replay_busy=value
        self.sync_controls()
        if not value and self.pending_close:QTimer.singleShot(0,self.close)

    def prepare_replay(self):
        if self._recording_busy or self._analysis_busy or self._replay_busy:return False
        hwnd=self.windows.currentData()
        if hwnd is None:
            QMessageBox.warning(self,'请选择窗口','请先在“战斗录制”标签页选择要复现的游戏窗口。')
            return False
        self.replay_panel.hwnd=hwnd
        self.replay_panel.keymap=self.base_config.keymap.copy()
        return True

    def refresh_windows(self):
        previous = self.windows.currentData()
        previous_record=self.windows.currentData(Qt.ItemDataRole.UserRole+1)
        self.windows.blockSignals(True)
        self.windows.clear()
        try:
            own_pid = os.getpid()
            items = [item for item in self.window_provider() if item['pid'] != own_pid]
            self.windows.addItem('请选择游戏窗口', None)
            for item in items:
                self.windows.addItem(f"{item['title']}  ·  HWND {item['hwnd']}  ·  PID {item['pid']}", item['hwnd'])
                self.windows.setItemData(self.windows.count()-1,item,Qt.ItemDataRole.UserRole+1)
            same_window=any(item==previous_record for item in items)
            if previous is not None and same_window and self.windows.findData(previous) >= 0:
                self.windows.setCurrentIndex(self.windows.findData(previous))
            else:
                candidates=[i+1 for i,item in enumerate(items) if item['title']==self.preferred_window_title]
                if not candidates:
                    candidates=[i+1 for i,item in enumerate(items) if item['title'].strip().lower() in ('blue archive','蔚蓝档案','蔚藍檔案')]
                if not candidates:
                    candidates = [i+1 for i,item in enumerate(items)
                                  if any(word in item['title'].lower() for word in ('blue archive', '蔚蓝档案', '蔚藍檔案'))
                                  and '录制器' not in item['title'] and 'recorder' not in item['title'].lower()]
                if len(candidates) == 1:
                    self.windows.setCurrentIndex(candidates[0])
        except Exception as error:
            self.log.appendPlainText(f'读取窗口列表失败：{error}')
        finally:
            self.windows.blockSignals(False)
            self.update_selection()

    def refresh_windows_when_idle(self):
        if (self.process.state()==QProcess.ProcessState.NotRunning and
            self.analysis_process.state()==QProcess.ProcessState.NotRunning and not self._replay_busy and not self.windows.view().isVisible()):
            self.refresh_windows()

    def update_selection(self, *_):
        item=self.windows.currentData(Qt.ItemDataRole.UserRole+1)
        if item and self.preference_path and item['title']!=self.preferred_window_title:
            self.preferred_window_title=item['title']
            try:
                self.preference_path.parent.mkdir(parents=True,exist_ok=True)
                self.preference_path.write_text(json.dumps({'game_window_title':item['title']},ensure_ascii=False,indent=2),encoding='utf-8')
            except OSError as error:
                self.log.appendPlainText(f'窗口选择偏好保存失败：{error}')
        self.sync_controls()

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, '选择录制保存目录', str(resolve_project_path(self.output.text(),ROOT)))
        if path:
            self.output.setText(portable_path(path,ROOT))

    def open_key_settings(self):
        if self._replay_busy or self.process.state()!=QProcess.ProcessState.NotRunning or self.analysis_process.state()!=QProcess.ProcessState.NotRunning:
            return
        from .keybindings_gui import KeyBindingsDialog
        dialog=KeyBindingsDialog(self,self.recording_config(),self.default_config,self.key_settings_path)
        if dialog.exec()==dialog.DialogCode.Accepted:
            self.base_config=dialog.result_config
            if self.start_hotkey_watcher:self.start_hotkey_watcher.set_key(self.base_config.start_key)
            self.replay_panel.keymap=self.base_config.keymap.copy()
            self.update_hotkeys()
            self.log.appendPlainText('键位设置已应用到后续录制和界面回放；已有录制按其历史映射分析。')

    def save_video_fps(self):
        self.base_config=replace(self.base_config,video_fps=self.video_fps.currentData())
        if self.key_settings_path:
            from .storage import write_json
            try:
                self.key_settings_path.parent.mkdir(parents=True,exist_ok=True)
                write_json(self.key_settings_path,self.recording_config().to_dict())
            except OSError as error:
                self.log.appendPlainText(f'录制帧率保存失败，本次选择仍有效：{error}')

    def recording_config(self):
        # 旧配置只继承键位等通用设置，不让历史采集选项改变界面的固定视频方案。
        return replace(self.base_config.for_recording('video'),capture_backend='mss',
                       video_fps=self.video_fps.currentData(),video_codec='libx264',video_crf=18,video_queue_size=8,sample_hz=5)

    def start_recording(self):
        if (self._replay_busy or self._recording_busy or self._analysis_busy or self.process.state() != QProcess.ProcessState.NotRunning
                or self.analysis_process.state() != QProcess.ProcessState.NotRunning or self.windows.currentData() is None):
            if self._recording_busy or self.process.state()!=QProcess.ProcessState.NotRunning:
                reason='上一段录制还在运行或保存，请等待录制进程结束后重试'
            elif self._replay_busy:reason='正在复现，请先停止复现'
            elif self._analysis_busy or self.analysis_process.state()!=QProcess.ProcessState.NotRunning:
                reason='正在分析，请等待分析结束'
            else:reason='尚未选中游戏窗口，请刷新并选择窗口'
            self.log.appendPlainText('本次没有启动新录制：'+reason)
            self.persist_recording_log('开始录制请求未执行：'+reason)
            return
        try:
            if not self.output.text().strip():
                raise ValueError('请选择保存目录')
            if importlib.util.find_spec('av') is None:
                raise RuntimeError('缺少视频编码依赖 PyAV。请在项目目录执行：.\\.venv\\Scripts\\python.exe -m pip install "av>=14,<17"')
            config = self.recording_config()
            if not self.temp.isValid():
                raise RuntimeError('无法创建临时配置目录')
            config_path = Path(self.temp.path()) / 'recording.json'
            config_path.write_text(json.dumps(config.to_dict(), ensure_ascii=False), encoding='utf-8')
            output = resolve_project_path(self.output.text(),ROOT)
            name=recording_name(self.recording_name.text())
            if name and (output/name).exists():
                raise ValueError('该录制文件名已存在，请换一个名称，已有录制不会覆盖')
            output.mkdir(parents=True, exist_ok=True)
            executable = Path(sys.executable)
            if executable.name.lower() == 'pythonw.exe':
                executable = executable.with_name('python.exe')
            self.session_path, self.final_state, self.stdout_pending = None, None, b''
            self.recorded_frames=0
            self.recording_started=False
            self.recording_forced_exit=False
            self.recording_exit_timer.stop()
            self.recording_kill_timer.stop()
            self.recording_summary.setText('尚未开始录制：0 帧画面，0 条输入')
            self.log.clear()
            log_root=ROOT/'logs'
            log_root.mkdir(parents=True,exist_ok=True)
            self.recording_log_path=log_root/f'recording-{datetime.now():%Y%m%d-%H%M%S-%f}.log'
            self.recording_log_path.write_text(f'准备录制：{name or "自动命名"}\n保存目录：{output}\n目标 HWND：{self.windows.currentData()}\n',encoding='utf-8')
            self.log.appendPlainText(f'本次诊断日志：{self.recording_log_path}')
            self.session_label.setText('正在创建会话……')
            self.state_label.setText('正在启动录制进程……')
            self.set_busy(True)
            self.process.setWorkingDirectory(str(ROOT))
            args=['-u', '-m', 'ba_recorder', 'record', '--hwnd',
                str(self.windows.currentData()), '--config', str(config_path), '--output', portable_path(output,ROOT),
                '--control-stdin','--start-when-focused']
            if name:args.extend(['--name',name])
            self.process.start(str(executable),args)
        except Exception as error:
            self.set_busy(False)
            self.log.appendPlainText(f'本次没有启动新录制：{error}')
            self.persist_recording_log(f'开始录制请求未执行：{error}')
            QMessageBox.warning(self, '无法启动录制', str(error))

    def restart_recording(self):
        if self.recording_state in ('recording','unfocused','paused'):
            self.send_control('restart')

    def send_control(self, command):
        if self.process.state() == QProcess.ProcessState.Running:
            self.process.write((json.dumps({'command': command}) + '\n').encode('utf-8'))
            self.state_label.setText({'stop':'正在停止并保存……','restart':'正在清空本段内容并重启录制……','pause':'正在暂停录制……',
                                      'resume':'正在继续录制……'}.get(command,'正在处理录制请求……'))
            self.abort_button.setEnabled(False)
            if command == 'stop':self.stop_button.setEnabled(False)

    def read_stdout(self):
        self.stdout_pending += bytes(self.process.readAllStandardOutput())
        while b'\n' in self.stdout_pending:
            line, self.stdout_pending = self.stdout_pending.split(b'\n', 1)
            self.handle_line(line.decode('utf-8', errors='replace').rstrip('\r'))

    def handle_line(self, line):
        self.persist_recording_log(line)
        if not line.startswith('@status '):
            self.log.appendPlainText(line)
            return
        try:
            message = json.loads(line[len('@status '):])
            if message.get('reset'):
                self.log.clear()
                if self.recording_log_path:
                    try:self.recording_log_path.write_text('本次录制已重启，之前的诊断内容已清空。\n'+line+'\n',encoding='utf-8')
                    except OSError as error:
                        self.recording_log_path=None
                        self.log.appendPlainText(f'重启后的诊断日志无法写入：{error}')
            state = message['state']
            self.recording_state=state
            self.abort_button.setText('重启录制')
            self.abort_button.setEnabled(state in ('recording','unfocused','paused'))
            self.state_label.setText(STATES.get(state, state))
            if state in ('recording','unfocused','paused','resume_pending'):
                self.recording_started=True
            if 'recording_started' in message:
                self.recording_started=message['recording_started']
            if 'captured_frames' in message:
                self.recorded_frames=message['captured_frames']
                self.recording_summary.setText(f'已保存画面：{self.recorded_frames} 帧；已保存输入：{message.get("input_events",0)} 条')
            if state=='armed':
                if message.get('start_request_pending'):
                    self.state_label.setText(message.get('reason_text') or '开始请求已保留：等待选中的游戏窗口进入前台。')
                else:
                    self.state_label.setText(f'尚未开始录制（已等待 {message.get("wait_seconds",0)} 秒）：请切回选中的游戏窗口并保持前台。')
            if message.get('session_path'):
                self.session_path = resolve_project_path(message['session_path'],ROOT)
                self.session_label.setText(f'当前会话：{self.session_path}')
            if state in ('completed', 'aborted', 'failed'):
                self.final_state = state
                if self.process.state()!=QProcess.ProcessState.NotRunning:
                    self.process.closeWriteChannel()
                    if not self.recording_exit_timer.isActive():self.recording_exit_timer.start(2000)
                if 'captured_frames' in message and not self.recorded_frames:
                    self.state_label.setText('没有录到画面：'+(message.get('reason_text') or '请查看诊断日志'))
                    self.log.appendPlainText('本次没有视频画面。会话目录仅保留已有输入或诊断信息；请确认录制状态和帧数开始增长。')
            if state == 'stopping':
                self.stop_button.setEnabled(False)
        except (ValueError, KeyError) as error:
            self.log.appendPlainText(f'状态消息无法解析：{error}')

    def read_stderr(self):
        text = bytes(self.process.readAllStandardError()).decode('utf-8', errors='replace')
        if text:
            self.persist_recording_log(text.rstrip())
            self.log.appendPlainText(text.rstrip())

    def persist_recording_log(self,text):
        if self.recording_log_path is None:return
        try:
            with self.recording_log_path.open('a',encoding='utf-8') as handle:
                handle.write(text+'\n')
        except OSError as error:
            self.recording_log_path=None
            self.log.appendPlainText(f'诊断日志写入失败：{error}')

    def finished(self, exit_code, _exit_status):
        self.read_stdout()
        self.read_stderr()
        if self.stdout_pending:
            self.handle_line(self.stdout_pending.decode('utf-8', errors='replace').strip())
            self.stdout_pending = b''
        if self.recording_forced_exit and self.final_state=='completed' and self.recorded_frames>0:
            self.state_label.setText('录制文件已保存，后台退出已处理')
        elif not self.final_state or exit_code != 0:
            self.state_label.setText('录制失败或进程异常退出，请查看日志')
        self.set_busy(False)
        self.recording_exit_timer.stop()
        self.recording_kill_timer.stop()
        text=f'本段录制已结束，已恢复热键待命。修改录制文件名后，切回游戏按 {self.base_config.start_key.upper()} 即可录制下一段；同名会话不会覆盖。'
        self.log.appendPlainText(text)
        self.persist_recording_log(text)
        if self.pending_close:
            QTimer.singleShot(0, self.close)

    def terminate_finished_recorder(self):
        if self.process.state()!=QProcess.ProcessState.NotRunning:
            self.recording_forced_exit=True
            self.log.appendPlainText('录制文件已保存，但子进程仍未退出，正在结束进程。')
            self.process.terminate()
            self.recording_kill_timer.start(1000)

    def kill_finished_recorder(self):
        if self.process.state()!=QProcess.ProcessState.NotRunning:
            self.recording_forced_exit=True
            self.process.kill()

    def process_error(self, error):
        self.persist_recording_log('录制进程错误：'+self.process.errorString())
        self.log.appendPlainText(f'录制进程错误：{self.process.errorString()}')
        if error == QProcess.ProcessError.FailedToStart:
            self.state_label.setText('无法启动录制进程')
            self.set_busy(False)
            if self.pending_close:
                QTimer.singleShot(0, self.close)

    def open_folder(self):
        path = self.session_path if self.session_path and self.session_path.exists() else resolve_project_path(self.output.text(),ROOT)
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def start_analysis(self):
        if self._replay_busy or self._recording_busy or self._analysis_busy:return
        if self.process.state() != QProcess.ProcessState.NotRunning or self.analysis_process.state() != QProcess.ProcessState.NotRunning:
            return
        path = QFileDialog.getExistingDirectory(self, '选择包含 session.json 的录制会话目录', str(ROOT / 'recordings'))
        if not path:
            return
        if not (Path(path) / 'session.json').exists():
            QMessageBox.warning(self, '请选择具体会话', '请选择包含 session.json 的具体录制文件夹，而不是 recordings 根目录。')
            return
        profiles = sorted((ROOT / 'profiles').glob('local-*/profile.json'), key=lambda p:p.stat().st_mtime, reverse=True)
        if profiles:
            visual_profile = profiles[0]
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, '选择已经校准的视觉配置', str(ROOT / 'profiles'), 'JSON 配置 (*.json)')
            if not chosen:
                return
            visual_profile = Path(chosen)
        self.log.clear()
        log_root=ROOT/'logs'
        log_root.mkdir(parents=True,exist_ok=True)
        self.analysis_log_path=log_root/f'analysis-{datetime.now():%Y%m%d-%H%M%S-%f}.log'
        self.analysis_request=dict(session=path,profile=visual_profile)
        self.persist_analysis_log(f'录制目录：{path}\n视觉配置：{visual_profile}')
        self.log.appendPlainText(f'分析诊断日志：{self.analysis_log_path}')
        self.analysis_output = None
        self.analysis_script_path = None
        self.analysis_pending = b''
        self.analysis_folder_button.setEnabled(False)
        self._analysis_busy=True
        self.sync_controls()
        self.start_team_assets('catalog')

    def start_team_assets(self,stage,*,refresh=False):
        import uuid
        self.analysis_stage=stage
        self.team_assets_result=Path(self.temp.path())/f'team-assets-{uuid.uuid4().hex}.json'
        executable=Path(sys.executable)
        if executable.name.lower()=='pythonw.exe':executable=executable.with_name('python.exe')
        args=['-u','-m','ba_recorder.team_assets','--library',str(ROOT/'.local/gamekee/students'),
              'list' if stage=='catalog' else 'ensure',
              '--output',str(self.team_assets_result)]
        if refresh:args.append('--refresh')
        if stage=='ensure':args.extend(['--team',str(self.analysis_request['team_path'])])
        self.state_label.setText('正在准备可选学生目录……' if stage=='catalog' else '正在校验并补齐所选学生图片……')
        self.analysis_process.start(str(executable),args)

    def team_assets_finished(self,exit_code):
        try:
            if self.pending_close:
                self.cancel_analysis_preparation();QTimer.singleShot(0,self.close);return
            if exit_code!=0:raise ValueError('队伍素材准备未完成，请查看下方日志后重试')
            result=json.loads(self.team_assets_result.read_text(encoding='utf-8'))
            if self.analysis_stage=='catalog':
                from .analysis_team import validate_team
                from .analysis_team_gui import AnalysisTeamDialog
                from .storage import write_json
                students=result['students']
                if len(students)<6:raise ValueError('可选学生目录不足六名，请检查网络并刷新目录')
                team_cache=ROOT/'.local/analysis_team.json';previous=None
                if team_cache.exists():
                    try:previous=json.loads(team_cache.read_text(encoding='utf-8'))
                    except (OSError,ValueError):pass
                dialog=AnalysisTeamDialog(self,students,previous)
                answer=dialog.exec()
                if answer==getattr(AnalysisTeamDialog,'Refresh',2):
                    self.start_team_assets('catalog',refresh=True);return
                if answer!=dialog.DialogCode.Accepted:
                    self.cancel_analysis_preparation();return
                team=validate_team(dialog.result_team,students)
                team_path=Path(self.temp.path())/'analysis-team.json';write_json(team_path,team)
                self.analysis_request.update(team=team,team_path=team_path)
                try:team_cache.parent.mkdir(parents=True,exist_ok=True);write_json(team_cache,team)
                except OSError as error:self.log.appendPlainText('本次队伍已确认，但偏好未保存：'+str(error))
                self.log.appendPlainText('已选择六人队伍：'+'、'.join(s['name'] for s in team['students']))
                self.start_team_assets('ensure');return
            from .storage import write_json
            write_json(self.analysis_request['team_path'],result['team'])
            self.launch_selected_analysis()
        except (OSError,ValueError,KeyError,TypeError) as error:
            self.log.appendPlainText('无法开始分析：'+str(error));self.persist_analysis_log(str(error))
            self.cancel_analysis_preparation('队伍素材准备失败，请查看日志')

    def cancel_analysis_preparation(self,message='已取消分析'):
        self._analysis_busy=False;self.analysis_stage=None;self.analysis_request=None
        self.state_label.setText(message);self.sync_controls()

    def launch_selected_analysis(self):
        request=self.analysis_request
        executable=Path(sys.executable)
        if executable.name.lower()=='pythonw.exe':executable=executable.with_name('python.exe')
        self.analysis_stage='analyze'
        self.state_label.setText('正在自动分析截图与操作，请稍候……')
        self.log.appendPlainText('普通学生图片已校验。本地离线分析，不调用大模型；缺少专用素材的阶段保留待核对。')
        self.analysis_process.start(str(executable), ['-u', '-m', 'ba_recorder', 'auto-analyze', str(request['session']),
                                                   '--visual-profile', str(request['profile']), '--output', str(ROOT / 'analyses'),
                                                   '--scripts-output',str(ROOT/'scripts'),'--team',str(request['team_path'])])

    def persist_analysis_log(self,text):
        if self.analysis_log_path is None:return
        try:
            with self.analysis_log_path.open('a',encoding='utf-8') as handle:handle.write(text+'\n')
        except OSError as error:
            self.analysis_log_path=None
            self.log.appendPlainText(f'分析诊断日志写入失败：{error}')

    def read_analysis(self):
        self.analysis_pending += bytes(self.analysis_process.readAllStandardOutput())
        while b'\n' in self.analysis_pending:
            line, self.analysis_pending = self.analysis_pending.split(b'\n', 1)
            text = line.decode('utf-8', errors='replace').rstrip('\r')
            self.persist_analysis_log(text)
            if text.startswith('@analysis '):
                try:
                    message=json.loads(text[len('@analysis '):])
                    self.analysis_output = Path(message['output'])
                    self.analysis_script_path=Path(message['script_path']) if message.get('script_path') else None
                except (ValueError,KeyError) as error:
                    self.log.appendPlainText(f'分析结果消息无效：{error}')
            else:
                self.log.appendPlainText(text)

    def analysis_finished(self, exit_code, _):
        self.read_analysis()
        if self.analysis_pending:
            self.persist_analysis_log(self.analysis_pending.decode('utf-8',errors='replace'))
            self.log.appendPlainText(self.analysis_pending.decode('utf-8',errors='replace'))
            self.analysis_pending = b''
        self.persist_analysis_log(f'分析进程退出码：{exit_code}')
        if self.analysis_stage in ('catalog','ensure'):
            self.team_assets_finished(exit_code);return
        self._analysis_busy=False;self.analysis_stage=None;self.analysis_request=None
        self.set_busy(False)
        self.analysis_folder_button.setEnabled(exit_code == 0 and self.analysis_output is not None)
        self.state_label.setText('自动分析完成' if exit_code == 0 and self.analysis_output else '自动分析失败，请查看日志')
        if self.analysis_output:
            self.log.appendPlainText(f'结果目录：{self.analysis_output}')
            if self.analysis_script_path:
                self.log.appendPlainText(f'技能轴已保存：{self.analysis_script_path}\n请在复现页加载此文件，核对后保存再执行。')
            try:
                flow=json.loads((self.analysis_output/'card_flow.json').read_text(encoding='utf-8'))
                roster='、'.join(s['identity'] for s in flow['roster']['slots'] if s['identity']) or '证据不足'
                hand='、'.join(flow['initial_hand']['cards']) if flow.get('initial_hand') else '证据不足'
                draws=' → '.join(d['actor'] for d in flow['observed_draws']) or '证据不足'
                selected='、'.join(s['name'] for s in flow.get('selected_team',{}).get('students',[])) if flow.get('selected_team') else '历史分析未指定'
                self.log.appendPlainText(f'用户指定队伍：{selected}\n暂停画面识别队伍：{roster}\n初始手牌：{hand}\n观测补牌：{draws}\n详细证据见“带牌与轮转.md”和 card_flow.json；不确定项不会自动补齐。')
            except FileNotFoundError:pass
            except (OSError,ValueError,KeyError,TypeError) as error:
                self.log.appendPlainText(f'带牌结果摘要未读取：{error}')
        if self.pending_close:
            QTimer.singleShot(0,self.close)

    def analysis_error(self,error):
        self.persist_analysis_log(f'分析进程出错：{self.analysis_process.errorString()}')
        self.log.appendPlainText(f'分析进程出错：{self.analysis_process.errorString()}')
        if error == QProcess.ProcessError.FailedToStart:
            self.analysis_finished(1,None)

    def open_analysis(self):
        if self.analysis_output and self.analysis_output.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.analysis_output)))

    def closeEvent(self, event):
        if self._replay_busy or self.replay_panel.process.state()!=QProcess.ProcessState.NotRunning:
            event.ignore()
            self.pending_close=True
            self.replay_panel.stop()
            self.replay_panel.log.appendPlainText('正在停止复现，退出后关闭主界面。')
        elif self.analysis_process.state() != QProcess.ProcessState.NotRunning:
            event.ignore()
            self.pending_close = True
            if self.analysis_stage in ('catalog','ensure'):
                self.log.appendPlainText('正在停止素材准备后退出，已完成的下载会保留。')
                self.analysis_process.terminate()
                QTimer.singleShot(1500,lambda:self.analysis_process.kill() if self.analysis_process.state()!=QProcess.ProcessState.NotRunning else None)
            else:self.log.appendPlainText('正在等待离线分析结束后退出，已生成的结果将保留。')
        elif self.process.state() != QProcess.ProcessState.NotRunning:
            event.ignore()
            self.pending_close = True
            self.send_control('stop')
            self.log.appendPlainText('正在保存并退出，请等待视频封装完成。')
        else:
            if self.start_hotkey_watcher:self.start_hotkey_watcher.timer.stop()
            event.accept()


def launch(*, smoke_test=False):
    # Qt 自行管理界面 DPI；录制子进程独立初始化 Win32 DPI。
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle('Fusion')
    configure_font(app)
    window = RecorderWindow(hotkey_state_reader=(lambda _:False) if smoke_test else None)
    window.show()
    if smoke_test:
        QTimer.singleShot(400, window.close)
    return app.exec()
