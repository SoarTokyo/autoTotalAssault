# SPDX-License-Identifier: GPL-3.0-or-later
"""由原始键鼠和截图生成带证据的操作表；运行时不读取人工答案。"""
import bisect
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import uuid

from .analysis_io import load_session, read_jsonl, safe_media
from .analysis_profile import AnalysisProfile
from .deterministic_vision import VisualReader
from .semantics import parse_actions
from .storage import write_json
from .media import FrameSource, analysis_frames
from .paths import portable_path
from .skill_variants import variant_spec, variant_cost_matches, variant_confirmed
from .card_flow import analyze_card_flow, write_card_flow
from .catalog_analysis import recognize_action_cards,catalog_cast_evidence


def nearest_index(times, time):
    i = bisect.bisect_left(times, time)
    return min((j for j in (i-1,i) if 0 <= j < len(times)), key=lambda j: abs(times[j]-time))


def scan_until(features, frames, start_ns, boundary_ns, active_budget_ns=2_500_000_000):
    """菜单暂停不消耗观察预算；缺帧/不明状态仍消耗，硬上限60秒。"""
    elapsed, previous = 0, start_ns
    previous_paused = False
    for i, frame in enumerate(frames):
        t = frame['t_ns']
        if t < start_ns:
            continue
        if t >= min(boundary_ns, start_ns+60_000_000_000):
            break
        delta = t-previous
        # 暂停观察超过1秒未刷新，不能继续假定暂停。
        elapsed += max(0, delta-1_000_000_000) if previous_paused else delta
        if elapsed > active_budget_ns:
            break
        paused = features[i]['pause']['valid']
        yield i
        previous, previous_paused = t, paused


def analyze_features(events, frames, features, reader):
    profile = AnalysisProfile(**reader.profile['action_profile'])
    # 使用本次记录中的映射由上层传入，不使用固定选卡事件序号或时间表。
    if 'runtime_keymap' in reader.profile:
        profile.keymap.update(reader.profile['runtime_keymap'])
        profile.__post_init__()
    actions = parse_actions(events, profile=profile)
    candidates = [a for a in actions if a['kind']=='skill_cast']
    times = [f['t_ns'] for f in frames]
    by_id = {e['id']:e for e in events}
    recording_start = next(e['t_ns'] for e in events if e['kind']=='recording_start')
    rows=[]
    for number, action in enumerate(candidates):
        source = [by_id[ident] for ident in action['source_event_ids']]
        down = next(e for e in reversed(source) if e['kind']=='mouse_down')
        up = next(e for e in reversed(source) if e['kind']=='mouse_up')
        selection = source[0]
        next_start = min((max(by_id[ident]['t_ns'] for ident in a['source_event_ids']
                             if by_id[ident]['kind']=='mouse_down') for a in candidates[number+1:]), default=times[-1]+1)
        next_mode_change=min((a['t_ns'] for a in actions if a['kind']=='auto_toggle' and a['t_ns']>up['t_ns']),default=times[-1]+1)
        next_pause=min((e['t_ns'] for e in events if e['kind']=='recording_pause' and e['t_ns']>up['t_ns']),default=times[-1]+1)
        previous_resume=max((e['t_ns'] for e in events if e['kind']=='recording_resume' and e['t_ns']<=selection['t_ns']),default=0)
        next_start=min(next_start,next_mode_change,next_pause)
        segment_indices=[i for i,t in enumerate(times) if previous_resume<=t<next_pause]
        if not segment_indices:continue
        def segment_nearest(t):
            return min(segment_indices,key=lambda i:abs(times[i]-t))
        # 40毫秒前取邻近帧，减少刚提交动作后的画面被当作“前值”的风险。
        before_i=segment_nearest(down['t_ns']-40_000_000)
        selected_i=segment_nearest(selection['t_ns']-120_000_000)
        release_i=segment_nearest(up['t_ns'])
        after_i=segment_nearest(up['t_ns']+250_000_000)
        pre, selected, after=features[before_i],features[selected_i],features[after_i]
        slot=action['slot']
        card_cost=pre['card_costs'][slot]
        if not card_cost['valid']:
            card_cost=selected['card_costs'][slot]
        before_cost=pre['cost']
        after_cost=after['cost']
        before_cost_i,after_cost_i=before_i,after_i
        if not before_cost['valid']:
            nearby=[i for i in segment_indices if max(previous_resume,down['t_ns']-350_000_000)<=times[i]<=up['t_ns']
                    and features[i]['cost']['valid'] and not features[i]['pause']['valid']]
            if nearby:
                before_cost_i=min(nearby,key=lambda i:abs(times[i]-(down['t_ns']-40_000_000)))
                before_cost=features[before_cost_i]['cost']
        if not after_cost['valid']:
            nearby=[i for i in segment_indices if up['t_ns']+50_000_000<=times[i]<min(next_start,up['t_ns']+750_000_000)
                    and features[i]['cost']['valid'] and not features[i]['pause']['valid']]
            if nearby:
                after_cost_i=min(nearby,key=lambda i:abs(times[i]-(up['t_ns']+250_000_000)))
                after_cost=features[after_cost_i]['cost']
        baseline_labels={features[i]['banner']['value'] for i in range(len(frames))
                         if max(previous_resume,selection['t_ns']-350_000_000) <= times[i] <= down['t_ns']
                         and features[i]['banner']['valid']}
        contexts={}
        for frame,feature in zip(frames,features):
            if not selection['t_ns']<=frame['t_ns']<=up['t_ns']:continue
            for tag,value in feature.get('special_contexts',{}).items():
                if value.get('valid') and value.get('score',0)>contexts.get(tag,{}).get('score',0):contexts[tag]=value
        context_pre=dict(pre,special_contexts=contexts)
        matches=[]
        # 长按瞄准可能持续数秒，取证预算从松开附近开始计算。
        for index in scan_until(features,frames,up['t_ns']-100_000_000,next_start):
            visual=features[index]
            banner=visual['banner']
            if not banner['valid'] or banner['value'] in baseline_labels:
                continue
            entry=banner['entry']
            spec=variant_spec(entry,reader.profile,dict(visual,special_contexts=contexts))
            if spec is not None and card_cost['valid'] and not variant_cost_matches(spec,card_cost):
                continue
            matches.append((banner['score'],index,entry))
        matches.sort(reverse=True,key=lambda x:x[0])
        best=matches[0] if matches else None
        # 两个不同技能都有强匹配时不猜测，保留待复核。
        distinct={entry['label'] for score,index,entry in matches if best and best[0]-score<.025}
        selected_entry=best[2] if best and len(distinct)==1 else None
        before_value=before_cost['value'] if before_cost['valid'] else None
        after_value=after_cost['value'] if after_cost['valid'] else None
        drop=before_value is not None and after_value is not None and after_value<before_value
        manual=pre['auto']['valid'] and pre['auto']['value']=='off'
        catalog=catalog_cast_evidence(frames,features,slot,selection['t_ns'],up['t_ns'],next_start,previous_resume)
        status='needs_review'
        reason='未获得唯一的新技能名称条或有效状态证据。'
        spec=variant_spec(selected_entry,reader.profile,dict(after,special_contexts=contexts)) if selected_entry else None
        if spec and variant_confirmed(spec,card_cost,context_pre,after,drop,catalog=catalog):
            status,reason='supported','对应阶段的新技能名称条、卡费规则、手动状态及所需视觉条件共同支持施放。'
        elif selected_entry:
            reason='已识别技能名称条，但阶段配置、费用或独立阶段信号不足，不能确认该变体。'
        if pre['pause']['valid']:
            status,reason='needs_review','目标输入发生在已观察的暂停菜单中，不直接判定有效施放。'
        catalog_identity=catalog['identity']
        if spec and spec.get('requires_selected_actor'):
            from .analysis_team import normalize
            if not catalog_identity or normalize(catalog_identity)!=normalize(spec['actor']):
                status,reason='needs_review','已观察到特殊技能名称条，但选中卡牌的角色身份未得到对应证据。'
            elif status=='supported':
                reason='选中角色与新的特殊技能名称条一致，满足本地阶段规则；该阶段不强制费用下降或换牌。'
        generic_catalog=False
        if catalog_identity and selected_entry is None:
            generic_catalog=True
            reason='图库识别到选中的学生，但费用下降、卡牌轮转或技能阶段证据不足，保留待核对。'
            if not drop and before_cost['valid']:
                nearby=[i for i in scan_until(features,frames,up['t_ns']+50_000_000,next_start)
                        if not features[i]['pause']['valid'] and features[i]['cost']['valid']
                        and features[i]['cost']['value']<before_cost['value']]
                if nearby:
                    after_cost_i=nearby[0];after_cost=features[after_cost_i]['cost'];after_value=after_cost['value'];drop=True
            if (catalog.get('identity_stable') and card_cost['valid'] and card_cost['value']>0 and drop and manual and
                    catalog['rotation'] and not pre['pause']['valid']):
                status,reason='supported','两帧图库身份一致、正卡费、费用下降、卡槽换牌及手动状态共同支持施放；不推断特殊阶段。'
        if catalog_identity and catalog.get('identity_stable') and selected_entry:
            normalized=lambda name:name.replace('（','(').replace('）',')').replace(' ','')
            if normalized(catalog_identity)!=normalized(selected_entry['actor']):
                status,reason='needs_review','图库卡面身份与技能横幅角色不一致，可能涉及复制或识别冲突，请核对。'
        row={'index':number+1,'kind':'skill_cast','status':status,'reason_text':reason,
             'review_status':'needs_review','executable':False,
             'skill':selected_entry['label'] if selected_entry else f'{catalog_identity}·技能施放' if generic_catalog else None,
             'actor':selected_entry['actor'] if selected_entry else catalog_identity,
             'classification':spec['phase'] if spec else 'skill_cast' if generic_catalog and status=='supported' else None,
             'skill_variant_id':spec['id'] if spec else None,
             'card_identity':spec.get('card_identity',spec['actor']) if spec else catalog_identity,
             'copy_state':catalog.get('copy_state',dict(value=None,valid=False,score=0.)),
             'shot':selected_entry.get('shot') if selected_entry else None,
             'slot':slot+1,'input_down_ns':down['t_ns'],'input_up_ns':up['t_ns'],
             'recording_relative_down_seconds':(down['t_ns']-recording_start)/1e9,
             'recording_relative_up_seconds':(up['t_ns']-recording_start)/1e9,
             'game_timer_near_release':features[release_i]['timer']['value'],
             'displayed_cost_near_target_down':before_value,'displayed_cost_after_sample':after_value,
             'displayed_card_cost':card_cost['value'] if card_cost['valid'] else None,
             'target_normalized':action['target']['point'],'target_identity':None,'method':action['method'],
             'source_event_ids':action['source_event_ids'],
             'evidence':{'before_frame':frames[before_i],'selection_frame':frames[selected_i],
                         'release_frame':frames[release_i],'after_frame':frames[after_i],
                         'before_cost_frame':frames[before_cost_i],'after_cost_frame':frames[after_cost_i],
                         'catalog':catalog,
                         'special_contexts':contexts,
                         'banner_frame':frames[best[1]] if best else None,
                         'banner_score':best[0] if best else None,
                         'cost_score':before_cost['score'],'card_cost_score':card_cost['score'],
                         'timer_score':features[release_i]['timer']['score'],
                         'manual_mode_score':pre['auto']['score']},
             'pause_observed_soon_after':any(features[i]['pause']['valid'] for i in range(
                 bisect.bisect_left(times,up['t_ns']),min(len(times),bisect.bisect_right(times,up['t_ns']+1_500_000_000))))}
        rows.append(row)
    from .battle_events import nearby_timer,visual_rows
    for row in rows:
        if row['game_timer_near_release'] is None:
            sample=nearby_timer(frames,features,row['input_up_ns'])
            recovered=getattr(reader,'timer_recovery',{}).get(row['input_up_ns'])
            if recovered and (sample is None or abs(recovered['offset_ms'])<abs(sample['offset_ms'])):
                sample=recovered
            if sample:
                row['game_timer_near_release']=sample['value']
                row['evidence']['timer_sample']=sample
                row['evidence']['timer_frame']=sample['frame']
    rows.extend(visual_rows(events,frames,features,rows))
    rows.sort(key=lambda row:row['input_up_ns'])
    for number,row in enumerate(rows,1):row['index']=number
    return rows,actions


def write_report(folder,session,rows):
    video_proofs={}
    required={f['id']:f for row in rows for f in row['evidence'].values()
              if isinstance(f,dict) and 'video_frame_index' in f}
    if required:
        import cv2
        (folder/'evidence').mkdir(exist_ok=True)
        with FrameSource(session) as source:
            for frame in sorted(required.values(),key=lambda f:f['t_ns']):
                image=source.read(frame)
                relative=f"evidence/{frame['id']}.jpg"
                ok,encoded=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,90])
                if not ok:
                    raise RuntimeError('视频证据图导出失败')
                with (folder/relative).open('xb') as handle:
                    handle.write(encoded.tobytes())
                video_proofs[frame['id']]=relative
    text=['# 脚本自动分析结果','',
          '此表由本地字形、图像模板和输入状态规则自动生成，运行时未读取人工逐次答案，未调用大模型。',
          '支持证据不等于目标命中或通关；时间来自邻近采样帧，费用为整数显示。未识别值为空，不用推测补齐。','',
          '|序号|操作/观测录制秒|附近游戏时间|技能/阶段|复制卡|卡槽|费用前→后|卡面费用|判定|',
          '|---:|---:|---|---|---|---:|---|---:|---|']
    html_rows=[]
    for row in rows:
        timer_label=row['game_timer_near_release']
        if row['evidence'].get('timer_sample') and row['evidence']['timer_sample']['offset_ms']:
            timer_label+=f"（邻帧 {row['evidence']['timer_sample']['offset_ms']:+.0f}ms）"
        cells=[row['index'],f"{row['recording_relative_up_seconds']:.3f}",timer_label,
               row['skill'] or '未识别',('是' if row['copy_state']['value'] else '否') if row.get('copy_state',{}).get('valid') else '待确认',row['slot'],f"{row['displayed_cost_near_target_down']}→{row['displayed_cost_after_sample']}",
               row['displayed_card_cost'],'有施放证据' if row['status']=='supported' else '待核对']
        if row['kind']=='auto_toggle':
            cells[4:8]=['—','—','—','—']
            cells[-1]='状态已确认' if row['status']=='supported' else '待核对'
        text.append('|'+ '|'.join('—' if c is None else str(c) for c in cells)+'|')
        proof=[]
        for field in ('before_frame','release_frame','after_frame','banner_frame','timer_frame'):
            frame=row['evidence'].get(field)
            if frame:
                import os
                relative=video_proofs.get(frame['id']) or Path(os.path.relpath(safe_media(session,frame['path']),folder)).as_posix()
                proof.append(f'<a href="{html.escape(relative)}"><img loading="lazy" src="{html.escape(relative)}" width="250"><br>{field}</a>')
        html_rows.append('<tr>'+''.join('<td>'+html.escape('—' if c is None else str(c))+'</td>' for c in cells)+'</tr>'
                         +'<tr><td colspan="9"><div class="proof">'+''.join(proof)+'</div></td></tr>')
    (folder/'自动分析表.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    page='<!doctype html><meta charset="utf-8"><title>脚本自动分析</title><style>body{font:15px "Microsoft YaHei",sans-serif;margin:24px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd;padding:9px}.proof{display:flex;gap:12px}img{max-width:100%;height:auto}</style><h1>脚本自动分析及证据</h1><p>纯本地规则，未调用大模型。时间/费用均保留对应截图，未知值不补写。</p><table>'+''.join(html_rows)+'</table>'
    (folder/'自动分析证据.html').write_text(page,encoding='utf-8')


def run(session,profile_path,output=Path('analyses'),progress=print,*,team_path=None):
    import cv2
    cv2.setNumThreads(1)
    team=None
    if team_path is not None:
        from .analysis_team import validate_team,available_students
        team=validate_team(json.loads(Path(team_path).read_text(encoding='utf-8')),available_students())
    manifest,events=load_session(session)
    all_frames=sorted(read_jsonl(safe_media(session,manifest['frames_path'])),key=lambda f:f['t_ns'])
    frames=analysis_frames(all_frames,events=events)
    if not frames:
        raise ValueError('该录制没有可分析的截图')
    reader=VisualReader(profile_path,team=team,allow_missing_special=True) if team else VisualReader(profile_path)
    if team:
        packs=reader.profile.get('special_skill_packs',[])
        progress('身份识别使用本次选择的六人图库，不加载旧队伍身份模板。',flush=True)
        progress('已加载特殊技能素材包：'+('、'.join(p['pack'] for p in packs) if packs else '无；缺少专用素材的阶段将保留待核对。'),flush=True)
        for warning in reader.profile.get('special_skill_warnings',[]):progress('提示：'+warning,flush=True)
    if reader.profile.get('hud_glyph_sources'):
        progress(f'已加载本地 HUD 补充字形：{len(reader.extra_glyphs)} 个，识别阈值保持不变。',flush=True)
    reader.profile['runtime_keymap']=manifest.get('config',{}).get('keymap',{})
    output=output.resolve()
    if output.is_relative_to(session.resolve()):
        raise ValueError('分析结果不能写入原始录制目录')
    folder=output/f"auto-{manifest['session_id']}-{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True,exist_ok=False)
    if team:write_json(folder/'analysis_team.json',team)
    features=[]
    with (folder/'frame_features.jsonl').open('x',encoding='utf-8') as handle, FrameSource(session) as source:
        for i,frame in enumerate(frames):
            image=reader.normalize_image(source.read(frame))
            record=reader.features(image,include_portraits=not bool(team),include_roster=not bool(team),include_stage_signals=True,include_hand=not bool(team))
            features.append(record)
            handle.write(json.dumps({'frame_id':frame['id'],'t_ns':frame['t_ns'],**record},ensure_ascii=False)+'\n')
            if i%50==0:
                progress(f'图像识别：{i+1}/{len(frames)}',flush=True)
    recognize_action_cards(session,events,frames,features,reader,folder,progress,team=team)
    from .timer_recovery import recover_action_timers
    reader.timer_recovery=recover_action_timers(session,events,frames,features,all_frames,reader,progress)
    write_json(folder/'timer_recovery.json',{'samples':list(reader.timer_recovery.values())})
    with (folder/'frame_features.jsonl').open('w',encoding='utf-8') as handle:
        for frame,record in zip(frames,features):
            handle.write(json.dumps({'frame_id':frame['id'],'t_ns':frame['t_ns'],**record},ensure_ascii=False)+'\n')
    rows,actions=analyze_features(events,frames,features,reader)
    from .auto_control import add_auto_controls
    rows=add_auto_controls(events,frames,features,rows,actions,reader.profile)
    summary={'schema_version':'1.0','algorithm':'local-hud-rules-v2','recording_session_id':manifest['session_id'],
             'profile':portable_path(profile_path),'profile_sha256':hashlib.sha256(profile_path.read_bytes()).hexdigest(),
             'scope_note':reader.profile['scope_note'],'counts':dict(Counter((r['classification'] if r['status']=='supported' else 'needs_review') for r in rows)),
             'candidates':len(rows),'actions':rows}
    if team:summary['selected_team']=team
    write_json(folder/'auto_actions.json',summary)
    write_json(folder/'input_actions.json',{'actions':actions})
    flow=analyze_card_flow(events,frames,features,[r for r in rows if r['kind']=='skill_cast'],reader.profile,semantic_actions=actions)
    flow['recording_session_id']=manifest['session_id']
    write_card_flow(folder,flow)
    roster_state={'complete':'完整','partial':'部分识别','unknown':'证据不足'}[flow['roster']['status']]
    progress('带牌分析完成：队伍 '+roster_state+'；观测换牌 '+str(len(flow['observed_draws']))+' 次。',flush=True)
    write_report(folder,session,rows)
    progress('脚本分析完成：'+json.dumps(summary['counts'],ensure_ascii=False),flush=True)
    return folder,summary
