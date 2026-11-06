# SPDX-License-Identifier: GPL-3.0-or-later
"""隔离进程中的 Windows Graphics Capture；主循环不等待原生采集调用。"""
from __future__ import annotations
from dataclasses import dataclass
import importlib.util
import multiprocessing
from multiprocessing.shared_memory import SharedMemory
import time



INSTALL_COMMAND='.\\.venv\\Scripts\\python.exe -m pip install "windows-capture==2.0.1"'


def require_wgc():
    if importlib.util.find_spec('windows_capture') is None:
        raise RuntimeError('未安装 Windows 窗口采集依赖。请在项目目录执行：'+INSTALL_COMMAND)


def client_image(image, client, bounds):
    """只接受有明确像素对应关系的客户区，不缩放或猜测标题栏高度。"""
    import numpy as np
    left,top,width,height=client
    if image.ndim!=3 or image.shape[2]!=4:return None
    if image.shape[:2]==(height,width):return np.ascontiguousarray(image)
    for x,y,right,bottom in bounds:
        if (bottom-y,right-x)!=image.shape[:2]:continue
        dx,dy=left-x,top-y
        if 0<=dx and 0<=dy and dx+width<=image.shape[1] and dy+height<=image.shape[0]:
            return np.ascontiguousarray(image[dy:dy+height,dx:dx+width])
    return None


def _capture_worker(hwnd,fps,shared_name,capacity,metadata,lock,stop,error,state):
    shared=None
    try:
        import numpy as np
        from .windows import initialize_dpi
        initialize_dpi()
        import ctypes
        from ctypes import wintypes
        import win32gui
        import win32process
        from windows_capture import WindowsCapture
        shared=SharedMemory(name=shared_name)
        dwm=ctypes.WinDLL('dwmapi')
        dwm.DwmGetWindowAttribute.argtypes=[wintypes.HWND,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD]
        dwm.DwmGetWindowAttribute.restype=ctypes.c_long
        last=0
        mismatches=0
        target_pid=win32process.GetWindowThreadProcessId(hwnd)[1]
        capture=WindowsCapture(window_hwnd=hwnd,cursor_capture=False,draw_border=False,
                               minimum_update_interval=max(1,round(1000/fps)))

        @capture.event
        def on_frame_arrived(frame,control):
            nonlocal last,mismatches
            if stop.is_set():
                control.stop()
                return
            now=time.perf_counter_ns()
            if now-last<int(.9e9/fps):return
            last=now
            foreground=win32gui.GetForegroundWindow()
            if foreground!=hwnd:
                if (not foreground or win32process.GetWindowThreadProcessId(foreground)[1]!=target_pid or
                    not (win32gui.IsChild(hwnd,foreground) or win32gui.GetAncestor(foreground,3)==hwnd)):
                    return
            left,top=win32gui.ClientToScreen(hwnd,(0,0))
            _,_,width,height=win32gui.GetClientRect(hwnd)
            bounds=[win32gui.GetWindowRect(hwnd)]
            rect=wintypes.RECT()
            if dwm.DwmGetWindowAttribute(hwnd,9,ctypes.byref(rect),ctypes.sizeof(rect))==0:
                bounds.insert(0,(rect.left,rect.top,rect.right,rect.bottom))
            image=client_image(frame.frame_buffer,(left,top,width,height),bounds)
            if image is None:
                mismatches+=1
                if mismatches>fps*2:raise RuntimeError('WGC 画面无法对齐游戏客户区，请检查窗口布局或重新开始录制')
                return
            mismatches=0
            size=image.nbytes
            if size>capacity:raise RuntimeError('采集画面超过共享缓冲容量，请缩小游戏窗口')
            if not lock.acquire(False):return
            try:
                target=np.ndarray((size,),dtype=np.uint8,buffer=shared.buf)
                target[:]=image.reshape(-1)
                del target
                # 所有进程使用同一 QPC 单调时钟；这里明确记录回调到达时间。
                metadata[1:7]=[now,time.perf_counter_ns(),width,height,left,top]
                metadata[0]+=1
            finally:lock.release()

        @capture.event
        def on_closed():
            if not stop.is_set():
                error.value='Windows 关闭了窗口采集会话'
                state.value=-1

        control=capture.start_free_threaded()
        state.value=1
        while not stop.wait(.03):
            if control.is_finished():
                control.wait()  # 传播原生线程或回调异常。
                raise RuntimeError('Windows 窗口采集线程已退出')
        control.stop()
    except BaseException as failure:
        error.value=f'{type(failure).__name__}: {failure}'[:2000]
        state.value=-1
    finally:
        if shared is not None:shared.close()


@dataclass
class WindowShot:
    image: np.ndarray
    capture_start_ns: int
    capture_end_ns: int


class WgcCapture:
    def __init__(self,window,clock,fps,*,worker=_capture_worker,check_dependency=True):
        if check_dependency:require_wgc()
        self.window,self.clock,self.fps=window,clock,fps
        self.worker=worker
        self.context=multiprocessing.get_context('spawn')
        self.process=None
        self.shared=None
        self.capacity=64*1024*1024
        self.sequence=0
        self.started_at=None
        self.target=None

    def __enter__(self):return self
    def __exit__(self,*args):self.close()

    def _start(self):
        self.close()
        self.shared=SharedMemory(create=True,size=self.capacity)
        self.metadata=self.context.Array('q',7,lock=False)
        self.lock=self.context.Lock()
        self.stop=self.context.Event()
        self.error=self.context.Array('u',2048,lock=False)
        self.state=self.context.Value('i',0,lock=False)
        self.target=self.window.hwnd
        self.sequence=0
        self.started_at=time.monotonic()
        self.process=self.context.Process(target=self.worker,args=(self.target,self.fps,self.shared.name,
            self.capacity,self.metadata,self.lock,self.stop,self.error,self.state),daemon=True,name='battle-wgc')
        try:self.process.start()
        except BaseException:
            self.process.close()
            self.process=None
            self.shared.close()
            self.shared.unlink()
            self.shared=None
            raise

    def grab(self,region):
        import numpy as np  # 录制主进程已在安装钩子之前预加载。
        if self.process is None or self.target!=self.window.hwnd:self._start()
        if self.state.value<0:raise RuntimeError('Windows 窗口采集失败：'+self.error.value)
        if not self.process.is_alive():raise RuntimeError('Windows 窗口采集子进程意外退出')
        if self.sequence==0 and time.monotonic()-self.started_at>10:
            raise RuntimeError('Windows 窗口采集10秒内没有返回首帧，请检查目标窗口及采集依赖')
        if not self.lock.acquire(False):return None
        try:
            sequence,start,end,width,height,left,top=self.metadata[:]
            if not sequence or sequence==self.sequence:return None
            self.sequence=sequence
            if (left,top,width,height)!=(region['left'],region['top'],region['width'],region['height']):return None
            image=np.ndarray((height,width,4),dtype=np.uint8,buffer=self.shared.buf).copy()
            return WindowShot(image,start-self.clock.origin_ns,end-self.clock.origin_ns)
        finally:self.lock.release()

    def close(self):
        if self.process is not None:
            self.stop.set()
            self.process.join(timeout=.5)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=.5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=.5)
            self.process.close()
            self.process=None
        if self.shared is not None:
            self.shared.close()
            self.shared.unlink()
            self.shared=None
