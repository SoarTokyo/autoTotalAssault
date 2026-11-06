# SPDX-License-Identifier: GPL-3.0-or-later
"""视频施放时间补查：只读邻近真实帧，不插值、不改变原帧时间。"""
from .analysis_profile import AnalysisProfile
from .media import FrameSource
from .semantics import parse_actions


def recover_action_timers(session,events,frames,features,all_frames,reader,progress=print):
    if not frames or not any('video_frame_index' in f for f in all_frames):return {}
    profile=AnalysisProfile(**reader.profile['action_profile'])
    profile.keymap.update(reader.profile.get('runtime_keymap',{}));profile.__post_init__()
    by_id={e['id']:e for e in events};targets=[]
    for action in parse_actions(events,profile=profile):
        if action['kind']!='skill_cast':continue
        up=next(by_id[e]['t_ns'] for e in reversed(action['source_event_ids']) if by_id[e]['kind']=='mouse_up')
        nearest=min(range(len(frames)),key=lambda i:abs(frames[i]['t_ns']-up))
        if not features[nearest]['timer']['valid']:targets.append(up)
    if not targets:return {}
    progress(f'正在逐帧补查 {len(targets)} 次操作附近的倒计时（最多前后250ms，不插值）……',flush=True)
    paused=[f['t_ns'] for f,obs in zip(frames,features) if obs['pause']['valid']]
    paused.extend(e['t_ns'] for e in events if e['kind'] in ('recording_pause','recording_resume'))
    def allowed(target,frame):
        t=frame['t_ns']
        return abs(t-target)<=250_000_000 and not any(min(t,target)<=p<=max(t,target) for p in paused)
    scheduled=[f for f in all_frames if any(allowed(t,f) for t in targets)]
    observations=[]
    with FrameSource(session) as source:
        for frame in scheduled:
            if not frame.get('window',{}).get('focused',True):continue
            image=reader.normalize_image(source.read(frame))
            if reader.match(image,'pause')['valid']:continue
            timer=reader.number(image,'timer')
            if timer['valid']:observations.append((frame,timer))
    result={}
    for target in targets:
        choices=[(f,timer) for f,timer in observations if allowed(target,f)]
        if not choices:continue
        frame,timer=min(choices,key=lambda v:abs(v[0]['t_ns']-target))
        result[target]=dict(value=timer['value'],frame=frame,offset_ms=round((frame['t_ns']-target)/1e6,3),
                            score=timer['score'],method='video_neighbor_retry',recognition=timer)
    progress(f'逐帧补查完成：{len(result)}/{len(targets)} 次获得邻近真实读数。',flush=True)
    return result
