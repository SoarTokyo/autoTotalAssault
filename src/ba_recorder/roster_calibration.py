# SPDX-License-Identifier: GPL-3.0-or-later
"""从人工确认的暂停菜单截图建立独立队伍头像模板，不修改历史数据。"""
import json
from pathlib import Path
import shutil


def calibrate_roster(base,frame,layout,output):
    import cv2
    import numpy as np
    base,frame,layout,output=map(lambda p:Path(p).resolve(),(base,frame,layout,output))
    if output.exists() or output.is_relative_to(base.parent):
        raise ValueError('请使用原视觉配置之外的新输出目录，不能覆盖已有配置')
    if any((parent/'session.json').exists() for parent in (output,*output.parents)):
        raise ValueError('不能将校准结果写入原始录制')
    profile=json.loads(base.read_text(encoding='utf-8'))
    entries=json.loads(layout.read_text(encoding='utf-8'))['slots']
    if len(entries)!=6 or len({e['identity'] for e in entries})!=6:
        raise ValueError('需要六个不同角色的人工确认菜单位置')
    image=cv2.imdecode(np.fromfile(frame,dtype=np.uint8),cv2.IMREAD_COLOR)
    if image is None:raise ValueError('无法读取暂停菜单校准图')
    width,height=profile['reference_size']
    if abs(image.shape[1]/image.shape[0]-width/height)>.015:raise ValueError('校准图宽高比不匹配')
    image=cv2.resize(image,(width,height))
    for entry in entries:
        x,y,x2,y2=entry['roi']
        if not (0<=x<x2<=width and 0<=y<y2<=height):raise ValueError('菜单头像区域超出画面')
    shutil.copytree(base.parent,output)
    profile['roster']=[]
    profile['roster_rois']=[entry['roi'] for entry in entries]
    for i,entry in enumerate(entries):
        x,y,x2,y2=entry['roi']
        relative=f'roster-face-{i}.png'
        ok,encoded=cv2.imencode('.png',image[y:y2,x:x2])
        if not ok:raise RuntimeError('菜单头像模板编码失败')
        with (output/relative).open('xb') as handle:handle.write(encoded.tobytes())
        profile['roster'].append(dict(label=entry['identity'],image=relative))
    profile.setdefault('thresholds',{})['roster']=.9
    profile['roster_scope_note']='人工标注菜单头像校准；位置是菜单格子，不代表抽牌顺序。需另行验证其他界面布局、角色和录制。'
    path=output/'profile.json'
    path.write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding='utf-8')
    return path
