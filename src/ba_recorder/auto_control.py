# SPDX-License-Identifier: GPL-3.0-or-later
"""AUTO 目标状态的视觉取证与幂等切换，不把自动施放转换成手动技能。"""
import time
from .battle_events import nearby_timer


def add_auto_controls(events,frames,features,rows,semantic_actions,profile):
    if not frames:return rows
    origin=next(e['t_ns'] for e in events if e['kind']=='recording_start')
    intents=[a for a in semantic_actions if a['kind']=='auto_toggle']
    used=set();controls=[];state=None;pending=None;previous=None
    w,h=profile['reference_size'];x,y,x2,y2=profile['rois']['auto']
    default_point=[(x+x2)/2/w,(y+y2)/2/h]
    def make(i,value,intent=None,initial=False):
        t=intent['t_ns'] if intent else frames[i]['t_ns']
        sample=nearby_timer(frames,features,t)
        known=value in ('on','off')
        label=('录制初始：' if initial else '')+('开启 AUTO' if value=='on' else '关闭 AUTO' if value=='off' else 'AUTO 切换待确认')
        frame=frames[i]
        return dict(index=0,kind='auto_toggle',status='supported' if known else 'needs_review',
            reason_text=('已用连续画面确认初始状态；这是状态初始化，不声称录到了开启/关闭输入。' if initial else
                         '连续画面确认 AUTO 目标状态；复现时先读状态，只在不一致时点击。' if known else
                         '录到了 AUTO 操作意图，但没有确认状态变化；请人工核对目标状态。'),
            review_status='needs_review',executable=False,skill=label,actor=None,card_identity=None,
            classification='auto_on' if value=='on' else 'auto_off' if value=='off' else 'auto_unknown',
            auto_state=(value=='on') if known else None,skill_variant_id=None,shot=None,slot=1,
            input_down_ns=t,input_up_ns=t,recording_relative_down_seconds=(t-origin)/1e9,
            recording_relative_up_seconds=(t-origin)/1e9,game_timer_near_release=sample['value'] if sample else None,
            displayed_cost_near_target_down=None,displayed_cost_after_sample=None,displayed_card_cost=None,
            target_normalized=features[i]['auto'].get('button_point',default_point),target_identity=None,
            method=intent['method'] if intent else 'initial_state' if initial else 'visual_state_change',
            source_event_ids=intent['source_event_ids'] if intent else [],copy_state=dict(value=None,valid=False,score=0),
            pause_observed_soon_after=False,evidence=dict(before_frame=frames[max(0,i-1)],release_frame=frame,
                after_frame=frames[min(len(frames)-1,i+1)],banner_frame=None,timer_sample=sample,
                timer_frame=sample['frame'] if sample else None,auto_initial_state=initial))
    for i,obs in enumerate(features):
        auto=obs['auto'];t=frames[i]['t_ns']
        if obs['pause']['valid'] or not auto['valid'] or auto.get('value') not in ('on','off'):
            pending=None;continue
        value=auto['value']
        if pending is None or pending[0]!=value or t-frames[pending[2]]['t_ns']>500_000_000:
            pending=(value,i,i);continue
        first=pending[1];pending=(value,first,i)
        if value==state:
            previous=i;continue
        candidates=[a for a in intents if a['id'] not in used and
                    -100_000_000<=frames[first]['t_ns']-a['t_ns']<=600_000_000]
        intent=min(candidates,key=lambda a:abs(a['t_ns']-frames[first]['t_ns'])) if candidates else None
        if intent:used.add(intent['id'])
        row=make(first,value,intent,initial=state is None and intent is None)
        # 长时间不可见后才发现变化，不能把发现时刻当成原始切换时刻。
        if state is not None and intent is None and previous is not None and frames[first]['t_ns']-frames[previous]['t_ns']>750_000_000:
            row.update(status='needs_review',reason_text='长时间缺少 AUTO 画面，已观察到新状态但切换时刻不明；请核对。')
        controls.append(row);state=value;previous=i
    for intent in intents:
        if intent['id'] not in used:
            i=min(range(len(frames)),key=lambda j:abs(frames[j]['t_ns']-intent['t_ns']))
            controls.append(make(i,None,intent))
    result=sorted([*rows,*controls],key=lambda r:r['input_up_ns'])
    for i,row in enumerate(result,1):row['index']=i
    return result


def replay_auto(action,capture,click,check,wait,emit,clock=time.monotonic):
    """一次最多点击一次；读不到状态或点击后未确认时停止，不盲目反复切换。"""
    from .replay_script import timer_ms
    target='on' if action['auto'] else 'off'
    trigger=action['trigger'];deadline=clock()+120
    near=False;sent=False;stable=None;votes=0
    emit('action_wait',action_id=action['id'],mode_text='设置 AUTO '+('开启' if action['auto'] else '关闭'))
    while clock()<deadline:
        check();obs=capture()
        if not sent:
            if not obs['timer']['valid'] or obs['pause']['valid']:
                stable=None;votes=0
                wait(.03);continue
            delta=timer_ms(obs['timer']['value'])-trigger['remaining_ms']
            if delta<=2000 and not near:
                near=True;deadline=min(deadline,clock()+trigger['timeout_ms']/1000)
            if delta < -trigger['time_tolerance_ms']:raise RuntimeError('错过 AUTO 切换时间，已停止复现')
            if delta>0:wait(.02);continue
        auto=obs['auto']
        value=auto.get('value') if auto.get('valid') and not obs['pause']['valid'] else None
        if value not in ('on','off'):
            stable=None;votes=0;wait(.03);continue
        votes=votes+1 if value==stable else 1;stable=value
        if votes<2:wait(.03);continue
        if value==target:
            emit('auto_confirmed',action_id=action['id'],auto=action['auto'],input_sent=sent,
                 observed_remaining_ms=timer_ms(obs['timer']['value']) if obs['timer']['valid'] else None)
            return
        if not sent:
            point=auto.get('button_point')
            if point is None:raise RuntimeError('未能从当前画面定位 AUTO 按钮，已停止，未发送输入')
            click(point);sent=True;votes=0;stable=None
            deadline=clock()+trigger['timeout_ms']/1000
            emit('auto_input_sent',action_id=action['id'],auto=action['auto'],point=point,
                 observed_remaining_ms=timer_ms(obs['timer']['value']) if obs['timer']['valid'] else None)
        wait(.03)
    raise RuntimeError('AUTO 状态识别或切换确认超时，已停止复现')
