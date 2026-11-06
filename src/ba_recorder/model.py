# SPDX-License-Identifier: GPL-3.0-or-later
from dataclasses import asdict, dataclass
import time


class Clock:
    def __init__(self):
        self.origin_ns = time.perf_counter_ns()

    def now(self):
        return time.perf_counter_ns() - self.origin_ns


@dataclass(frozen=True)
class WindowState:
    hwnd: int
    pid: int
    left: int
    top: int
    width: int
    height: int
    focused: bool

    def to_dict(self):
        return asdict(self)

    def contains(self, x, y):
        return self.left <= x < self.left + self.width and self.top <= y < self.top + self.height

    def point(self, x, y):
        if self.width <= 0 or self.height <= 0:
            raise ValueError("窗口客户区尺寸无效")
        return {"desktop": [x, y], "client": [x - self.left, y - self.top],
                "normalized": [(x - self.left) / self.width, (y - self.top) / self.height]}


def in_scope(state: WindowState | None, point=None):
    return bool(state and state.focused and state.width > 0 and state.height > 0
                and (point is None or state.contains(*point)))
