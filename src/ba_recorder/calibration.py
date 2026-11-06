# SPDX-License-Identifier: GPL-3.0-or-later
"""用户手动框选截图区域，输出独立配置；不猜测游戏 HUD 坐标。"""
from dataclasses import asdict
import json

from .analysis_profile import AnalysisProfile


def calibrate(frame, output, base=None):
    import cv2
    import numpy as np
    output = output.resolve()
    for parent in (output.parent, *output.parents):
        if (parent / 'session.json').exists():
            raise ValueError('校准配置不能写入原始录制目录')
    if output.exists():
        raise ValueError('输出配置已存在，请使用新文件名，避免覆盖已有校准')
    image = cv2.imdecode(np.fromfile(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('无法读取用于校准的截图')
    h, w = image.shape[:2]
    profile = AnalysisProfile.load(base)
    labels = {'slot_0': '左侧技能卡', 'slot_1': '中间技能卡', 'slot_2': '右侧技能卡',
              'target': '有效战场目标区（排除 HUD）', 'cancel': '技能取消/放回区',
              'menu': '菜单按钮', 'auto': 'AUTO 按钮', 'speed': '倍速按钮'}
    try:
        for name, label in labels.items():
            print(f'请框选【{label}】。Enter 确认，Esc 跳过；完成全部区域后保存。', flush=True)
            x, y, width, height = cv2.selectROI('BA Recorder - Calibration', image, showCrosshair=True, fromCenter=False)
            if width and height:
                profile.regions[name] = [x / w, y / h, (x + width) / w, (y + height) / h]
    finally:
        cv2.destroyAllWindows()
    if not all(name in profile.regions for name in ('slot_0', 'slot_1', 'slot_2', 'target')):
        raise ValueError('未完成三个卡槽及目标区校准，没有保存可用配置')
    profile.geometry_confirmed = True
    profile.__post_init__()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as handle:
        json.dump(asdict(profile), handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return output
