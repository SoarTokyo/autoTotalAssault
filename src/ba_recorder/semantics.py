# SPDX-License-Identifier: GPL-3.0-or-later
"""离线语义解析。输入意图、视觉验证和人工复核状态分别保存。"""
import math

from .analysis_profile import AnalysisProfile


LABELS = {
    'skill_select': '选择技能', 'skill_cast': '尝试施放技能', 'skill_cancel': '取消/放回技能',
    'menu_toggle': '打开/关闭菜单', 'auto_toggle': '切换 AUTO', 'speed_cycle': '切换倍速',
    'unclassified': '未分类操作', 'pointer_motion': '无动作关联的鼠标移动',
}


class Evidence:
    def __init__(self, observations, profile):
        self.profile = profile
        self.records = sorted(observations, key=lambda r: r['t_ns'])

    def before(self, field, t_ns):
        candidates = [r for r in self.records if r['field'] == field and r['t_ns'] <= t_ns]
        if not candidates:
            return None
        record = candidates[-1]
        # 最近一条无效结果不能被更旧的有效值掩盖。
        if (not record.get('valid') or record.get('confidence') is None
                or record['confidence'] < self.profile.min_confidence
                or t_ns - record['t_ns'] > self.profile.max_observation_age_ms * 1_000_000):
            return None
        return record

    def active_deadline(self, t_ns, end_ns):
        """只扣除有视觉证据的暂停时间，不把现实看笔记时长当作战斗推进。"""
        remaining = self.profile.evidence_window_ms * 1_000_000
        cursor = t_ns
        initial = self.before('menu_open', t_ns)
        paused = bool(initial and initial['value'] is True)
        pause_until = initial['t_ns'] + self.profile.max_observation_age_ms * 1_000_000 if paused else t_ns
        limit = min(end_ns, t_ns + 60_000_000_000)
        for record in self.records:
            if record['field'] != 'menu_open' or not t_ns < record['t_ns'] < limit:
                continue
            active_start = max(cursor, pause_until) if paused else cursor
            elapsed = max(0, record['t_ns'] - active_start)
            if elapsed >= remaining:
                return active_start + remaining
            remaining -= elapsed
            cursor = record['t_ns']
            paused = bool(record.get('valid') and record.get('confidence') is not None
                          and record['confidence'] >= self.profile.min_confidence and record['value'] is True)
            pause_until = record['t_ns'] + self.profile.max_observation_age_ms * 1_000_000 if paused else cursor
        return min(limit, (max(cursor, pause_until) if paused else cursor) + remaining)

    def pair(self, field, t_ns, end_ns, *, pause_aware=False):
        before = self.before(field, t_ns)
        until = self.active_deadline(t_ns, end_ns) if pause_aware else min(t_ns + self.profile.evidence_window_ms * 1_000_000, end_ns)
        # 超出下一操作边界的画面不能用来验证前一个操作。
        candidates = [r for r in self.records if r['field'] == field and t_ns < r['t_ns'] < until]
        if pause_aware:
            candidates = [r for r in candidates if not ((menu := self.before('menu_open', r['t_ns']))
                                                        and menu['value'] is True)]
        after = candidates[-1] if candidates else None
        if after and (not after.get('valid') or after.get('confidence') is None
                      or after['confidence'] < self.profile.min_confidence):
            after = None
        return before, after


def parse_actions(events, observations=(), profile=None):
    profile = profile or AnalysisProfile()
    events = sorted(events, key=lambda r: (r['t_ns'], int(r['id'][1:])))
    evidence = Evidence(observations, profile)
    actions, selected, pressed, keys = [], None, {}, set()
    reverse_keys = {key: action for action, key in profile.keymap.items()}
    moves = []

    def emit(kind, sources, t_ns, method, reason, **extra):
        item = {'schema_version': '1.0', 'id': f'a{len(actions)}', 'kind': kind,
                'label': LABELS[kind], 't_ns': t_ns, 'method': method,
                'source_event_ids': list(dict.fromkeys(sources)), 'evidence_ids': [],
                'verification': 'not_applicable' if kind == 'pointer_motion' else 'unverified',
                'review_status': 'needs_review', 'executable': False, 'reason_text': reason, **extra}
        actions.append(item)
        return item

    def flush_moves():
        if moves:
            emit('pointer_motion', [e['id'] for e in moves], moves[0]['t_ns'], 'mouse',
                 '移动未关联到点击或拖拽，作为无用操作候选保留；不删除原始记录。')
            moves.clear()

    def cancel(event, reason, method='context', extra_ids=()):
        nonlocal selected
        if selected:
            emit('skill_cancel', selected['ids'] + list(extra_ids) + [event['id']], event['t_ns'], method,
                 reason, slot=selected['slot'])
            selected = None

    def select(event, slot, method, ids=None):
        nonlocal selected
        if selected:
            cancel(event, '重复或切换选卡；上一选择没有可靠施放证据。', method)
        sources = ids or [event['id']]
        emit('skill_select', sources, event['t_ns'], method, '仅确认选卡输入意图，不代表技能已经施放。', slot=slot)
        selected = {'slot': slot, 'ids': sources, 't_ns': event['t_ns']}

    def cast(event, method, ids, path):
        nonlocal selected
        emit('skill_cast', selected['ids'] + ids, event['t_ns'], method,
             '已记录施放手势和目标坐标，等待费用/卡牌轮转等视觉证据。',
             slot=selected['slot'], target={'point': event['data']['normalized'],
                                          'kind': 'unknown', 'identity': None}, drag_path=path)
        selected = None

    for event in events:
        kind, data, t_ns = event['kind'], event['data'], event['t_ns']
        if selected and t_ns - selected['t_ns'] > profile.selection_timeout_ms * 1_000_000:
            cancel(event, '选择等待超时，不能判定已经施放或已经放回。')
        menu_state = evidence.before('menu_open', t_ns)
        if selected and menu_state and menu_state['value'] is True:
            cancel(event, '画面显示菜单已打开，之前的选卡不再关联后续目标点击。')
        if kind in ('window_state', 'session_end', 'recording_stop', 'recording_pause', 'recording_resume'):
            if kind != 'window_state' or not event['window']['focused']:
                cancel(event, '失去焦点、暂停或结束录制，选卡过程不完整。')
                for down, path in pressed.values():
                    emit('unclassified', [down['id']] + [p['event_id'] for p in path], t_ns, 'mouse',
                         '鼠标按下后缺少可靠释放记录，保留供人工检查。')
                pressed.clear()
                keys.clear()
                flush_moves()
            continue
        if kind == 'key_up':
            keys.discard(data['key'])
            continue
        if kind == 'key_down':
            flush_moves()
            key = data['key']
            if key in keys:
                emit('unclassified', [event['id']], t_ns, 'keyboard', '按键保持产生的重复事件，不重复推断动作。')
                continue
            keys.add(key)
            action = reverse_keys.get(key)
            menu = evidence.before('menu_open', t_ns)
            if menu and menu['value'] is True and action not in ('context_escape', 'menu_toggle'):
                emit('unclassified', [event['id']], t_ns, 'keyboard', '画面显示菜单已打开，不按战斗选卡或状态快捷键解释。')
                continue
            if action and action.startswith('select_slot_'):
                select(event, int(action[-1]), 'keyboard_select')
            elif action in ('toggle_auto', 'cycle_speed', 'menu_toggle'):
                emit({'toggle_auto': 'auto_toggle', 'cycle_speed': 'speed_cycle', 'menu_toggle': 'menu_toggle'}[action],
                     [event['id']], t_ns, 'keyboard', '已记录快捷键意图，具体前后状态由视觉结果确认。')
            elif action in ('cancel_skill', 'context_escape') and selected:
                cancel(event, '选卡后请求取消；需结合画面区分取消选卡和打开菜单。', 'keyboard_cancel')
            elif action == 'context_escape':
                emit('menu_toggle', [event['id']], t_ns, 'keyboard_context',
                     'Esc 可能开关菜单或执行其他上下文操作，需画面确认。')
            else:
                emit('unclassified', [event['id']], t_ns, 'keyboard', '该按键没有配置战斗语义，保留为无用操作候选。')
        elif kind == 'mouse_move':
            if pressed:
                for down, path in pressed.values():
                    path.append({'event_id': event['id'], 'offset_ns': t_ns - down['t_ns'],
                                 'point': data['normalized']})
            else:
                moves.append(event)
        elif kind == 'mouse_down':
            flush_moves()
            button = data['button']
            if button in pressed:
                emit('unclassified', [pressed[button][0]['id']], t_ns, 'mouse', '重复按下且缺少释放，手势不完整。')
            pressed[button] = (event, [{'event_id': event['id'], 'offset_ns': 0, 'point': data['normalized']}])
        elif kind == 'mouse_up':
            button = data['button']
            gesture = pressed.pop(button, None)
            if not gesture:
                emit('unclassified', [event['id']], t_ns, 'mouse', '缺少对应鼠标按下记录。')
                continue
            down, path = gesture
            path.append({'event_id': event['id'], 'offset_ns': t_ns - down['t_ns'], 'point': data['normalized']})
            ids = [p['event_id'] for p in path]
            start_point = down['data']['normalized']
            dragged = max(math.dist(start_point, p['point']) for p in path) >= profile.drag_threshold
            start_region, end_region = profile.region(start_point), profile.region(data['normalized'])
            geometry = down['window'], event['window']
            if any(geometry[0][k] != geometry[1][k] for k in ('left', 'top', 'width', 'height', 'hwnd', 'pid')):
                cancel(event, '手势期间窗口几何改变，目标坐标需要重新检查。', extra_ids=ids)
                emit('unclassified', ids, t_ns, 'mouse', '手势期间窗口几何改变，不能可靠分类。')
            elif button != 'left':
                emit('unclassified', ids, t_ns, 'mouse', '非左键手势未预设取消含义，保留人工复核。')
            elif start_region and start_region.startswith('slot_'):
                slot = int(start_region[-1])
                select(down, slot, 'mouse_drag' if dragged else 'mouse_select', [down['id']])
                if dragged and end_region == 'target':
                    cast(event, 'drag_from_card', ids, path)
                elif dragged:
                    cancel(event, '卡牌拖出后放回卡槽或取消/非目标区域，记为假操作候选。', 'drag_return', ids)
            elif end_region in ('menu', 'auto', 'speed') and start_region == end_region and not dragged:
                if end_region == 'menu' and selected:
                    cancel(event, '菜单输入打断选卡过程，不能将恢复后的点击当作原技能施放。', 'mouse_context', ids)
                emit({'menu': 'menu_toggle', 'auto': 'auto_toggle', 'speed': 'speed_cycle'}[end_region],
                     ids, t_ns, 'mouse_click', '已记录控件点击意图，不能仅凭点击断定状态变化。')
            elif selected and end_region == 'cancel':
                cancel(event, '目标选择后点击/拖至取消区，记为取消候选。', 'mouse_cancel', ids)
            elif selected and start_region == 'target' and end_region == 'target':
                cast(event, 'select_then_drag' if dragged else 'select_then_click', ids, path if dragged else [])
            else:
                emit('unclassified', ids, t_ns, 'mouse', '未校准区域或未关联有效选卡，保留为误点击/无用操作候选。')
        elif kind == 'scroll':
            emit('unclassified', [event['id']], t_ns, 'mouse', '滚轮无默认战斗语义。')
    flush_moves()
    if selected and events:
        cancel(events[-1], '记录结束时仍存在未完成的选卡，不能推断成功。')
    for down, path in pressed.values():
        emit('unclassified', [p['event_id'] for p in path], down['t_ns'], 'mouse', '记录结束时鼠标手势尚未释放。')

    # 只用下一次可能改变战斗状态的操作之前的证据，不跨操作错误归因。
    effect_kinds = {'skill_cast', 'skill_select', 'skill_cancel', 'menu_toggle', 'auto_toggle', 'speed_cycle'}
    ordered = sorted(actions, key=lambda a: (a['t_ns'], int(a['id'][1:])))
    for index, action in enumerate(ordered):
        t_ns = action['t_ns']
        future = [a for a in ordered[index + 1:] if a['kind'] in effect_kinds and a['t_ns'] > t_ns]
        if action['kind'] == 'skill_cast':
            # 已确认的菜单开关不等于取消上一次施放，恢复后仍可验证；
            # 下一次选卡/施放/AUTO 等操作仍然截断取证，避免错误归因。
            def blocks_cast(candidate):
                if candidate['kind'] in ('menu_toggle', 'skill_cancel'):
                    before = evidence.before('menu_open', candidate['t_ns'])
                    next_input = next((a['t_ns'] for a in future if a['t_ns'] > candidate['t_ns']), candidate['t_ns'] + 1_800_000_001)
                    until = min(next_input, candidate['t_ns'] + 1_800_000_001)
                    if before and any(r['field'] == 'menu_open' and candidate['t_ns'] < r['t_ns'] < until
                                      and r.get('valid') and r.get('confidence') is not None
                                      and r['confidence'] >= profile.min_confidence and r['value'] != before['value']
                                      for r in evidence.records):
                        return False
                return True
            future = [a for a in future if blocks_cast(a)]
            fallback = t_ns + 60_000_000_000
        else:
            fallback = t_ns + profile.evidence_window_ms * 1_000_000 + 1
        boundary = next((a['t_ns'] for a in future), fallback)
        state_field = {'auto_toggle': 'auto', 'speed_cycle': 'speed', 'menu_toggle': 'menu_open'}.get(action['kind'])
        if state_field:
            before, after = evidence.pair(state_field, t_ns, boundary)
            action['state_before'] = before['value'] if before else None
            action['state_after'] = after['value'] if after else None
            action['evidence_ids'] = [r['id'] for r in (before, after) if r]
            if before and after and before['value'] != after['value']:
                action.update(verification='verified', reason_text='前后视觉状态变化已确认；具体值已记录。')
            elif before and after:
                action.update(verification='no_change_observed', reason_text='前后观察状态相同，未确认切换成功。')
        elif action['kind'] == 'skill_cancel':
            before, after = evidence.pair('menu_open', t_ns, boundary)
            if before and after and before['value'] != after['value']:
                action.update(kind='menu_toggle', label=LABELS['menu_toggle'], verification='verified',
                              state_before=before['value'], state_after=after['value'],
                              evidence_ids=[before['id'], after['id']],
                              reason_text='上下文输入实际改变了菜单状态，已按视觉证据改分类；未据此推断技能成功。')
        elif action['kind'] == 'skill_cast':
            cost_before, cost_after = evidence.pair('ex_cost', t_ns, boundary, pause_aware=True)
            card_before, card_after = evidence.pair('skill_cards', t_ns, boundary, pause_aware=True)
            action['evidence_ids'] = [r['id'] for r in (cost_before, cost_after, card_before, card_after) if r]
            slot = action['slot']
            cards_valid = (card_before and card_after and isinstance(card_before['value'], list)
                           and isinstance(card_after['value'], list) and len(card_before['value']) > slot
                           and len(card_after['value']) > slot)
            action['skill_identity'] = card_before['value'][slot] if cards_valid else None
            cost_drop = bool(cost_before and cost_after and type(cost_before['value']) in (int, float)
                             and type(cost_after['value']) in (int, float) and cost_after['value'] < cost_before['value'])
            rotation = bool(cards_valid and card_before['value'][slot] is not None
                            and card_after['value'][slot] is not None
                            and card_before['value'][slot] != card_after['value'][slot])
            auto = evidence.before('auto', t_ns)
            action['signals'] = {'cost_drop': cost_drop, 'card_rotation': rotation,
                                 'manual_mode_observed': bool(auto and auto['value'] is False)}
            if cost_drop and rotation and auto and auto['value'] is False:
                action.update(verification='supported', reason_text='费用下降、所选卡槽轮转且观察为手动模式，支持已施放；目标身份仍待确认。')
            elif cost_drop or rotation:
                action.update(verification='ambiguous', reason_text='发现费用/卡牌变化，但证据不足或 AUTO 状态不明，不能归因于本次输入。')
            action['target']['identity'] = None
        # 所有自动解析结果都需人工复核，不能直接变成回放授权。
    return ordered
