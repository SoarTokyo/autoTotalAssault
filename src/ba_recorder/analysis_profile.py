# SPDX-License-Identifier: GPL-3.0-or-later
"""动作解析配置：快捷键可预置，几何位置必须独立校准。"""
from dataclasses import dataclass, field
import json
import math
from pathlib import Path


DEFAULT_KEYS = {"select_slot_0": "vk:49", "select_slot_1": "vk:50", "select_slot_2": "vk:51",
                "toggle_auto": "vk:65", "cycle_speed": "vk:83", "context_escape": "esc"}


@dataclass
class AnalysisProfile:
    keymap: dict = field(default_factory=lambda: DEFAULT_KEYS.copy())
    regions: dict = field(default_factory=dict)
    geometry_confirmed: bool = False
    drag_threshold: float = 0.015
    selection_timeout_ms: int = 10000
    evidence_window_ms: int = 1800
    max_observation_age_ms: int = 1000
    min_confidence: float = 0.8

    def __post_init__(self):
        if type(self.geometry_confirmed) is not bool:
            raise ValueError('geometry_confirmed 必须是布尔值')
        supported = set(DEFAULT_KEYS) | {'cancel_skill', 'menu_toggle'}
        if not isinstance(self.keymap, dict) or any(k not in supported or not isinstance(v, str) or not v for k, v in self.keymap.items()):
            raise ValueError('动作按键映射无效')
        if len(set(self.keymap.values())) != len(self.keymap):
            raise ValueError('同一按键不能映射多个动作；Esc 请使用上下文动作 context_escape')
        if not isinstance(self.regions, dict):
            raise ValueError('regions 必须是区域映射')
        for name, rect in self.regions.items():
            if name not in {'slot_0', 'slot_1', 'slot_2', 'target', 'cancel', 'menu', 'auto', 'speed'}:
                raise ValueError(f'不支持的区域名称：{name}')
            if not isinstance(rect, list) or len(rect) != 4 or any(type(n) not in (int, float) or not math.isfinite(n) for n in rect):
                raise ValueError(f'区域 {name} 必须包含四个有限数值')
            x1, y1, x2, y2 = rect
            if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
                raise ValueError(f'区域 {name} 必须是有效的归一化矩形')
        for name in ('drag_threshold', 'min_confidence'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f'{name} 必须在 (0,1] 范围内')
        for name in ('selection_timeout_ms', 'evidence_window_ms', 'max_observation_age_ms'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f'{name} 必须是正整数')

    def region(self, point):
        if not self.geometry_confirmed or point is None:
            return None
        # 控件优先，target 可覆盖整个战斗视口，但不能吞掉选卡/取消区域。
        for name in ('cancel', 'menu', 'auto', 'speed', 'slot_0', 'slot_1', 'slot_2', 'target'):
            if name in self.regions:
                x1, y1, x2, y2 = self.regions[name]
                if x1 <= point[0] < x2 and y1 <= point[1] < y2:
                    return name
        return None

    @classmethod
    def load(cls, path: Path | None):
        return cls(**json.loads(path.read_text(encoding='utf-8'))) if path else cls()
