# SPDX-License-Identifier: GPL-3.0-or-later
"""礼服日奈射击卡的可用状态；身份匹配不代表已结束冷却。"""
DRESS_HINA_SHOOTING_CARD='日奈（礼服）·射击卡'


def shooting_card_state(card_roi):
    # 避开左上费用、下方次数和白色边框，只看步枪卡面内部。
    return shooting_card_inner_state(card_roi[30:100,35:105])


def shooting_card_inner_state(inner):
    import cv2
    import numpy as np
    if inner.size==0:
        return dict(identity=DRESS_HINA_SHOOTING_CARD,valid=False,value='unknown',metrics={})
    hsv=cv2.cvtColor(inner,cv2.COLOR_BGR2HSV)
    saturation=float(np.median(hsv[:,:,1]))
    brightness=float(np.median(hsv[:,:,2]))
    colored=float(np.mean(hsv[:,:,1]>60))
    if saturation>=60 and colored>=.5 and brightness>=100:
        state='ready'
    elif saturation<=30 and colored<=.15:
        state='cooldown'
    else:
        state='unknown'
    return dict(identity=DRESS_HINA_SHOOTING_CARD,valid=state!='unknown',value=state,
                metrics=dict(median_saturation=saturation,median_brightness=brightness,color_fraction=colored))
