# SPDX-License-Identifier: GPL-3.0-or-later
"""可校准的离线模板识别；不内置未经验证的游戏截图或状态。"""
import json
from pathlib import Path
import time
import uuid

from .analysis_io import read_jsonl, safe_media
from .media import FrameSource, analysis_frames


def recognize(session, templates_path, output=Path('analyses')):
    import cv2
    import numpy as np
    config = json.loads(templates_path.read_text(encoding='utf-8'))
    if config.get('schema_version') != '1.0' or not config.get('calibration_confirmed'):
        raise ValueError('模板配置尚未确认校准，不能用占位模板执行识别')
    templates = config.get('templates', [])
    if not templates:
        raise ValueError('模板列表为空；需要实际游戏截图裁出的模板')
    allowed = {'auto', 'speed', 'menu_open', 'selected_slot'}
    prepared = []
    for item in templates:
        if item['field'] not in allowed:
            raise ValueError('当前模板后端仅支持 AUTO、倍速、菜单和选卡状态')
        value = item['value']
        if ((item['field'] in {'auto', 'menu_open'} and type(value) is not bool)
                or (item['field'] == 'speed' and (type(value) is not int or value not in (1, 2, 3)))
                or (item['field'] == 'selected_slot' and value is not None and (type(value) is not int or value not in (0, 1, 2)))):
            raise ValueError('模板状态值类型无效')
        roi = item['roi']
        if len(roi) != 4 or any(type(n) not in (int, float) or not np.isfinite(n) for n in roi):
            raise ValueError('模板区域必须是四个有限数值')
        if not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
            raise ValueError('模板区域不是有效的归一化矩形')
        threshold = item.get('threshold', .95)
        if type(threshold) not in (int, float) or not np.isfinite(threshold) or not 0 < threshold <= 1:
            raise ValueError('模板匹配阈值必须在 (0,1] 范围内')
        image = cv2.imdecode(np.fromfile(safe_media(templates_path.parent, item['template']), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.reshape(-1, 3).std(axis=0).max() < 1:
            raise ValueError('模板不存在或过于均匀，无法可靠匹配')
        prepared.append((item, image))
    output_root = output.resolve()
    if output_root.is_relative_to(session.resolve()):
        raise ValueError('识别输出不能位于原始录制目录内')
    manifest = json.loads((session / 'session.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') not in ('1.0','1.1'):
        raise ValueError('不支持该录制格式版本')
    frames = analysis_frames(read_jsonl(safe_media(session, manifest['frames_path'])))
    folder = output_root / f'recognition-{uuid.uuid4().hex}'
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / 'observations.jsonl'
    count = 0
    with path.open('x', encoding='utf-8') as handle, FrameSource(session) as source:
        for frame in frames:
            started = time.perf_counter_ns()
            image = source.read(frame)
            scores = {}
            for item, template in prepared:
                h, w = image.shape[:2]
                x1, y1, x2, y2 = item['roi']
                crop = image[int(y1*h):int(y2*h), int(x1*w):int(x2*w)]
                # 不自动缩放模板掩盖分辨率差异；不匹配尺寸时输出无效。
                score = None
                if (crop.shape[0] >= template.shape[0] and crop.shape[1] >= template.shape[1]
                        and crop.reshape(-1, 3).std(axis=0).max() >= 1):
                    result = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)
                    finite = np.where(np.isfinite(result), result, -1)
                    score = float(finite.max())
                scores.setdefault(item['field'], []).append((score, item))
            for field, candidates in scores.items():
                matches = sorted([(score, item) for score, item in candidates if score is not None
                                  and score >= item.get('threshold', .95)], key=lambda pair: pair[0], reverse=True)
                best = matches[0] if matches else None
                contradictory = bool(best and any(item['value'] != best[1]['value'] and best[0] - score < .03
                                                  for score, item in matches[1:]))
                valid = best is not None and not contradictory
                record = {'schema_version': '1.0', 'id': f'o{count}', 't_ns': frame['t_ns'],
                          'completed_ns': None,
                          'frame_id': frame['id'], 'field': field, 'value': best[1]['value'] if valid else None,
                          'valid': valid, 'confidence': max(0, min(1, best[0])) if valid else None,
                          'algorithm': 'opencv-template-v1', 'raw_text': None,
                          'processing_mode': 'offline', 'processing_duration_ns': time.perf_counter_ns() - started}
                # 离线识别不属于录制时钟，不能虚构实时完成时间。
                handle.write(json.dumps(record, ensure_ascii=False) + '\n')
                count += 1
    return path, count
