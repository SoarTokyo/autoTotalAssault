# SPDX-License-Identifier: GPL-3.0-or-later
"""回放轴的数据转换与执行前检查；不修改录制或分析结果。"""
import json
import math
from pathlib import Path
from uuid import uuid4
from .naming import recording_name

import jsonschema
from .card_state import DRESS_HINA_SHOOTING_CARD

SCHEMA = Path(__file__).resolve().parents[2] / 'schemas/combat_script.schema.json'


def export_analysis_script(analysis_path, session, keymap, output):
    """导出待核对的编辑轴；使用录制目录名，同名时保留已有人工修改。"""
    session=Path(session).resolve()
    output=Path(output).resolve()
    if output.is_relative_to(session):
        raise ValueError('技能轴不能保存到原始录制目录')
    name=recording_name(session.name)
    data=import_script(analysis_path,keymap)
    jsonschema.validate(data,json.loads(SCHEMA.read_text(encoding='utf-8')))
    output.mkdir(parents=True,exist_ok=True)
    number=1
    while True:
        suffix='' if number==1 else f'（分析{number}）'
        path=output/f'{name}{suffix}.json'
        try:
            with path.open('x',encoding='utf-8') as handle:
                json.dump(data,handle,ensure_ascii=False,indent=2)
                handle.write('\n')
            return path
        except FileExistsError:
            number+=1


def timer_ms(text):
    minutes, seconds = str(text).split(':')
    value = float(seconds)
    if not minutes.isdigit() or not math.isfinite(value) or not 0 <= value < 60:
        raise ValueError('时间必须是 分:秒，例如 03:49.900')
    return int(minutes) * 60000 + round(value * 1000)


def timer_text(value):
    if value is None:return ''
    return f'{value // 60000:02d}:{value % 60000 / 1000:06.3f}'


def import_script(path, keymap):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if 'algorithm' not in data:
        jsonschema.validate(data, json.loads(SCHEMA.read_text(encoding='utf-8')))
        return data
    result = dict(schema_version='1.0', script_id=str(uuid4()), revision=1,
                  recording_session_id=data['recording_session_id'],
                  reference_resolution={'width': 1280, 'height': 720}, keymap=keymap,
                  time_anchors=[], corrections=[], actions=[])
    if data.get('selected_team'):result['selected_team']=data['selected_team']
    for item in data['actions']:
        if item.get('kind')=='auto_toggle' or item.get('method')=='auto':
            observed=item.get('method')=='auto' and item.get('kind')!='auto_toggle'
            try:remaining=timer_ms(item.get('game_timer_near_release'))
            except (ValueError,TypeError):remaining=None
            action=dict(id=f"a{item['index']}",kind='observe_auto_skill' if observed else 'set_auto',
                ignored=observed or item.get('status')!='supported',review_status='needs_review',
                source_event_ids=item['source_event_ids'],source_frame_ids=[],historical_t_ns=item['input_up_ns'],
                trigger=dict(remaining_ms=remaining,time_tolerance_ms=500,conditions=[],timeout_ms=10000,on_failure='abort'),
                notes=(item.get('skill') or '')+'；'+('AUTO历史施放仅供查看，由开关AUTO让游戏自行选取目标，不发送手动选卡或目标输入。' if observed else item.get('reason_text','')))
            if observed:action['skill']=dict(slot=item['slot']-1,identity=item.get('card_identity') or item.get('actor'))
            else:action['auto']=item.get('auto_state')
            result['actions'].append(action)
            continue
        if item.get('kind')=='transition':
            result['actions'].append(dict(id=f"a{item['index']}",kind='skip_transition',ignored=True,
                review_status='needs_review',source_event_ids=item['source_event_ids'],source_frame_ids=[],
                historical_t_ns=item['input_up_ns'],trigger=dict(remaining_ms=timer_ms(item['game_timer_near_release'])+1000,
                time_tolerance_ms=500,conditions=[],timeout_ms=30000,on_failure='abort'),
                target=dict(kind='point',point=item['target_normalized'],identity=None),
                transition=dict(window_ms=15000),notes='跳过转场：检测窗口比录制最后可见倒计时提前1游戏秒；HUD消失后连点，倒计时恢复立即停止。请先核对确认按钮坐标。'))
            continue
        if item.get('status') != 'supported' and not item.get('actor'):
            continue
        actor = item.get('card_identity') or item['actor']
        if not item.get('card_identity') and item['classification'] == 'followup_shot':
            actor += '·射击卡'
        try:
            remaining=timer_ms(item.get('game_timer_near_release'))
        except (ValueError,TypeError):
            remaining=None
        notes=item['skill']
        sample=item.get('evidence',{}).get('timer_sample')
        if sample and sample.get('offset_ms'):
            notes+=f"；时间取自邻帧（相对操作 {sample['offset_ms']:+.3f}ms），严格卡点前请人工核对"
        if item.get('copy_state',{}).get('valid'):
            notes+='；录制时复制卡标记：'+('是' if item['copy_state']['value'] else '否')
        if item.get('classification')=='skill_cast':notes+='；图库确认卡面身份，特殊技能阶段未推断'
        if item.get('status')!='supported':notes+='；未确认施放，默认不启用：'+item.get('reason_text','请手动核对')
        if remaining is None:
            notes+='；倒计时未识别，请手动补填后再执行'
        result['actions'].append(dict(
            id=f"a{item['index']}", kind='cast_skill', ignored=item.get('status')!='supported', review_status='needs_review',
            source_event_ids=item['source_event_ids'], source_frame_ids=[], historical_t_ns=item['input_up_ns'],
            trigger=dict(remaining_ms=remaining, time_tolerance_ms=500,
                         conditions=[dict(field='ex_cost', op='gte', value=item.get('displayed_cost_near_target_down') or 0,
                                          max_age_ms=350, min_confidence=.75)],
                         timeout_ms=10000, on_failure='abort'),
            skill=dict(slot=item['slot']-1, identity=actor),
            target=dict(kind='point', point=item['target_normalized'], identity=None), notes=notes))
    return result


def validate_script(data):
    jsonschema.validate(data, json.loads(SCHEMA.read_text(encoding='utf-8')))
    actions = [a for a in data['actions'] if not a['ignored']]
    if not actions:
        raise ValueError('技能轴没有启用的动作')
    previous = float('inf')
    ids = set()
    previous_action=None
    for action in actions:
        if action['id'] in ids:
            raise ValueError('动作 ID 重复')
        ids.add(action['id'])
        if action['review_status'] != 'confirmed':
            raise ValueError('请先核对并保存技能轴，不能直接执行待核对的识别结果')
        if action['kind']=='observe_auto_skill':
            raise ValueError('AUTO 施放行仅供观察，不能作为手动技能执行；请使用开启/关闭 AUTO 动作')
        if action['kind'] not in ('cast_skill', 'wait', 'skip_transition', 'set_auto'):
            raise ValueError('本版不执行倍速或菜单切换')
        trigger = action['trigger']
        if trigger.get('strict_time',False):
            if action['kind']!='cast_skill' or trigger.get('after_previous',False):
                raise ValueError('“严格卡点”仅用于技能，且不能与“秒接上个”同时勾选')
        if trigger.get('after_previous',False):
            if action['kind']!='cast_skill' or previous_action is None or previous_action['kind']!='cast_skill':
                raise ValueError('“秒接上个”必须接在上一条启用的技能动作之后，不能用于第一条或接在等待动作后')
        if trigger['remaining_ms'] is None:
            raise ValueError(f'动作 {action["id"]} 缺少战斗倒计时，请补填时间或取消启用该行')
        if not trigger.get('after_previous',False) and trigger['remaining_ms'] > previous:
            raise ValueError('动作必须按战斗倒计时递减排列；当前不支持跨战斗重置')
        # 秒接的历史倒计时不约束后续正常动作的顺序。
        if not trigger.get('after_previous',False):
            previous = trigger['remaining_ms']
        previous_action=action
        for condition in trigger['conditions']:
            if condition['field'] != 'ex_cost' or condition['op'] not in ('eq','gte','lte'):
                raise ValueError('本版条件仅支持 EX 费用数值比较')
            if type(condition['value']) not in (int,float) or not math.isfinite(condition['value']):
                raise ValueError('费用条件必须是有限数值')
        if action['kind']=='skip_transition':
            point=action.get('target',{}).get('point')
            if not point or any(not math.isfinite(v) or not 0<=v<=1 for v in point):
                raise ValueError('转场必须有有效的归一化确认按钮坐标')
            if trigger['conditions']:raise ValueError('转场不使用费用条件')
        if action['kind']=='set_auto':
            if type(action.get('auto')) is not bool:raise ValueError('请明确选择 AUTO 开启或关闭状态')
            if trigger['conditions']:raise ValueError('AUTO 开关不使用费用条件')
        if action['kind'] == 'cast_skill':
            if action.get('drag_path'):
                raise ValueError('本版回放使用快捷键选卡加目标点击，不执行拖动路径；请先转换为点目标')
            slot = action['skill'].get('slot')
            point = action['target'].get('point')
            if slot not in (0,1,2) or not point or any(not math.isfinite(v) or not 0 <= v <= 1 for v in point):
                raise ValueError('技能必须有卡槽和有效的归一化目标坐标')
            if action['target'].get('identity'):
                raise ValueError('本版只能点击固定坐标，不支持自动追踪目标角色')
            validate_key(data['keymap'].get(f'select_slot_{slot}', ''))
    return actions


def validate_key(value):
    from .keybindings import virtual_key
    code=virtual_key(value)
    if code in (119,120):
        raise ValueError('选卡键不能与回放 F8/F9 热键冲突')
    return code


def timing_tolerance_ms(action):
    trigger=action['trigger']
    return trigger.get('strict_tolerance_ms',100) if trigger.get('strict_time',False) else trigger['time_tolerance_ms']


def strict_early_tolerance_ms(action):
    # 兼容旧轴中的40/100ms配置，但严格卡点不再允许任何提前容量。
    return 0


def readiness(action, observation, *, lead_ms=0, projected_remaining_ms=None):
    """单次新鲜观测的纯判断；未知状态一律不执行。"""
    timer = observation['timer']
    trigger = action['trigger']
    if not trigger.get('after_previous',False):
        if not timer['valid']:
            return 'unknown'
        remaining = timer_ms(timer['value'])
        if trigger.get('strict_time',False) and projected_remaining_ms is not None:
            remaining=projected_remaining_ms
        delta = remaining - trigger['remaining_ms']
        if delta < -timing_tolerance_ms(action)-1e-6:
            return 'missed'
        early_window=strict_early_tolerance_ms(action) if trigger.get('strict_time',False) else lead_ms
        if delta > early_window+(0 if trigger.get('strict_time',False) else 1e-6):
            return 'early'
    if observation['pause']['valid'] or not observation['auto']['valid'] or observation['auto']['value'] != 'off':
        return 'blocked'
    for condition in trigger['conditions']:
        cost = observation['cost']
        if not cost['valid'] or cost['score'] < condition['min_confidence']:
            return 'blocked'
        value, expected = cost['value'], condition['value']
        if not {'gte': value >= expected, 'lte': value <= expected, 'eq': value == expected}[condition['op']]:
            return 'blocked'
    if action['kind'] == 'cast_skill':
        identity = action['skill'].get('identity')
        portrait = observation['portraits'][action['skill']['slot']]
        if identity and (not portrait['valid'] or portrait['value'] != identity):
            return 'blocked'
    return 'ready'


def blocking_reason(action, observation):
    if not action['trigger'].get('after_previous',False) and not observation['timer']['valid']:
        return '战斗倒计时无法识别'
    if observation['pause']['valid']:
        return '检测到暂停菜单'
    if not observation['auto']['valid'] or observation['auto']['value'] != 'off':
        return 'AUTO 未确认关闭'
    for condition in action['trigger']['conditions']:
        cost=observation['cost']
        if not cost['valid'] or cost['score'] < condition['min_confidence']:
            return '费用识别无效或置信度不足'
        v,e=cost['value'],condition['value']
        if not {'gte':v>=e,'lte':v<=e,'eq':v==e}[condition['op']]:
            return f'费用条件不满足：当前 {v}，要求 {condition["op"]} {e}'
    if action['kind']=='cast_skill' and action['skill'].get('identity'):
        portrait=observation['portraits'][action['skill']['slot']]
        if not portrait['valid'] or portrait['value']!=action['skill']['identity']:
            return (f'卡槽 {action["skill"]["slot"]+1} 身份未确认：预期 {action["skill"]["identity"]}，'
                    f'当前 {portrait.get("value") or "未知"}，分数 {portrait.get("score",0):.2f}')
    return '前置条件已满足'


def card_availability_reason(action, observation):
    """释放前同时核对游戏卡费；等待时绝不发送选卡键。"""
    if action['kind']!='cast_skill':return None
    slot=action['skill']['slot']
    cards=observation.get('card_costs',[])
    if len(cards)<=slot or not cards[slot]['valid'] or cards[slot].get('score',0)<.85:
        return '卡费尚未可靠识别，等待冷却或卡面恢复'
    cost=observation['cost']
    if not cost['valid'] or cost.get('score',0)<.75:
        return '当前费用识别无效'
    if cost['value']<cards[slot]['value']:
        return f'费用不足以使用当前卡牌：当前 {cost["value"]}，技能卡费 {cards[slot]["value"]}'
    if action['skill'].get('identity')==DRESS_HINA_SHOOTING_CARD:
        states=observation.get('card_readiness',[])
        state=states[slot] if len(states)>slot else {}
        if state.get('identity')!=DRESS_HINA_SHOOTING_CARD or not state.get('valid'):
            return '礼服日奈射击卡可用状态未确认，等待卡面恢复'
        if state.get('value')!='ready':
            return '礼服日奈射击卡仍是灰卡，等待射击冷却结束'
    return None


def chain_wait_ms(action):
    # 老版本的 chain_timeout_ms 是抢接窗口，不再作为等待费用/冷却的上限。
    return action['trigger'].get('chain_wait_timeout_ms',max(10000,action['trigger']['timeout_ms']))


def strict_pickup_ready(action, observation):
    """仅严格动作允许提前提牌；不要求轴的额外费用已达成。"""
    if not action['trigger'].get('strict_time',False) or action['kind']!='cast_skill':return False
    timer=observation['timer']
    if not timer['valid']:return False
    delta=timer_ms(timer['value'])-action['trigger']['remaining_ms']
    if not 0<delta<=1000:return False
    probe=dict(action,trigger=dict(action['trigger'],strict_time=False,after_previous=True,conditions=[]))
    return readiness(probe,observation)=='ready' and card_availability_reason(action,observation) is None
