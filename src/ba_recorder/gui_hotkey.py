# SPDX-License-Identifier: GPL-3.0-or-later
"""界面热键边沿检测；只查询指定按键，不拦截游戏按键或保存键盘活动。"""
import ctypes
from PySide6.QtCore import QObject,QTimer,Signal
from .keybindings import virtual_key


class StartHotkeyWatcher(QObject):
    pressed=Signal()
    def __init__(self,key,parent=None,state_reader=None):
        super().__init__(parent)
        if state_reader is None:
            get_state=ctypes.WinDLL('user32').GetAsyncKeyState
            get_state.argtypes=[ctypes.c_int];get_state.restype=ctypes.c_short
            state_reader=lambda vk:bool(get_state(vk)&0x8000)
        self.read_state=state_reader;self.key=key;self.down=False
        self.timer=QTimer(self);self.timer.setInterval(15);self.timer.timeout.connect(self.poll)
        self.set_key(key);self.timer.start()

    def set_key(self,key):
        self.key=key;self.vk=virtual_key(key)
        self.down=self.read_state(self.vk)

    def poll(self):
        down=self.read_state(self.vk)
        pressed=down and not self.down
        self.down=down
        if pressed:self.pressed.emit()
