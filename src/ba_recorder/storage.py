# SPDX-License-Identifier: GPL-3.0-or-later
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid
import threading
from .messages import reason_text
from .naming import recording_name


def write_json(path, value):
    temporary = path.with_suffix(".partial")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    temporary.replace(path)


class SessionStore:
    def __init__(self, root: Path, config, clock, window, *, session_name=None):
        self.closed = False
        self.event_write_lock=threading.Lock()
        session_id = str(uuid.uuid4())
        self.path = root / (recording_name(session_name) or session_id)
        self.path.mkdir(parents=True, exist_ok=False)
        self._owned_path=self.path.resolve()
        (self.path / "frames").mkdir()
        self.events = (self.path / "events.jsonl").open("x", encoding="utf-8", buffering=1)
        self.frames = (self.path / "frames.jsonl").open("x", encoding="utf-8", buffering=1)
        self.extra_frames = None
        self.manifest = {"schema_version": "1.1" if config.get('video_enabled') else "1.0", "session_id": session_id,
                         "created_utc": datetime.now(timezone.utc).isoformat(),
                         "clock": {"kind": "perf_counter_ns", "origin_ns": clock.origin_ns},
                         "window": window, "config": config, "status": "recording",
                         "events_path": "events.jsonl", "frames_path": "frames.jsonl"}
        write_json(self.path / "session.json", self.manifest)

    def video_frame(self, record):
        if self.closed:
            raise RuntimeError('录制已结束，不能修改原始记录')
        self.frames.write(json.dumps(record, ensure_ascii=False) + '\n')

    def restart(self,window):
        """仅清空当前未结束的会话；调用前必须停止所有输入和视频写入线程。"""
        if self.closed or self.manifest.get('status')!='recording':
            raise RuntimeError('只能重启当前尚未结束的录制')
        root=self.path.resolve()
        if root!=self._owned_path or self.path.is_symlink():
            raise RuntimeError('当前会话路径已改变，不能清空录制')
        current=json.loads((root/'session.json').read_text(encoding='utf-8'))
        if current.get('session_id')!=self.manifest['session_id'] or current.get('status')!='recording':
            raise RuntimeError('会话归属或状态改变，不能清空录制')
        # 先验证全部媒体路径，再执行删除；不触及其他会话或自定义文件。
        targets=[]
        for name in ('frames','video'):
            directory=root/name
            if not directory.exists():continue
            entries=[directory,*directory.rglob('*')]
            for entry in entries:
                if (getattr(entry.lstat(),'st_file_attributes',0)&0x400 or
                        entry.is_symlink() or not entry.resolve().is_relative_to(root)):
                    raise RuntimeError('录制媒体包含链接或越界路径，不能重启')
                targets.append(entry)
        for name in ('events.jsonl','frames.jsonl','screenshots.jsonl','capture-stall.log'):
            entry=root/name
            if entry.exists() and (entry.is_symlink() or not entry.resolve().is_relative_to(root)):
                raise RuntimeError('会话数据路径已改变，不能清空录制')
        self.events.close();self.frames.close()
        if self.extra_frames:self.extra_frames.close()
        for entry in sorted(targets,key=lambda p:len(p.parts),reverse=True):
            if entry.is_dir():entry.rmdir()
            else:entry.unlink()
        for name in ('screenshots.jsonl','capture-stall.log'):
            path=root/name
            if path.exists():path.unlink()
        (root/'frames').mkdir()
        self.events=(root/'events.jsonl').open('w',encoding='utf-8',buffering=1)
        self.frames=(root/'frames.jsonl').open('w',encoding='utf-8',buffering=1)
        self.extra_frames=None
        self.manifest['window']=window
        for field in ('video','recording_summary','screenshots_path','termination_reason','termination_reason_text','end_ns'):
            self.manifest.pop(field,None)
        write_json(root/'session.json',self.manifest)

    def extra_frame(self, record, png):
        if self.closed:
            raise RuntimeError('录制已结束，不能修改原始记录')
        if self.extra_frames is None:
            self.extra_frames = (self.path / 'screenshots.jsonl').open('x',encoding='utf-8',buffering=1)
        with (self.path / record['path']).open('xb') as handle:
            handle.write(png)
        self.extra_frames.write(json.dumps(record,ensure_ascii=False)+'\n')

    def event(self, record):
        payload=json.dumps(record,ensure_ascii=False)+'\n'
        with self.event_write_lock:
            if self.closed:
                raise RuntimeError("录制已结束，不能修改原始记录")
            self.events.write(payload)

    def frame(self, record, png):
        if self.closed:
            raise RuntimeError("录制已结束，不能修改原始记录")
        with (self.path / record["path"]).open("xb") as handle:
            handle.write(png)
        self.frames.write(json.dumps(record) + "\n")

    def finish(self, status, reason, end_ns):
        if self.closed:
            raise RuntimeError("录制会话已经结束")
        self.closed = True
        self.events.close()
        self.frames.close()
        if self.extra_frames:
            self.extra_frames.close()
            self.manifest['screenshots_path']='screenshots.jsonl'
        self.manifest.update(status=status, termination_reason=reason,
                             termination_reason_text=reason_text(reason), end_ns=end_ns)
        write_json(self.path / "session.json", self.manifest)


class FrameBuffer:
    """压缩截图的滚动缓冲区；同一帧只写入磁盘一次。"""
    def __init__(self, pre_ns, post_ns, max_frames, persist, all_frames=False):
        self.pre_ns, self.post_ns = pre_ns, post_ns
        self.max_frames, self.persist = max_frames, persist
        self.all_frames = all_frames
        self.frames = deque()
        self.saved = set()
        self.until_ns = -1

    def _save(self, record, png):
        if record["id"] not in self.saved:
            self.persist(record, png)
            self.saved.add(record["id"])

    def add(self, record, png):
        self.frames.append((record, png))
        cutoff = record["t_ns"] - self.pre_ns
        while self.frames and (self.frames[0][0]["t_ns"] < cutoff or len(self.frames) > self.max_frames):
            self.frames.popleft()
        if self.all_frames or record["t_ns"] <= self.until_ns:
            self._save(record, png)

    def operation(self, t_ns):
        self.until_ns = max(self.until_ns, t_ns + self.post_ns)
        for record, png in self.frames:
            if t_ns - self.pre_ns <= record["t_ns"] <= self.until_ns:
                self._save(record, png)
