# SPDX-License-Identifier: GPL-3.0-or-later
"""录制只读加载与独立分析结果输出。"""
from collections import Counter
import json
import math
from pathlib import Path
import uuid

from .semantics import parse_actions
from .storage import write_json


def read_jsonl(path):
    records = []
    with path.open(encoding='utf-8') as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f'{path.name} 第 {number} 行不完整或格式错误；请先恢复到独立副本。') from error
    return records


def safe_media(session, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError('媒体引用必须是相对路径')
    root = session.resolve()
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError('媒体引用超出录制目录')
    return result


def load_session(session):
    manifest = json.loads((session / 'session.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') not in ('1.0','1.1'):
        raise ValueError('不支持该录制格式版本')
    events = read_jsonl(safe_media(session, manifest['events_path']))
    seen = set()
    for event in events:
        if (event.get('schema_version') != '1.0' or type(event.get('t_ns')) is not int or event['t_ns'] < 0
                or not isinstance(event.get('id'), str) or not event['id'].startswith('e')
                or not event['id'][1:].isdigit() or event['id'] in seen):
            raise ValueError('事件版本、ID 或时间戳无效')
        seen.add(event['id'])
    return manifest, events


def analyze(session, profile, observations_path=None, output=Path('analyses')):
    manifest, events = load_session(session)
    observations = read_jsonl(observations_path) if observations_path else []
    for record in observations:
        if record.get('schema_version') != '1.0' or type(record.get('t_ns')) is not int or record['t_ns'] < 0:
            raise ValueError('识别结果版本或时间戳无效')
        confidence = record.get('confidence')
        if confidence is not None and (type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            raise ValueError('识别置信度必须是 [0,1] 范围内有限数值或 null')
        if type(record.get('valid')) is not bool or not isinstance(record.get('field'), str) or not isinstance(record.get('id'), str):
            raise ValueError('识别结果缺少有效性、字段或 ID')
    destination = output.resolve()
    if destination.is_relative_to(session.resolve()):
        raise ValueError('分析输出不能位于原始录制目录内')
    actions = parse_actions(events, observations, profile)
    folder = destination / f"{manifest['session_id']}-{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / 'actions.json', {'schema_version': '1.0', 'recording_session_id': manifest['session_id'],
                                        'profile': vars(profile), 'actions': actions})
    counts = Counter(a['label'] for a in actions)
    lines = ['# 战斗操作分析', '', f"来源会话：{manifest['session_id']}", '',
             f"区域校准：{'已确认' if profile.geometry_confirmed else '未确认；鼠标语义不会凭空推断'}", '',
             '所有动作均需人工复核；目标坐标不等于目标角色身份。分析不会修改原始录制，也不会执行输入。', '']
    lines.extend(f'- {label}：{count} 项' for label, count in counts.items())
    lines += ['', '| 时间（秒） | 类型 | 输入方式 | 验证状态 | 说明 |', '|---|---|---|---|---|']
    states = {'verified': '状态变化已确认', 'supported': '有施放支持证据', 'ambiguous': '证据不确定',
              'unverified': '待画面验证', 'not_applicable': '无动作关联', 'no_change_observed': '未观察到变化'}
    for action in actions:
        reason = action['reason_text'].replace('|', '\\|').replace('\n', ' ')
        lines.append(f"| {action['t_ns'] / 1e9:.3f} | {action['label']} | {action['method']} | {states[action['verification']]} | {reason} |")
    with (folder / 'report.md').open('x', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')
    return folder, len(actions)
