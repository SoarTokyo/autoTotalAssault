# SPDX-License-Identifier: GPL-3.0-or-later
"""从实际截图裁出状态模板；模板及配置保存在独立目录。"""
import json
import uuid

from .storage import write_json


def add_template(frame, config_path, field, value):
    import cv2
    import numpy as np
    if field not in ('auto', 'speed', 'menu_open', 'selected_slot'):
        raise ValueError('不支持该模板字段')
    if ((field in ('auto', 'menu_open') and type(value) is not bool)
            or (field == 'speed' and (type(value) is not int or value not in (1, 2, 3)))
            or (field == 'selected_slot' and value is not None and (type(value) is not int or value not in (0, 1, 2)))):
        raise ValueError('状态值无效；开关用 true/false，倍速用 1/2/3，选卡用 0/1/2/null')
    config_path = config_path.resolve()
    for parent in (config_path.parent, *config_path.parents):
        if (parent / 'session.json').exists():
            raise ValueError('模板配置不能写入原始录制目录')
    config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {
        'schema_version': '1.0', 'calibration_confirmed': True, 'templates': []}
    if config.get('schema_version') != '1.0':
        raise ValueError('不支持该模板配置版本')
    image = cv2.imdecode(np.fromfile(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('无法读取截图')
    print(f'请框选能稳定表明 {field}={value} 的图标区域；Enter 确认，Esc 取消。', flush=True)
    try:
        x, y, width, height = cv2.selectROI('BA Recorder - Template', image, showCrosshair=True, fromCenter=False)
    finally:
        cv2.destroyAllWindows()
    if not width or not height:
        raise ValueError('已取消模板采集')
    crop = image[y:y+height, x:x+width]
    if crop.reshape(-1, 3).std(axis=0).max() < 1:
        raise ValueError('区域过于均匀，请框选有辨识度的状态图标')
    h, w = image.shape[:2]
    config_path.parent.mkdir(parents=True, exist_ok=True)
    folder = config_path.parent / 'templates'
    folder.mkdir(exist_ok=True)
    relative = f'templates/{uuid.uuid4().hex}.png'
    encoded, png = cv2.imencode('.png', crop)
    if not encoded:
        raise ValueError('模板编码失败')
    with (config_path.parent / relative).open('xb') as handle:
        handle.write(png.tobytes())
    config['templates'].append({'field': field, 'value': value, 'roi': [x/w, y/h, (x+width)/w, (y+height)/h],
                                'template': relative, 'threshold': .95})
    config['calibration_confirmed'] = True
    write_json(config_path, config)
    return config_path
