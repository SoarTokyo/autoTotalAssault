# SPDX-License-Identifier: GPL-3.0-or-later
"""倒计时背景抑制兜底；只增强失败帧，不降低字形识别门槛。"""
import cv2
import numpy as np


def enhanced_timer(gray,decode):
    if gray.size==0 or min(gray.shape)<9 or float(gray.std())<1:return None
    variants=[]
    for size in (9,15):
        top=cv2.morphologyEx(gray,cv2.MORPH_TOPHAT,cv2.getStructuringElement(cv2.MORPH_RECT,(size,size)))
        variants.append((f'tophat_{size}',cv2.normalize(top,None,0,255,cv2.NORM_MINMAX)))
    high=gray.astype(np.float32)-cv2.GaussianBlur(gray,(0,0),2).astype(np.float32)
    variants.append(('local_background',np.clip(high*5+100,0,255).astype(np.uint8)))
    votes=[]
    for name,image in variants:
        for threshold in (None,80,140,160,180):
            result=decode(image,'timer',threshold)
            if result['valid']:votes.append((name,result))
    if len({name for name,_ in votes})<2 or len({v['value'] for _,v in votes})!=1:return None
    # 同一处理的多个阈值不算多个独立处理；所有通过字形校验的读数必须一致。
    return dict(max((v for _,v in votes),key=lambda v:v['score']),
                method='timer_background_consensus',votes=len(votes),
                enhancements=sorted({name for name,_ in votes}))
