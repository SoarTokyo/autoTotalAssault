# SPDX-License-Identifier: GPL-3.0-or-later
"""补充没有手动施放输入的历史事件，保持原始记录不可变。"""
from .transition import hud_absent


def nearby_timer(frames,features,t_ns,limit_ns=250_000_000):
    choices=[i for i,f in enumerate(frames) if abs(f['t_ns']-t_ns)<=limit_ns
             and features[i]['timer']['valid'] and not features[i]['pause']['valid']
             and not any(features[j]['pause']['valid'] for j in range(len(frames))
                         if min(t_ns,f['t_ns'])<=frames[j]['t_ns']<=max(t_ns,f['t_ns']))]
    if not choices:return None
    i=min(choices,key=lambda i:abs(frames[i]['t_ns']-t_ns))
    return dict(value=features[i]['timer']['value'],frame=frames[i],
                offset_ms=round((frames[i]['t_ns']-t_ns)/1e6,3),score=features[i]['timer']['score'])


def visual_rows(events,frames,features,manual_rows):
    start=next(e['t_ns'] for e in events if e['kind']=='recording_start')
    result=[]
    def base(i,kind,actor,label):
        t=frames[i]['t_ns'];f=features[i];timer=nearby_timer(frames,features,t)
        return dict(index=0,kind=kind,status='supported',reason_text=label,review_status='needs_review',executable=False,
            skill=label,actor=actor,classification='auto_cast' if kind=='skill_cast' else 'transition',
            card_identity=actor,skill_variant_id=None,shot=None,slot=1,input_down_ns=t,input_up_ns=t,
            recording_relative_down_seconds=(t-start)/1e9,recording_relative_up_seconds=(t-start)/1e9,
            game_timer_near_release=timer['value'] if timer else None,
            displayed_cost_near_target_down=None,displayed_cost_after_sample=None,displayed_card_cost=None,
            target_normalized=[.5,.5],target_identity=None,method='auto' if kind=='skill_cast' else 'transition',
            source_event_ids=[],copy_state=dict(value=None,valid=False,score=0),pause_observed_soon_after=False,
            evidence=dict(before_frame=frames[max(0,i-1)],release_frame=frames[i],after_frame=frames[min(len(frames)-1,i+1)],
                          banner_frame=frames[i] if kind=='skill_cast' else None,timer_sample=timer))
    # 同一个名称条连续多帧只产生一条事件；不从 AUTO 开启本身推断技能。
    groups=[]
    for i,f in enumerate(features):
        b=f.get('auto_banner',{})
        recently_auto=any(features[j]['auto'].get('value')=='on' and features[j]['auto']['valid']
                          for j in range(max(0,i-5),i+1) if frames[i]['t_ns']-frames[j]['t_ns']<=700_000_000)
        if not (b.get('valid') and recently_auto and not f['pause']['valid']):continue
        if groups and groups[-1][-1][1]==b['value'] and frames[i]['t_ns']-frames[groups[-1][-1][0]]['t_ns']<700_000_000:
            groups[-1].append((i,b['value']))
        else:groups.append([(i,b['value'])])
    for group in groups:
        if len(group)<2:continue
        i,actor=group[0];t=frames[i]['t_ns']
        if any(abs(r['input_up_ns']-t)<800_000_000 for r in manual_rows):continue
        row=base(i,'skill_cast',actor,actor+'·AUTO施放')
        prior=[j for j in range(i) if 0<t-frames[j]['t_ns']<=1_000_000_000]
        slots=[]
        for j in reversed(prior):
            slots=[n+1 for n,c in enumerate(features[j].get('catalog_cards',[])) if c.get('valid') and c.get('value')==actor]
            if len(slots)==1:break
        row['slot']=slots[0] if len(slots)==1 else 1
        row['evidence']['auto_slot_observed']=len(slots)==1
        row['evidence']['auto_banner_first_seen_ns']=t
        row['reason_text']='AUTO开启与连续技能横幅支持施放；仅保留历史观察，由AUTO开关驱动游戏自行选择目标，不转为手动技能。'
        result.append(row)
    # 仅把“HUD消失→确认框→HUD恢复”作为候选，普通OCR失败不算转场。
    i=0
    while i<len(frames):
        if not hud_absent(features[i]):i+=1;continue
        first=i
        # 动画背景偶尔会被误读成一个卡费数字；恢复以倒计时为准，不能由单个卡费终止片段。
        while i<len(frames) and not features[i]['timer']['valid'] and not features[i]['pause']['valid']:i+=1
        last=i-1
        confirmations=[j for j in range(first,i) if features[j].get('transition_confirm',{}).get('valid')]
        if (not confirmations or first==0 or i==len(frames)
                or sum(hud_absent(features[j]) for j in range(first,i))<3
                or frames[last]['t_ns']-frames[first]['t_ns']<700_000_000
                or not features[i]['timer']['valid']):continue
        previous=next((j for j in range(first-1,max(-1,first-20),-1) if features[j]['timer']['valid']),None)
        if previous is None:continue
        row=base(first,'transition',None,'跳过转场动画')
        row['game_timer_near_release']=features[previous]['timer']['value']
        row['evidence'].update(before_frame=frames[previous],after_frame=frames[i],banner_frame=frames[confirmations[0]])
        clicks=[e for e in events if e['kind']=='mouse_down' and frames[first]['t_ns']<=e['t_ns']<=frames[i]['t_ns']]
        # 只接受确认按钮内部的点击，避免把动画首击位置当作确认位置。
        clicks=[e for e in clicks if e.get('data',{}).get('normalized') and
                .50<=e['data']['normalized'][0]<=.70 and .64<=e['data']['normalized'][1]<=.76]
        row['target_normalized']=[.60,.70]
        if clicks:
            row['target_normalized']=clicks[-1]['data']['normalized']
            row['source_event_ids']=[e['id'] for e in clicks]
        row['reason_text']='观测到HUD消失、中央确认框及倒计时恢复；请核对证据后启用转场。'
        row['evidence']['transition_end_ns']=frames[i]['t_ns']
        result.append(row)
    return result
