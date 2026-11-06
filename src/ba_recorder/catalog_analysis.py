# SPDX-License-Identifier: GPL-3.0-or-later
"""离线图库身份与轮转证据；不以角色图标推断复制技能或特殊技能阶段。"""
from collections import Counter
import hashlib
import time

from .analysis_profile import AnalysisProfile
from .catalog_cards import CatalogCardReader
from .media import FrameSource
from .paths import PROJECT_ROOT,portable_path
from .semantics import parse_actions
from .storage import write_json


def default_manifests():
    return sorted((PROJECT_ROOT/'.local/gamekee/students').glob('manifest-*.json'))


def recognize_action_cards(session,events,frames,features,reader,folder,progress=print,team=None):
    if not events and not team:return
    profile=AnalysisProfile(**reader.profile['action_profile'])
    profile.keymap.update(reader.profile.get('runtime_keymap',{}));profile.__post_init__()
    actions=[a for a in parse_actions(events,profile=profile) if a['kind']=='skill_cast']
    if not actions and not team:return
    if not frames:return
    paths=default_manifests()
    if not paths:
        if team is not None:raise ValueError('已选择分析队伍，但找不到本地学生图库，请先下载图片')
        progress('未找到已下载的学生图库；本次继续使用视觉配置中的模板。',flush=True)
        return
    import cv2
    cv2.setNumThreads(1)
    progress('正在加载本地学生图库特征索引……',flush=True)
    loading_start=time.perf_counter()
    catalog=getattr(reader,'catalog_reader',None) if team else None
    if catalog is None:catalog=CatalogCardReader(paths,progress=progress,
                              content_ids=[s['content_id'] for s in team['students']] if team else None)
    loading_seconds=time.perf_counter()-loading_start
    progress(f'已加载学生图库：{len(catalog.references)} 个角色图；正在识别操作前后卡牌。',flush=True)
    by_event={e['id']:e for e in events}
    times=[f['t_ns'] for f in frames]
    schedule={}
    from .deterministic import nearest_index,scan_until
    for number,action in enumerate(actions):
        source=[by_event[e] for e in action['source_event_ids']]
        down=next(e['t_ns'] for e in reversed(source) if e['kind']=='mouse_down')
        up=next(e['t_ns'] for e in reversed(source) if e['kind']=='mouse_up')
        selection=source[0]['t_ns']
        for t in (selection-300_000_000,selection-120_000_000,down-40_000_000,
                  up-100_000_000,up-250_000_000,up-400_000_000):
            index=nearest_index(times,t)
            schedule.setdefault(index,set()).add(action['slot'])
        next_down=min((by_event[e]['t_ns'] for a in actions[number+1:] for e in a['source_event_ids']
                       if by_event[e]['kind']=='mouse_down'),default=times[-1]+1)
        next_pause=min((e['t_ns'] for e in events if e['kind']=='recording_pause' and e['t_ns']>up),default=times[-1]+1)
        following=[i for i in scan_until(features,frames,up+50_000_000,min(next_down,next_pause))
                   if not features[i]['pause']['valid']]
        # 覆盖快速连招间隙与暂停恢复后的早期卡面，最多取 12 帧。
        for index in following[:12]:schedule.setdefault(index,set()).add(action['slot'])
    roster_schedule=set()
    # AUTO 没有手动事件，仍需采样其施放前的手牌，供卡槽与费用取证。
    for i,feature in enumerate(features):
        if feature['auto'].get('value')=='on':schedule.setdefault(i,set()).update(range(3))
    if team:
        # 每段暂停先取两张，避免把人工选定队伍当作已观察到的队伍。
        paused=False;count=0
        for i,feature in enumerate(features):
            if feature['pause']['valid']:
                if not paused:count=0
                if count<3:roster_schedule.add(i)
                count+=1
            else:count=0
            paused=feature['pause']['valid']
        # 初始手牌与每次提牌前必须有相邻两帧的完整三卡证据。
        first=min((by_event[a['source_event_ids'][0]]['t_ns'] for a in actions),default=frames[-1]['t_ns']+1)
        initial=[i for i,f in enumerate(frames) if f['t_ns']<first and not features[i]['pause']['valid']]
        for i in initial[:12]:schedule.setdefault(i,set()).update(range(3))
        for action in actions:
            start=by_event[action['source_event_ids'][0]]['t_ns']
            before=[i for i,f in enumerate(frames) if start-700_000_000<=f['t_ns']<start and not features[i]['pause']['valid']]
            for i in before[-3:]:schedule.setdefault(i,set()).update(range(3))
        for slots in schedule.values():slots.update(range(3))
    cache={}
    matching_start=time.perf_counter()
    scheduled_indices=sorted(set(schedule)|roster_schedule)
    with FrameSource(session) as source:
        for n,index in enumerate(scheduled_indices):
            image=reader.normalize_image(source.read(frames[index]))
            if index in roster_schedule:
                roster=[]
                for slot in range(len(reader.profile.get('roster_rois',[]))):
                    roster.append(reader.catalog_observation(reader.roster_crop(image,slot)))
                features[index]['roster']=roster
            if features[index]['pause']['valid']:continue
            slots=features[index].setdefault('catalog_cards',[dict(valid=False,value=None,score=0) for _ in range(3)])
            for slot in sorted(schedule[index]):
                if slots[slot].get('evidence_source')=='downloaded_catalog_sift':continue
                crop=reader.crop(image,'portrait',slot)
                key=hashlib.sha256(crop.tobytes()).digest()
                if team:
                    cache[key]=reader.catalog_observation(crop)
                elif key not in cache:
                    result=catalog.recognize(crop)
                    best=result['candidates'][0] if result['candidates'] else {}
                    cache[key]=dict(valid=result['valid'],value=result.get('name'),content_id=result['content_id'],
                                    score=best.get('inlier_ratio',0),inlier_gap=result.get('inlier_gap',0),
                                    inliers=best.get('inliers',0),reason=result['reason'],
                                    candidates=result['candidates'][:2],evidence_source='downloaded_catalog_sift')
                slots[slot]=dict(cache[key],copy_state=reader.skill_copy_state(image,slot))
            if team:features[index]['hand_cards']=[dict(v) for v in slots]
            if n%25==0:progress(f'图库识别：{n+1}/{len(scheduled_indices)} 个队伍与操作画面',flush=True)
    write_json(folder/'catalog_usage.json',dict(reference_count=len(catalog.references),
        selected_team=team,
        special_skill_packs=reader.profile.get('special_skill_packs',[]),
        hud_glyph_sources=reader.profile.get('hud_glyph_sources',[]),
        manifests=[dict(path=portable_path(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths],
        sampled_frames=len(scheduled_indices),unique_crops=len(cache),
        loading_seconds=loading_seconds,matching_seconds=time.perf_counter()-matching_start,
        note='仅读取本地下载图库；未联网，不创建猜测的形态或本地卡牌库。'))


def catalog_cast_evidence(frames,features,slot,selection_ns,up_ns,end_ns,start_ns=0):
    from .deterministic import scan_until
    post_indices=set(scan_until(features,frames,up_ns+50_000_000,end_ns))
    before=[];after=[];copy_before=[]
    for index,(frame,feature) in enumerate(zip(frames,features)):
        t=frame['t_ns']
        if not max(start_ns,selection_ns-350_000_000)<=t<end_ns:continue
        if t>up_ns and index not in post_indices:continue
        cards=feature.get('catalog_cards',[])
        if feature['pause']['valid'] or len(cards)<=slot:continue
        if t<=up_ns:
            copy_before.append(dict(cards[slot].get('copy_state',{}),t_ns=t,frame_id=frame['id']))
        if not cards[slot].get('valid'):continue
        observation=dict(cards[slot],frame_id=frame['id'],t_ns=t)
        if t<=up_ns:before.append(observation)
        elif t>=up_ns+50_000_000:after.append(observation)
    copy_state=dict(value=None,valid=False,score=0.,reason='copy_state_not_stable')
    copy_votes=[p for p in copy_before if p.get('valid') and p['t_ns']>=up_ns-350_000_000]
    if (copy_before and copy_before[-1].get('valid') and len(copy_votes)>=2
            and copy_votes[-1]['value']==copy_votes[-2]['value']):
        copy_state=dict(copy_votes[-1],score=min(copy_votes[-1]['score'],copy_votes[-2]['score']))
    if not before:return dict(identity=None,rotation=False,before=[],after=[],copy_state=copy_state)
    # 最新卡面必须至少在两张不同画面中得到同一身份；不把换牌前的旧身份当成当前卡。
    latest=before[-1]
    votes=[p for p in before if p['content_id']==latest['content_id']]
    stable=len(votes)>=2
    identity=latest['value'] if stable or (latest.get('inliers',0)>=20 and latest.get('inlier_gap',0)>=8) else None
    changed=[p for p in after if p['content_id']!=latest['content_id']]
    replacements=Counter(p['content_id'] for p in changed)
    rotation=bool(identity and replacements and max(replacements.values())>=2 and
                  not any(p['content_id']==latest['content_id'] for p in after[-2:]))
    return dict(identity=identity,identity_stable=stable,content_id=latest['content_id'] if identity else None,rotation=rotation,copy_state=copy_state,
                before=votes,after=after)
