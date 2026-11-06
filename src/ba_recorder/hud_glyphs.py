# SPDX-License-Identifier: GPL-3.0-or-later
"""加载有来源及校验值的本地 HUD 字形补充，不改变识别阈值。"""
import hashlib
import json
from pathlib import Path
import cv2
import numpy as np
from .analysis_io import safe_media
from .paths import PROJECT_ROOT,portable_path


def load_glyphs(reader,root=None):
    from .deterministic_vision import glyphs
    root=Path(root or PROJECT_ROOT/'.local/hud-glyphs')
    result={};sources=[]
    for path in sorted(root.glob('*/manifest.json')):
        data=json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema_version')!='1.0':raise ValueError('本地 HUD 字形版本无效')
        if data['reference_size']!=reader.profile['reference_size']:continue
        for i,entry in enumerate(data['glyphs']):
            if entry['font'] not in ('timer','cost','card') or type(entry['digit']) is not int or not 0<=entry['digit']<=9:
                raise ValueError('本地 HUD 字形标注无效')
            raw=safe_media(path.parent,entry['image']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise ValueError('本地 HUD 字形图片校验失败')
            image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
            if image is None:raise ValueError('本地 HUD 字形图片无法解码')
            normalized=glyphs(image,entry['font'])
            if len(normalized)!=1:raise ValueError('HUD 字形素材必须恰好包含一个有效数字')
            result[f'{entry["font"]}_{entry["digit"]}_extra_{path.parent.name}_{i}']=normalized[0]
        sources.append(dict(manifest=portable_path(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    reader.profile['hud_glyph_sources']=sources
    return result
