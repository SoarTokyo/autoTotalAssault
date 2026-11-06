# SPDX-License-Identifier: GPL-3.0-or-later
"""离线队伍与手牌证据；观察到的换牌不等于保证未来抽牌顺序。"""
from collections import Counter
import json


def actor_for(identity,profile):
    for spec in profile.get('skill_variants',[]):
        if spec.get('card_identity')==identity:return spec['actor']
    for entry in profile.get('portraits',[]):
        if entry['label']==identity and entry.get('actor'):return entry['actor']
    for entry in profile.get('banners',[]):
        if identity==entry['actor'] or (entry.get('phase')=='followup_shot' and identity==entry['actor']+'·射击卡'):
            return entry['actor']
    return identity


def hand(feature):
    portraits=feature.get('hand_cards',feature.get('portraits',[]))
    if feature.get('pause',{}).get('valid') or not feature.get('timer',{}).get('valid') or len(portraits)!=3:
        return None
    if not all(p.get('valid') and p.get('value') for p in portraits):return None
    if 'hand_cards' not in feature and not all(p.get('score',0)>=.85 for p in portraits):return None
    return tuple(p['value'] for p in portraits)


def stable_hand(frames,features,start,end,*,latest=False,active_budget_ns=None):
    previous=None
    result=None
    elapsed=0
    previous_ns=start
    previous_paused=False
    for frame,feature in zip(frames,features):
        if not start<=frame['t_ns']<end:continue
        delta=frame['t_ns']-previous_ns
        elapsed+=max(0,delta-1_000_000_000) if previous_paused else delta
        previous_ns=frame['t_ns']
        previous_paused=feature.get('pause',{}).get('valid',False)
        if active_budget_ns is not None and elapsed>active_budget_ns:break
        current=hand(feature)
        if current and previous and current==previous[0] and 0<frame['t_ns']-previous[1]['t_ns']<=500_000_000:
            result=dict(cards=list(current),frame_ids=[previous[1]['id'],frame['id']],t_ns=frame['t_ns'],
                        confidence=min(previous[2],min(p['score'] for p in feature.get('hand_cards',feature.get('portraits',[])))))
            if not latest:return result
        previous=(current,frame,min(p['score'] for p in feature.get('hand_cards',feature.get('portraits',[])))) if current else None
    return result


def analyze_card_flow(events,frames,features,rows,profile,*,semantic_actions=()):
    roster_slots=[]
    for position in range(6):
        candidates={}
        for frame,feature in zip(frames,features):
            roster=feature.get('roster',[])
            if not feature.get('pause',{}).get('valid') or len(roster)<=position:continue
            value=roster[position]
            if value.get('valid'):
                candidates.setdefault(value['value'],[]).append((frame['id'],value['score']))
        identity=next(iter(candidates)) if len(candidates)==1 else None
        evidence=candidates.get(identity,[])
        confirmed=len(evidence)>=2
        roster_slots.append(dict(position=position+1,identity=identity if confirmed else None,
                                 status='observed' if confirmed else 'conflict' if len(candidates)>1 else 'unknown',
                                 candidates=list(candidates),frame_ids=[v[0] for v in evidence[:2]],
                                 confidence=min(v[1] for v in evidence) if confirmed else None))
    start=next((e['t_ns'] for e in events if e['kind']=='recording_start'),0)
    first=min((r['input_down_ns'] for r in rows),default=2**63)
    initial=stable_hand(frames,features,start,first)
    transitions=[]
    draws=[]
    for index,row in enumerate(rows):
        end=min((r['input_down_ns'] for r in rows[index+1:]),default=2**63)
        end=min(end,min((e['t_ns'] for e in events if e['kind']=='recording_pause' and e['t_ns']>row['input_up_ns']),default=2**63))
        # 不跨人工暂停，也不沿用上次录制片段的手牌。
        segment_start=max((e['t_ns'] for e in events if e['kind']=='recording_resume' and e['t_ns']<=row['input_down_ns']),default=start)
        previous_up=rows[index-1]['input_up_ns'] if index else start
        before=stable_hand(frames,features,max(segment_start,previous_up,row['input_down_ns']-3_000_000_000),row['input_down_ns'],latest=True)
        after=stable_hand(frames,features,row['input_up_ns'],min(end,row['input_up_ns']+60_000_000_000),latest=True,active_budget_ns=3_000_000_000)
        status='unknown'
        new=[]
        selected_card=before['cards'][row['slot']-1] if before else None
        selected_matches=(actor_for(selected_card,profile)==row['actor']) if selected_card and row['actor'] else None
        if before and after and row['status']=='supported':
            old_actors=Counter(actor_for(v,profile) for v in before['cards'])
            new_actors=Counter(actor_for(v,profile) for v in after['cards'])
            added=list((new_actors-old_actors).elements())
            removed=list((old_actors-new_actors).elements())
            if len(added)==len(removed)==1 and removed[0]==row['actor']:
                status='observed_replacement'
                new=added
                draws.append(dict(actor=added[0],action_index=row['index'],frame_ids=after['frame_ids']))
            elif not added and not removed:
                status='same_actors_stage_change' if before['cards']!=after['cards'] else 'same_hand_or_redraw'
            else:status='ambiguous_change'
            if selected_matches is False:
                status='selection_conflict'
                if new:draws.pop()
                new=[]
        transitions.append(dict(action_index=row['index'],actor=row['actor'],phase=row['classification'],
                                variant_id=row.get('skill_variant_id'),selected_slot=row['slot'],
                                selected_card=selected_card,selection_matches_actor=selected_matches,
                                status=status,before=before,after=after,new_actors=new))
    known={s['identity'] for s in roster_slots if s['identity']}
    sequence=([actor_for(v,profile) for v in initial['cards']]+[d['actor'] for d in draws[:3]]) if initial else []
    complete=(len(sequence)==6 and len(set(sequence))==6 and len(known)==6 and set(sequence)==known)
    return dict(schema_version='1.0',roster=dict(status='complete' if len(known)==6 else 'partial' if known else 'unknown',slots=roster_slots),
                selected_team=profile.get('selected_team'),
                initial_hand=initial,observed_draws=draws,transitions=transitions,
                initial_and_first_three_draws=sequence if complete else None,
                selection_events=[{k:a.get(k) for k in ('kind','t_ns','slot','method','source_event_ids')}
                                  for a in semantic_actions if a['kind'] in ('skill_select','skill_cancel')],
                rotation_status='observed_and_roster_consistent' if complete else 'needs_review',
                note='队伍位置、初始手牌与观测抽牌顺序分别保存；不保证复现时相同，不按技能次数盲推下一张。')


def write_card_flow(folder,result):
    (folder/'card_flow.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 带牌与技能卡轮转','',result['note'],'','|队伍位置|角色|证据状态|','|---|---|---|']
    if result.get('selected_team'):
        lines[3:3]=['用户指定队伍：'+'、'.join(s['name'] for s in result['selected_team']['students'])+'。以下表格仅列画面识别结果，不以人工选择代替观测。','']
    states={'observed':'已观测','unknown':'未识别','conflict':'证据冲突'}
    for slot in result['roster']['slots']:
        lines.append(f"|{slot['position']}|{slot['identity'] or '未识别'}|{states[slot['status']]}|")
    lines+=['', '初始三张：'+(' → '.join(result['initial_hand']['cards']) if result['initial_hand'] else '未获得动作前的稳定画面'),
            '观测补入的新角色：'+(' → '.join(d['actor'] for d in result['observed_draws']) or '尚无足够证据'),'',
            '|动作|角色|阶段|卡槽|换牌证据|','|---|---|---|---|---|']
    labels={'unknown':'证据不足','observed_replacement':'观测到其他角色补入','same_actors_stage_change':'同角色阶段卡变化',
            'same_hand_or_redraw':'手牌未变或同角色重抽，不能判定抽新牌','ambiguous_change':'变化不唯一，待确认',
            'selection_conflict':'选中卡槽与技能角色证据冲突'}
    for row in result['transitions']:
        phase={'ex_activation':'EX启动','followup_shot':'追加射击'}.get(row['phase'],row['phase'] or '未识别')
        lines.append(f"|{row['action_index']}|{row['actor'] or '未识别'}|{phase}|{row['selected_slot']}|{labels[row['status']]}|")
    (folder/'带牌与轮转.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
