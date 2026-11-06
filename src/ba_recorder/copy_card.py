# SPDX-License-Identifier: GPL-3.0-or-later
"""复制卡的发光青蓝边框检测；只用颜色与几何，不读取学生美术模板。"""
import cv2
import numpy as np

_Y,_X=np.mgrid[:170,:125]
_REGIONS=(
    (_Y>=17)&(_Y<=26)&(_X>=47)&(_X<=118),
    (_Y>=36)&(_Y<=123)&(abs(_X-(124-.20*(_Y-20)))<=4),
    (_Y>=130)&(_Y<=141)&(_X>=3)&(_X<=100)&((_X<33)|(_X>66)),
)


def copy_frame_state(crop):
    unknown=dict(value=None,valid=False,score=0.,reason='copy_frame_unclear',evidence_source='copy_frame_geometry_v1')
    if crop is None or crop.ndim!=3 or min(crop.shape[:2])<40:return unknown
    image=cv2.resize(crop,(125,170)).astype(np.int16)
    cyan=(image[:,:,0]>=205)&(image[:,:,1]>=175)&(image[:,:,0]-image[:,:,2]>=35)
    white=(image.min(2)>=160)&(image.max(2)-image.min(2)<=50)
    scores=[float(cyan[region].mean()) for region in _REGIONS]
    whites=[float(white[region].mean()) for region in _REGIONS]
    detail=dict(edge_scores=scores,white_edge_scores=whites)
    # 原卡头像和普通神秘属性卡也可含蓝色；必须在两个预期边框部位同时出现。
    if scores[2]>=.15 and max(scores[:2])>=.10 and max(scores)>=.20:
        return dict(value=True,valid=True,score=min(.99,.8+max(scores)*.18),
                    reason='luminous_copy_frame',evidence_source='copy_frame_geometry_v1',**detail)
    # 严重泛白或选卡过渡不作“非复制卡”判断。
    if whites[0]>=.80 and whites[1]>.85:return dict(unknown,reason='copy_frame_washed_out',**detail)
    if scores[0]<.18 and scores[1]<.08 and scores[2]<.12 and sum(v>=.15 for v in whites)>=2:
        return dict(value=False,valid=True,score=.90,reason='ordinary_thin_frame',
                    evidence_source='copy_frame_geometry_v1',**detail)
    return dict(unknown,**detail)
