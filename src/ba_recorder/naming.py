# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows 录制目录名称检查；保留内部稳定文件名和会话 ID。"""
import re


def recording_name(value):
    if not value:return None
    if value!=value.strip() or value.endswith('.'):
        raise ValueError('录制文件名不能以空格开头或结尾，也不能以句点结尾')
    if len(value)>100:
        raise ValueError('录制文件名最多 100 个字符')
    if any(ord(c)<32 or c in '<>:"/\\|?*' for c in value) or value in ('.','..'):
        raise ValueError('录制文件名不能包含路径、控制字符或 <>:"/\\|?*')
    stem=value.split('.')[0].upper()
    if stem in ('CON','PRN','AUX','NUL','CONIN$','CONOUT$') or re.fullmatch(r'(COM|LPT)[1-9¹²³]',stem):
        raise ValueError('录制文件名不能使用 Windows 保留名称')
    return value
