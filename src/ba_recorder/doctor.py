# SPDX-License-Identifier: GPL-3.0-or-later
"""读取开发环境状态，不启动全局监听，也不发送输入。"""
import importlib.metadata
import platform
import sys


def doctor():
    print(f'Python：{sys.version.split()[0]}\n解释器：{sys.executable}\n系统：{platform.platform()}')
    missing = []
    for package in ('mss', 'pynput', 'pywin32', 'jsonschema', 'numpy', 'opencv-python'):
        try:
            print(f'{package}：{importlib.metadata.version(package)}')
        except importlib.metadata.PackageNotFoundError:
            print(f'{package}：未安装')
            missing.append(package)
    try:
        print(f'PySide6（可选界面依赖）：{importlib.metadata.version("PySide6")}')
    except importlib.metadata.PackageNotFoundError:
        print('PySide6（可选界面依赖）：未安装，当前命令行流程不需要')
    try:
        print(f'PyAV（可选视频后端）：{importlib.metadata.version("av")}')
    except importlib.metadata.PackageNotFoundError:
        print('PyAV（可选视频后端）：未安装，视频将尝试 Windows H.264 / MPEG-4')
    try:
        print(f'Windows Capture（可选采集后端）：{importlib.metadata.version("windows-capture")}')
    except importlib.metadata.PackageNotFoundError:
        print('Windows Capture：未安装；界面默认使用 mss，无需安装此可选后端')
    return int(bool(missing) or sys.version_info < (3, 11) or sys.platform != 'win32')
