# SPDX-License-Identifier: GPL-3.0-or-later
"""灰卡等待专用小区域采集，不运行 OCR 或身份模板匹配。"""
import math

from .card_state import shooting_card_inner_state


class ProbeFocusLost(RuntimeError):
    pass


def probe_shooting_card(capture_device, window, profile, slot):
    import cv2
    import numpy as np
    before=window.snapshot()
    if not before.focused:raise ProbeFocusLost('灰卡快速检测期间游戏失去焦点')
    ref_width,ref_height=profile['reference_size']
    x,y,_,_=profile['rois']['portrait']
    x+=round(profile['slot_stride']*slot)
    left=max(0,math.floor((x+35)*before.width/ref_width))
    top=max(0,math.floor((y+30)*before.height/ref_height))
    right=min(before.width,math.ceil((x+105)*before.width/ref_width))
    bottom=min(before.height,math.ceil((y+100)*before.height/ref_height))
    if right<=left or bottom<=top:return None
    shot=capture_device.grab(dict(left=before.left+left,top=before.top+top,width=right-left,height=bottom-top))
    after=window.snapshot()
    if not after.focused:raise ProbeFocusLost('灰卡快速截图期间游戏失去焦点')
    geometry=lambda s:(s.hwnd,s.pid,s.left,s.top,s.width,s.height)
    if geometry(before)!=geometry(after):return None
    inner=np.asarray(shot)[:,:,:3]
    if inner.shape[:2]!=(70,70):inner=cv2.resize(inner,(70,70),interpolation=cv2.INTER_AREA)
    return shooting_card_inner_state(inner)
