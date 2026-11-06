# SPDX-License-Identifier: GPL-3.0-or-later
"""只读 Win32 窗口适配器，不激活窗口，也不模拟输入。"""
import ctypes
import sys
from .model import WindowState


def initialize_dpi():
    if sys.platform != "win32":
        raise RuntimeError("实时录制需要 Windows 11")
    # 在导入输入监听和截图组件之前调用；ERROR_ACCESS_DENIED 表示已经设置。
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
    if not user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
        error = ctypes.get_last_error()
        if error != 5:
            raise ctypes.WinError(error)


def list_windows():
    import win32gui
    import win32process
    result = []

    def visit(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        if title and win32gui.IsWindowVisible(hwnd):
            result.append({"hwnd": hwnd, "pid": win32process.GetWindowThreadProcessId(hwnd)[1], "title": title})
    win32gui.EnumWindows(visit, None)
    return result


class Window:
    def __init__(self, hwnd):
        import win32gui
        import win32process
        self.gui = win32gui
        self.process = win32process
        self.hwnd = hwnd
        if not self.gui.IsWindow(hwnd):
            raise ValueError("所选窗口不存在")
        self.pid = self.process.GetWindowThreadProcessId(hwnd)[1]
        self.title = self.gui.GetWindowText(hwnd)
        self.window_class=self.gui.GetClassName(hwnd)
        self.follow_game_window=False
        user32=ctypes.WinDLL('user32',use_last_error=True)
        self._foreground_window=user32.GetForegroundWindow
        self._foreground_window.argtypes=[]
        self._foreground_window.restype=ctypes.c_void_p
        self._input_state=None
        self._input_foreground=None

    def _follow_foreground_game(self,foreground):
        if not self.follow_game_window or not foreground or foreground==self.hwnd:return
        if self.title.strip().lower() not in ('blue archive','蔚蓝档案','蔚藍檔案'):return
        if (not self.gui.IsWindow(foreground) or not self.gui.IsWindowVisible(foreground) or
            self.gui.IsIconic(foreground) or self.gui.GetWindowText(foreground)!=self.title or
            self.gui.GetClassName(foreground)!=self.window_class):return
        _,_,width,height=self.gui.GetClientRect(foreground)
        if width<=0 or height<=0:return
        self.hwnd=foreground
        self.pid=self.process.GetWindowThreadProcessId(foreground)[1]

    def _is_foreground(self,foreground):
        if self.gui.IsIconic(self.hwnd):return False
        if foreground==self.hwnd:return True
        if not self.follow_game_window or not foreground:return False
        if self.process.GetWindowThreadProcessId(foreground)[1]!=self.pid:return False
        return bool(self.gui.IsChild(self.hwnd,foreground) or
                    self.gui.GetAncestor(foreground,3)==self.hwnd)

    def focus_diagnostics(self):
        foreground=self.gui.GetForegroundWindow()
        return {'foreground_hwnd':foreground,
                'foreground_pid':self.process.GetWindowThreadProcessId(foreground)[1] if foreground else None,
                'target_hwnd':self.hwnd,'target_pid':self.pid,'minimized':bool(self.gui.IsIconic(self.hwnd))}

    def input_snapshot(self):
        """输入钩子只取缓存几何并快速核对前台，不查询客户区或等待窗口锁。"""
        state=self._input_state
        if state is None:return None
        if not state.focused or self._foreground_window()!=self._input_foreground:
            return WindowState(state.hwnd,state.pid,state.left,state.top,state.width,state.height,False)
        return state

    def snapshot(self):
        # 不在跨线程锁内调用 Win32；低级输入回调使用 input_snapshot，不参与此查询。
        foreground=self.gui.GetForegroundWindow()
        self._follow_foreground_game(foreground)
        if not self.gui.IsWindow(self.hwnd) or self.process.GetWindowThreadProcessId(self.hwnd)[1] != self.pid:
            raise RuntimeError("所选窗口已关闭，或其所属进程已改变")
        left, top = self.gui.ClientToScreen(self.hwnd, (0, 0))
        _, _, width, height = self.gui.GetClientRect(self.hwnd)
        focused=self._is_foreground(foreground)
        state=WindowState(self.hwnd,self.pid,left,top,width,height,focused)
        self._input_foreground=foreground
        self._input_state=state
        return state
