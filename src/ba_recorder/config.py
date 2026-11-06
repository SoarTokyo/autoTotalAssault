# SPDX-License-Identifier: GPL-3.0-or-later
from dataclasses import asdict, dataclass, field, replace
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class Config:
    sample_hz: float = 5.0
    pre_seconds: float = 2.0
    post_seconds: float = 3.0
    max_buffer_frames: int = 100
    queue_size: int = 20000
    all_frames: bool = False
    start_key: str = "f8"
    stop_key: str = "f9"
    abort_key: str = "f7"
    allowed_keys: list[str] = field(default_factory=list)
    keymap: dict[str, str] = field(default_factory=dict)
    video_enabled: bool = False
    video_fps: int = 30
    video_codec: str = 'auto'
    video_crf: int = 18
    video_queue_size: int = 8
    save_event_png: bool = False
    capture_backend: str = 'mss'
    screenshot_format: str = 'png'
    jpeg_quality: int = 85

    def __post_init__(self):
        for name in ("sample_hz", "pre_seconds", "post_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} 必须是有限数值")
        if not 0.5 <= self.sample_hz <= 30 or not 0 <= self.pre_seconds <= 30 or not 0 <= self.post_seconds <= 30:
            raise ValueError("sample_hz 必须在 0.5～30 之间；操作前后截图时长必须在 0～30 秒之间")
        for name in ("max_buffer_frames", "queue_size"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须是正整数")
        if self.max_buffer_frames < math.ceil(self.sample_hz * self.pre_seconds) + 2:
            raise ValueError("max_buffer_frames 必须足以容纳操作前的截图，并额外预留两帧")
        keys = (self.start_key, self.stop_key, self.abort_key)
        if any(k not in {f"f{i}" for i in range(1, 25)} for k in keys) or len(set(keys)) != 3:
            raise ValueError("开始、停止和中止热键必须互不相同，且使用 f1～f24 名称")
        if type(self.all_frames) is not bool:
            raise ValueError("all_frames 必须是布尔值")
        if type(self.video_enabled) is not bool or type(self.save_event_png) is not bool:
            raise ValueError('视频开关及操作截图开关必须是布尔值')
        if type(self.video_fps) is not int or not 1 <= self.video_fps <= 60:
            raise ValueError('视频帧率必须为 1～60 的整数')
        if self.video_codec not in ('auto','libx264','h264_msmf','mp4v'):
            raise ValueError('不支持的视频编码器')
        if self.capture_backend not in ('wgc','mss'):
            raise ValueError('不支持的画面采集后端')
        if self.screenshot_format not in ('png','jpg') or type(self.jpeg_quality) is not int or not 40<=self.jpeg_quality<=100:
            raise ValueError('截图格式必须是 png/jpg，JPEG 质量必须为40～100的整数')
        if type(self.video_crf) is not int or not 12 <= self.video_crf <= 30:
            raise ValueError('video_crf 必须在 12～30 之间')
        if type(self.video_queue_size) is not int or not 1 <= self.video_queue_size <= 32:
            raise ValueError('视频队列必须容纳 1～32 帧')
        if not isinstance(self.allowed_keys, list) or any(not isinstance(k, str) for k in self.allowed_keys):
            raise ValueError("allowed_keys 必须是按键名称列表")
        if not isinstance(self.keymap, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in self.keymap.items()):
            raise ValueError("keymap 必须将动作名称映射为按键名称")

    def to_dict(self):
        return asdict(self)

    def without_game_exit_hotkey(self):
        """只迁移运行副本；读取历史录制配置不会改变原始映射。"""
        from .keybindings import canonical_key
        used={canonical_key(v) for v in self.keymap.values()}
        used.update((self.start_key,self.stop_key,self.abort_key))
        updates={}
        for field,preferred in [('start_key','f8'),('stop_key','f9'),('abort_key','f7')]:
            if getattr(self,field)!='f10':continue
            candidate=next((k for k in [preferred,'f7','f6','f5','f11','f12','f4','f3','f2','f1'] if k not in used),None)
            if candidate is None:raise ValueError('找不到可替代 F10 的录制热键，请调整键位配置')
            used.add(candidate);updates[field]=candidate
        return replace(self,**updates) if updates else self

    def for_recording(self,mode='video'):
        if mode=='screenshots':
            return replace(self,video_enabled=False,all_frames=True,save_event_png=False,
                           pre_seconds=0,post_seconds=0,capture_backend='mss')
        if mode!='video':raise ValueError('不支持的录制方式')
        return replace(self,video_enabled=True,all_frames=False,save_event_png=False,pre_seconds=0,post_seconds=0)

    @classmethod
    def load(cls, path: Path | None):
        return cls(**json.loads(path.read_text(encoding="utf-8"))) if path else cls()
