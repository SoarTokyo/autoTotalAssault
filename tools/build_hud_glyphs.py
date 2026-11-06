# SPDX-License-Identifier: GPL-3.0-or-later
"""按显式录制帧标注构建本地 HUD 字形补充素材。"""
import argparse
import hashlib
import json
from pathlib import Path
import cv2
from ba_recorder.analysis_io import load_session,read_jsonl,safe_media
from ba_recorder.media import FrameSource
from ba_recorder.storage import write_json

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--spec',type=Path,required=True)
parser.add_argument('--session',type=Path,required=True)
parser.add_argument('--output',type=Path,default=Path('.local/hud-glyphs'))
args=parser.parse_args()
data=json.loads(args.spec.read_text(encoding='utf-8'));manifest,_=load_session(args.session)
if data['source_session_id']!=manifest['session_id']:raise ValueError('字形标注与源录制不匹配')
root=(args.output/data['id']).resolve()
if not root.is_relative_to(args.output.resolve()) or root.is_relative_to(args.session.resolve()):raise ValueError('字形输出路径无效')
if root.exists():raise ValueError('字形素材包已存在，不覆盖已有素材')
frames={f['id']:f for f in read_jsonl(safe_media(args.session,manifest['frames_path']))}
assets=[]
with FrameSource(args.session) as source:
    for entry in sorted(data['glyphs'],key=lambda e:frames[e['source_frame']]['t_ns']):
        image=cv2.resize(source.read(frames[entry['source_frame']]),tuple(data['reference_size']))
        x,y,x2,y2=entry['source_roi']
        if not 0<=x<x2<=image.shape[1] or not 0<=y<y2<=image.shape[0]:raise ValueError('字形裁剪框无效')
        ok,encoded=cv2.imencode('.png',image[y:y2,x:x2])
        if not ok:raise ValueError('字形图片编码失败')
        assets.append((entry,encoded.tobytes()))
root.mkdir(parents=True)
data['glyphs']=[]
for i,(entry,raw) in enumerate(assets):
    name=f'glyph-{i}.png';(root/name).write_bytes(raw)
    data['glyphs'].append(dict(entry,image=name,sha256=hashlib.sha256(raw).hexdigest()))
write_json(root/'manifest.json',data)
print('已构建 HUD 字形素材包：',root)
