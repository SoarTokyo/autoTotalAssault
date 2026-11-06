# SPDX-License-Identifier: GPL-3.0-or-later
"""读取界面控制管道；Windows 用管道状态区分暂时无数据与断开。"""
import ctypes
import sys
import time


def windows_pipe_probe(raw):
    if sys.platform != 'win32':
        return None
    import msvcrt
    from ctypes import wintypes
    try:
        handle=msvcrt.get_osfhandle(raw.fileno())
    except (AttributeError, OSError, ValueError):
        return None  # 内存流和测试替身。
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetFileType.argtypes=[wintypes.HANDLE]
    kernel.GetFileType.restype=wintypes.DWORD
    if kernel.GetFileType(handle)!=3:
        return None
    peek=kernel.PeekNamedPipe
    peek.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,
                   ctypes.c_void_p,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
    peek.restype=wintypes.BOOL
    def available():
        count=wintypes.DWORD()
        if peek(handle,None,0,None,ctypes.byref(count),None):
            return count.value
        error=ctypes.get_last_error()
        if error in (109,232,233):
            return None  # Windows 已确认写入端断开。
        raise ctypes.WinError(error)
    return available


def control_lines(stream, *, probe_factory=windows_pipe_probe):
    raw=getattr(getattr(stream,'buffer',None),'raw',None)
    if raw is None:
        yield from stream
        return
    available=probe_factory(raw)
    pending=b''
    while True:
        count=available() if available else 4096
        if count is None:
            break
        if count==0:
            time.sleep(.01)
            continue
        try:
            chunk=raw.read(min(count,4096))
        except BlockingIOError:
            time.sleep(.01)
            continue
        if chunk is None or (chunk==b'' and available is not None):
            time.sleep(.01)
            continue
        if chunk==b'':
            break
        pending+=chunk
        while b'\n' in pending:
            line,pending=pending.split(b'\n',1)
            yield line.decode('utf-8')
    if pending:
        yield pending.decode('utf-8')
