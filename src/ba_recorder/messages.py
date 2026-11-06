# SPDX-License-Identifier: GPL-3.0-or-later
"""面向用户的中文说明；稳定的机器状态码保持不变。"""


def reason_text(reason):
    return {
        "abort_hotkey": "按下中止热键",
        "stop_hotkey": "按下停止热键",
        "keyboard_interrupt": "用户按下 Ctrl+C",
        "window_changed_during_capture": "截图期间窗口位置、尺寸或焦点发生变化",
        "unknown": "原因未知",
        "abort_request": "用户请求中止录制",
        "controller_disconnected": "控制界面连接已断开",
        "cancel_before_start": "开始录制前已取消等待",
    }.get(reason, reason)
