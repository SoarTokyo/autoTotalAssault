# SPDX-License-Identifier: GPL-3.0-or-later
"""源码运行时的项目相对路径；绝对路径只在访问文件时解析。"""
from pathlib import Path

PROJECT_ROOT=Path(__file__).resolve().parents[2]


def resolve_project_path(value,root=PROJECT_ROOT):
    path=Path(value).expanduser()
    return (path if path.is_absolute() else root/path).resolve()


def portable_path(value,root=PROJECT_ROOT):
    path=Path(value).expanduser().resolve()
    try:return path.relative_to(root.resolve()).as_posix()
    except ValueError:return str(path)
