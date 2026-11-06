# SPDX-License-Identifier: GPL-3.0-or-later
"""游戏画面驱动的顺序回放；不使用录制的历史停顿作为等待时间。"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from .replay_script import readiness, timer_ms, validate_key, validate_script, blocking_reason, card_availability_reason, chain_wait_ms, timing_tolerance_ms, strict_pickup_ready, strict_early_tolerance_ms
from .replay_timing import CardIdentityMemory, TimingTracker
from .card_state import DRESS_HINA_SHOOTING_CARD
from .fast_card import probe_shooting_card, ProbeFocusLost
from .paths import portable_path
from .replay_log import DISPLAY_EVENT_KINDS


class ReplayStopped(RuntimeError):
    pass


class ObservationFocusLost(ReplayStopped):
    """截图阶段失焦；仅尚未发送本次输入的等待流程可转为暂停。"""


class InputDriver:
    def __init__(self, window, stop, enabled):
        from pynput import keyboard, mouse
        self.keyboard, self.mouse_module = keyboard, mouse
        self.keys, self.mouse = keyboard.Controller(), mouse.Controller()
        self.window, self.stop, self.enabled = window, stop, enabled
        self.last_timing={}

    def guard(self):
        state = self.window.snapshot()
        if self.stop.is_set():
            raise ReplayStopped('已收到停止请求')
        if not state.focused or state.width <= 0 or state.height <= 0:
            raise ReplayStopped('输入过程中游戏失去焦点，已停止；请检查是否残留技能选择')
        return state

    def delay(self, seconds):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            self.guard()
            self.stop.wait(.005)

    def wait_until(self,deadline):
        while True:
            self.guard()
            remaining=deadline-time.monotonic()
            if remaining<=0:return
            self.stop.wait(min(.002,remaining))

    def cast(self, action, keymap):
        self.select(action,keymap)
        self.target(action)

    def select(self, action, keymap):
        self.guard()
        if not self.enabled:
            return
        self.last_timing={}
        from .keybindings import canonical_key
        binding=keymap[f"select_slot_{action['skill']['slot']}"]
        code=validate_key(binding)
        name=canonical_key(binding)
        key=(getattr(self.keyboard.Key,name) if not name.startswith('vk:') else self.keyboard.KeyCode.from_vk(code))
        try:
            self.guard()
            self.keys.press(key)
            self.last_timing['key_down_ns']=time.monotonic_ns()
            self.delay(.035)
        finally:
            self.keys.release(key)
            self.last_timing['key_up_ns']=time.monotonic_ns()
        self.delay(.06)

    def target(self,action):
        self.guard()
        if not self.enabled:return
        state = self.guard()
        x,y = action['target']['point']
        self.mouse.position = (state.left+min(state.width-1,round(x*state.width)),
                               state.top+min(state.height-1,round(y*state.height)))
        self.guard()
        try:
            self.mouse.press(self.mouse_module.Button.left)
            self.last_timing['target_press_ns']=time.monotonic_ns()
            self.delay(.035)
        finally:
            self.mouse.release(self.mouse_module.Button.left)
            self.last_timing['target_release_ns']=time.monotonic_ns()
        self.guard()


def run(script_path, profile_path, hwnd, output, *, dry_run=True, input_lead_ms=130, late_tolerance_ms=1500,
        predictive_timing=True):
    import cv2
    import mss
    import numpy as np
    from pynput import keyboard
    from .deterministic_vision import VisualReader
    from .windows import Window, initialize_dpi

    script = json.loads(script_path.read_text(encoding='utf-8'))
    if not 0 <= input_lead_ms <= 500:
        raise ValueError('输入提前量必须在 0～500 毫秒之间')
    actions = validate_script(script)
    if type(late_tolerance_ms) is not int or not 0 <= late_tolerance_ms <= 10000:
        raise ValueError('迟到容差必须在 0～10000 个游戏毫秒之间')
    # 只改变本次执行副本，不回写用户保存的轴。
    import copy
    script=copy.deepcopy(script)
    actions=[a for a in script['actions'] if not a['ignored']]
    for action in actions:
        action['trigger']['time_tolerance_ms']=(timing_tolerance_ms(action) if action['trigger'].get('strict_time',False)
            else max(action['trigger']['time_tolerance_ms'],late_tolerance_ms))
        if action['trigger'].get('strict_time',False):action['trigger']['strict_early_tolerance_ms']=0
    input_budget_ms=float(input_lead_ms)
    target_budget_ms=35.0
    reader = VisualReader(profile_path,team=script['selected_team']) if script.get('selected_team') else VisualReader(profile_path)
    reader.prepare_catalog({a.get('skill',{}).get('identity') for a in actions})
    initialize_dpi()
    window = Window(hwnd)
    stop, resume = threading.Event(), threading.Event()
    root = output / str(uuid4())
    root.mkdir(parents=True, exist_ok=False)
    (root/'script.json').write_text(json.dumps(script,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'visual_profile_source.json').write_text(json.dumps(
        {'path':portable_path(profile_path),'input_lead_ms':input_lead_ms,
         'late_tolerance_ms':late_tolerance_ms,'predictive_timing':predictive_timing}),encoding='utf-8')
    origin = time.monotonic_ns()
    log = (root/'execution.jsonl').open('x',encoding='utf-8')
    image_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='replay-evidence')
    image_jobs=[]

    pending_display=[]
    action_details={a['id']:dict(action_name=a.get('notes',''),planned_remaining_ms=a['trigger']['remaining_ms']) for a in actions}

    def emit(kind, **details):
        event=dict(kind=kind,t_ns=time.monotonic_ns()-origin,wall_time_ns=time.time_ns(),
                   **action_details.get(details.get('action_id'),{}))
        event.update(details)
        payload=json.dumps(event,ensure_ascii=False)
        log.write(payload+'\n')
        log.flush()
        if kind in DISPLAY_EVENT_KINDS:
            pending_display.append('@replay '+payload)
        # 卡点前的预测详情延迟到输入结束后输出，避免终端输出拖慢点击。
        if kind in ('input_sent','dry_run','visually_supported','termination','action_wait',
                    'strict_pickup_sent','strict_pickup_confirmed','strict_pickup_clock_calibrated',
                    'transition_armed','transition_finished','auto_input_sent','auto_confirmed'):
            if pending_display:
                print('\n'.join(pending_display),flush=True)
                pending_display.clear()

    def hotkey(key, *extra):
        if key == keyboard.Key.f9:
            stop.set()
        elif key == keyboard.Key.f8:
            resume.set()

    def controls():
        import sys
        for line in sys.stdin:
            try:
                command=json.loads(line).get('command')
                if command=='stop':
                    stop.set()
                    return  # 不再阻塞等下一行，释放 stdin 的缓冲读取锁。
                elif command=='resume':
                    resume.set()
            except ValueError:
                pass
    threading.Thread(target=controls,daemon=True).start()
    listener = keyboard.Listener(on_press=hotkey)

    def check():
        for job in list(image_jobs):
            if job.done():
                job.result()
                image_jobs.remove(job)
        if stop.is_set():
            raise ReplayStopped('用户停止回放')
        if not listener.is_alive():
            raise ReplayStopped('紧急停止监听已退出')

    def arm(message):
        resume.clear()
        print(message+'；切回游戏后按 F8 继续，F9 停止。',flush=True)
        emit('paused',reason=message)
        while True:
            check()
            if resume.is_set() and window.snapshot().focused:
                resume.clear()
                return
            stop.wait(.03)

    def capture(capture_device, *, focus_slot=None, inspect_selection=False, lightweight=False):
        state=window.snapshot()
        if not state.focused:
            raise ObservationFocusLost('观测过程中失去焦点')
        started=time.monotonic()
        image=np.asarray(capture_device.grab(dict(left=state.left,top=state.top,width=state.width,height=state.height)))[:,:,:3].copy()
        if not window.snapshot().focused:
            raise ObservationFocusLost('截图期间失去焦点')
        obs=reader.features(reader.normalize_image(image),include_portraits=not lightweight,include_readiness=not lightweight,
                            focus_slot=focus_slot,include_selection=inspect_selection)
        if not window.snapshot().focused:
            raise ObservationFocusLost('识别画面期间失去焦点')
        return image,obs,time.monotonic()-started

    def fast_watch(capture_device,action,deadline):
        started=time.monotonic()
        end=min(deadline,started+.1)  # 至少每100ms回到完整前置检查，不能只盯颜色盲跑。
        probes=0
        state=None
        while time.monotonic()<end:
            check()
            tick=time.monotonic()
            try:
                state=probe_shooting_card(capture_device,window,reader.profile,action['skill']['slot'])
            except ProbeFocusLost as error:
                raise ObservationFocusLost(str(error)) from error
            probes+=1
            if state is None or (state['valid'] and state['value']=='ready'):break
            stop.wait(max(0,1/120-(time.monotonic()-tick)))
        elapsed=time.monotonic()-started
        emit('cooldown_fast_watch',action_id=action['id'],probes=probes,elapsed_ms=round(elapsed*1000,3),
             observed_probe_hz=round(probes/elapsed,1) if elapsed and probes>=2 else None,
             result=state['value'] if state else ('geometry_changed' if probes else 'deadline'),target_probe_hz=120)

    def write_image(name,image):
        ok,encoded=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,94])
        if not ok:
            raise RuntimeError('执行证据截图编码失败')
        encoded.tofile(root/name)

    def save_image(name,image):
        for job in list(image_jobs):
            if job.done():
                job.result()
                image_jobs.remove(job)
        if len(image_jobs)>=8:
            raise ReplayStopped('执行证据写盘跟不上，已停止，避免拖延秒接动作')
        image_jobs.append(image_pool.submit(write_image,name,image))

    status,reason='failed','尚未执行'
    try:
        listener.start()
        listener.wait()
        driver = InputDriver(window,stop,not dry_run)
        print(f"回放日志：{root.resolve()}\n模式：{'仅演练，不发送输入' if dry_run else '实际键鼠回放'}",flush=True)
        print(f'允许迟到至少 {late_tolerance_ms} 个游戏毫秒；费用和身份条件仍须满足。',flush=True)
        arm('已加载技能轴，等待手动启动')
        handoff=None
        with mss.mss() as capture_device:
            for index,action in enumerate(actions):
                if action['kind']=='set_auto':
                    from .auto_control import replay_auto
                    latest={}
                    def auto_capture():
                        if not window.snapshot().focused:arm('游戏失去焦点，AUTO 切换已暂停')
                        image,obs,_=capture(capture_device,lightweight=True)
                        latest['image']=image
                        return obs
                    def auto_click(point):
                        save_image(f'auto-{index}-before.jpg',latest['image'])
                        driver.target(dict(target=dict(point=point)))
                    try:replay_auto(action,auto_capture,auto_click,check,stop.wait,emit)
                    finally:
                        if 'image' in latest:save_image(f'auto-{index}-last.jpg',latest['image'])
                    handoff=None
                    continue
                if action['kind']=='skip_transition':
                    from .transition import replay_transition,hud_absent
                    print('等待转场：'+action['id']+'；倒计时恢复时立即停止连点。',flush=True)
                    transition_proofs=set()
                    def transition_capture():
                        image,obs,_=capture(capture_device,lightweight=True)
                        phase=('confirm' if obs.get('transition_confirm',{}).get('valid') else
                               'hidden' if hud_absent(obs) else
                               'after' if 'hidden' in transition_proofs and obs['timer']['valid'] else 'before')
                        if phase not in transition_proofs:
                            save_image(f'transition-{index}-{phase}.jpg',image)
                            transition_proofs.add(phase)
                        return obs
                    replay_transition(action,transition_capture,lambda:driver.target(action),check,stop.wait,emit)
                    print('转场已结束，停止连点并重新校准后续技能时间。',flush=True)
                    handoff=None
                    continue
                chained=action['trigger'].get('after_previous',False)
                strict=action['trigger'].get('strict_time',False)
                if chained and (handoff is None or handoff['action_id']!=actions[index-1]['id']):
                    raise ReplayStopped(f"动作 {action['id']} 缺少上一条技能的释放确认，不能秒接")
                emit('action_wait',action_id=action['id'],mode_text='秒接上个' if chained else '严格卡点' if strict else '定时释放')
                print(f"等待动作 {index+1}/{len(actions)}：{action.get('notes',action['id'])}",flush=True)
                deadline=(handoff['verified_at']+chain_wait_ms(action)/1000
                          if chained else time.monotonic()+120)
                reuse=handoff if chained else None
                max_age=min([.35]+[c['max_age_ms']/1000 for c in action['trigger']['conditions']])
                timing=TimingTracker()
                memory=CardIdentityMemory(action)
                predicted_start_at=None
                strict_pickup=None
                last_block=None
                previous_block='尚未取得有效观测'
                last_block_print=0
                focus_pause_required=False
                while True:
                    check()
                    try:
                        if focus_pause_required or not window.snapshot().focused:
                            raise ObservationFocusLost('等待期间游戏失去焦点')
                        if reuse is not None and time.monotonic()-reuse['captured_at']<=max_age:
                            image,obs=reuse['image'],reuse['observation']
                            age=time.monotonic()-reuse['captured_at']
                        else:
                            image,obs,age=capture(capture_device,focus_slot=action.get('skill',{}).get('slot'),inspect_selection=strict)
                    except ObservationFocusLost:
                        if strict_pickup is not None:
                            raise ReplayStopped('严格提牌后游戏失去焦点，已终止；请检查残留选卡状态')
                        arm('游戏失去焦点，回放已暂停')
                        timing=TimingTracker()
                        memory=CardIdentityMemory(action)
                        deadline=time.monotonic()+(chain_wait_ms(action)/1000 if chained else 120)
                        reuse=None;predicted_start_at=None;focus_pause_required=False
                        continue
                    if chained and time.monotonic()>deadline:
                        raise ReplayStopped(f"动作 {action['id']} 秒接等待条件超时；{previous_block}")
                    reuse=None
                    now=time.monotonic()
                    captured_at=now-age
                    had_previous=timing.previous is not None
                    consistent=timing.update(obs,captured_at)
                    if had_previous and not consistent:
                        memory=CardIdentityMemory(action)
                    effective=memory.apply(obs,now)
                    pickup_confirmed=False
                    if strict_pickup is not None:
                        slot=action['skill']['slot']
                        selections=obs.get('selected_cards',[])
                        if any(s['valid'] and s['value'] and i!=slot for i,s in enumerate(selections)):
                            raise ReplayStopped('严格卡点检测到其他卡槽被提起，未点击目标')
                        hint=selections[slot] if len(selections)>slot else {}
                        pickup_confirmed=hint.get('valid') and hint.get('value')
                        if pickup_confirmed:
                            if not strict_pickup['confirmed']:
                                strict_pickup['confirmed']=True
                                timing=TimingTracker(slow_time=True)
                                consistent=timing.update(obs,captured_at)
                                emit('strict_pickup_confirmed',action_id=action['id'],capture_t_ns=round(captured_at*1e9)-origin,remaining_ms=timer_ms(obs['timer']['value']) if obs['timer']['valid'] else None)
                            strict_pickup['last_seen']=now
                            effective['portraits'][slot]=dict(strict_pickup['portrait'],evidence_source='strict_selected_slot')
                            effective['card_costs'][slot]=dict(value=hint['cost'],valid=True,score=hint['score'])
                            if action['skill'].get('identity')==DRESS_HINA_SHOOTING_CARD:
                                effective['card_readiness'][slot]=dict(identity=DRESS_HINA_SHOOTING_CARD,valid=True,value='ready',evidence_source='selection_accepted')
                        elif now-strict_pickup['last_seen']>(.2 if strict_pickup['confirmed'] else .5):
                            raise ReplayStopped('严格卡点提牌未确认或选卡已丢失，未点击目标；请检查残留选卡状态')
                    execution_budget=target_budget_ms if pickup_confirmed else input_budget_ms
                    if pickup_confirmed and timing.reliable(now) and not strict_pickup['calibration_logged']:
                        strict_pickup['calibration_logged']=True
                        emit('strict_pickup_clock_calibrated',action_id=action['id'],game_rate=timing.rate,
                             previous_game_rate=strict_pickup['previous_rate'],
                             slowdown_observed=timing.rate<strict_pickup['previous_rate']*.8)
                    # 根据实测倒计时速度，把采集/识别与选卡点击延迟计入提前窗口。
                    lead=timing.lead_ms(age,execution_budget) if action['kind']=='cast_skill' and not chained else 0
                    projected=(timer_ms(obs['timer']['value'])-lead if obs['timer']['valid'] else None)
                    if strict and timing.reliable(now):projected=timing.estimate_remaining_at(now+execution_budget/1000)
                    state=readiness(action,effective,lead_ms=lead,
                                    projected_remaining_ms=projected if strict and timing.reliable(now) else None)
                    previous_block=blocking_reason(action,effective)
                    if strict and not timing.reliable(now) and state!='missed':
                        state='blocked'
                        previous_block='严格卡点需要稳定的倒计时流速，正在重新校准'
                    if strict and not dry_run and not pickup_confirmed and state!='missed':
                        state='blocked'
                        previous_block='等待严格卡点提牌条件或选卡确认'
                    unavailable=card_availability_reason(action,effective)
                    if unavailable:
                        if state!='missed':state='blocked'
                        previous_block=unavailable
                    if (strict and obs['timer']['valid'] and state in ('early','ready','missed') and
                        timer_ms(obs['timer']['value'])-action['trigger']['remaining_ms']>strict_early_tolerance_ms(action)):
                        state='early'
                        if previous_block=='前置条件已满足':
                            previous_block='等待可见倒计时到达目标，严格卡点不允许提前点击'
                    if previous_block!='前置条件已满足':
                        if previous_block!=last_block or now-last_block_print>=1:
                            emit('precondition_blocked',action_id=action['id'],reason_text=previous_block)
                        if now-last_block_print>=1:
                            print(f'等待条件：{previous_block}',flush=True)
                            last_block_print=now
                        last_block=previous_block
                    emit('observation',action_id=action['id'],observation=obs,processing_seconds=age,
                         decision=state,blocking_reason=previous_block,lead_game_ms=lead,
                         measured_game_rate=timing.rate,identity_evidence=effective.get('portraits'),
                         timer_consistent=consistent,strict_pickup_confirmed=bool(pickup_confirmed))
                    current=timer_ms(obs['timer']['value']) if obs['timer']['valid'] else None
                    if not chained and strict_pickup is None and current is not None and current <= action['trigger']['remaining_ms']+2000:
                        deadline=min(deadline,now+action['trigger']['timeout_ms']/1000)
                    if (strict and not dry_run and strict_pickup is None and consistent and age<=max_age
                        and strict_pickup_ready(action,effective)):
                        driver.select(action,script['keymap'])
                        at=time.monotonic()
                        strict_pickup=dict(confirmed=False,last_seen=at,portrait=copy.deepcopy(effective['portraits'][action['skill']['slot']]),
                                           previous_rate=timing.rate,calibration_logged=False)
                        timing=TimingTracker()
                        deadline=max(deadline,at+max(30,action['trigger']['timeout_ms']/1000))
                        emit('strict_pickup_sent',action_id=action['id'],remaining_ms=current,
                             observed_ex_cost=obs['cost']['value'],card_cost=obs['card_costs'][action['skill']['slot']]['value'])
                        save_image(f'{index+1:03d}-pickup.jpg',image)
                        continue
                    if state=='missed' and consistent:
                        save_image(f'{index+1:03d}-missed.jpg',image)
                        if strict:
                            raise ReplayStopped(f"动作 {action['id']} 严格卡点已错过，未点击目标；{previous_block}")
                        if action['trigger']['on_failure']=='pause':
                            arm(f"动作 {action['id']} 已错过时间；{previous_block}；继续前请检查战斗是否需要重开")
                            deadline=time.monotonic()+120
                            timing=TimingTracker()
                            memory=CardIdentityMemory(action)
                            continue
                        raise ReplayStopped(f"动作 {action['id']} 已错过目标时间，未补放；{previous_block}")
                    if ((predictive_timing or strict) and not chained and action['kind']=='cast_skill' and consistent and age<=max_age
                        and previous_block=='前置条件已满足' and state in ('early','ready')):
                        predicted_start_at=timing.predict_start_at(action['trigger']['remaining_ms'],time.monotonic(),
                                                                  input_duration_ms=execution_budget)
                        if predicted_start_at is not None and predicted_start_at-captured_at>max_age:
                            predicted_start_at=None
                        if predicted_start_at is not None:
                            emit('release_forecast',action_id=action['id'],remaining_ms=current,game_rate=timing.rate,
                                 cast_start_delay_ms=round((predicted_start_at-time.monotonic())*1000,3),
                                 forecast_cast_start_t_ns=round((predicted_start_at-origin/1e9)*1e9),input_duration_ms=execution_budget,
                                 input_kind='target_only' if pickup_confirmed else 'full_cast')
                            break
                    estimated_due=timing.estimate_remaining_at(time.monotonic()+execution_budget/1000)
                    strict_due=(not strict or (estimated_due is not None and
                        estimated_due<=action['trigger']['remaining_ms']))
                    if state=='ready' and (chained or consistent) and age <= max_age and strict_due:
                        break
                    if now > deadline:
                        if chained:
                            raise ReplayStopped(f"动作 {action['id']} 秒接等待超时：{previous_block}")
                        if action['trigger']['on_failure']=='pause':
                            arm(f"动作 {action['id']} 等待超时：{state}")
                            deadline=time.monotonic()+120
                            timing=TimingTracker()
                            memory=CardIdentityMemory(action)
                            continue
                        raise ReplayStopped(f"动作 {action['id']} 等待超时：{state}；可能费用、卡牌、AUTO、暂停或识别不符合")
                    if not pickup_confirmed and action.get('skill',{}).get('identity')==DRESS_HINA_SHOOTING_CARD:
                        slots=obs.get('card_readiness',[])
                        slot=action['skill']['slot']
                        if len(slots)>slot and (not slots[slot]['valid'] or slots[slot]['value']!='ready'):
                            try:fast_watch(capture_device,action,deadline)
                            except ObservationFocusLost:focus_pause_required=True
                            continue  # 恢复彩色立即重查前置条件，不再追加普通轮询等待。
                    stop.wait(.005 if chained or strict_pickup is not None else .025)
                emit('planned',action_id=action['id'],planned_remaining_ms=action['trigger']['remaining_ms'],
                     observed_remaining_ms=current, deviation_ms=current-action['trigger']['remaining_ms'] if current is not None else None,
                     predicted_release_remaining_ms=current-lead if current is not None else None, input_lead_ms=input_lead_ms,
                     trigger_kind='after_previous' if chained else 'battle_timer')
                if chained:
                    if time.monotonic()>deadline:
                        raise ReplayStopped(f"动作 {action['id']} 已超过秒接等待上限")
                    emit('chain_trigger',action_id=action['id'],previous_action_id=handoff['action_id'],
                         since_previous_input_ms=round((time.monotonic()-handoff['input_sent_at'])*1000),
                         since_previous_confirmation_ms=round((time.monotonic()-handoff['verified_at'])*1000),
                         reused_confirmation_frame=obs is handoff['observation'],simulated=dry_run)
                if not chained and current is not None and current < action['trigger']['remaining_ms']:
                    print(f'动作 {action["id"]} 比计划晚 {action["trigger"]["remaining_ms"]-current} 个游戏毫秒，仍在容差内，继续执行。',flush=True)
                if action['kind']=='wait':
                    emit('wait_satisfied',action_id=action['id'])
                    continue
                if predicted_start_at is not None:
                    driver.wait_until(predicted_start_at)
                if time.monotonic()-captured_at>max_age:
                    raise ReplayStopped('释放前观测已过期，已停止以免使用过时的费用或卡牌状态')
                if strict:
                    early_limit=strict_early_tolerance_ms(action)
                    estimated_press=timing.estimate_remaining_at(time.monotonic())
                    early_press_ms=estimated_press-action['trigger']['remaining_ms']
                    if early_press_ms>early_limit:
                        press_deadline=time.monotonic()+(early_press_ms-early_limit)/timing.rate/1000
                        if press_deadline-captured_at>max_age:
                            raise ReplayStopped('严格提前窗口需等待过久，当前观测将过期，未点击目标')
                        emit('strict_early_wait',action_id=action['id'],predicted_press_early_ms=early_press_ms,
                             early_tolerance_ms=early_limit)
                        driver.wait_until(press_deadline)
                    if time.monotonic()-captured_at>max_age:
                        raise ReplayStopped('严格提前窗口检查时观测已过期，未点击目标')
                    estimated_press=timing.estimate_remaining_at(time.monotonic())
                    estimated_release=timing.estimate_remaining_at(time.monotonic()+execution_budget/1000)
                    deviation=estimated_release-action['trigger']['remaining_ms']
                    emit('strict_timing_check',action_id=action['id'],predicted_release_remaining_ms=estimated_release,
                         deviation_ms=deviation,tolerance_ms=timing_tolerance_ms(action),input_duration_ms=execution_budget,
                         selected=bool(pickup_confirmed),early_tolerance_ms=early_limit,late_tolerance_ms=timing_tolerance_ms(action),
                         observed_remaining_ms=current,predicted_press_remaining_ms=estimated_press,
                         predicted_press_early_ms=estimated_press-action['trigger']['remaining_ms'])
                    if (deviation>early_limit or deviation < -timing_tolerance_ms(action)-1e-6 or
                        estimated_press-action['trigger']['remaining_ms']>early_limit):
                        raise ReplayStopped(f"动作 {action['id']} 严格卡点预测偏差 {deviation:.1f} 毫秒超出容差，未发送输入")
                cast_started_at=time.monotonic()
                if pickup_confirmed:driver.target(action)
                else:driver.cast(action,script['keymap'])
                input_sent_at=time.monotonic()
                raw_timing=getattr(driver,'last_timing',{})
                input_timing={k.replace('_ns','_t_ns'):v-origin for k,v in raw_timing.items()} if isinstance(raw_timing,dict) else {}
                press=input_timing.get('target_press_t_ns')
                release=input_timing.get('target_release_t_ns')
                emit('input_sent' if not dry_run else 'dry_run',action_id=action['id'],
                     input_start_t_ns=round(cast_started_at*1e9)-origin,input_end_t_ns=round(input_sent_at*1e9)-origin,
                     input_timing=input_timing,input_kind='target_only' if pickup_confirmed else 'full_cast',
                     estimated_press_remaining_ms=timing.estimate_remaining_at((press+origin)/1e9) if press is not None else None,
                     estimated_release_remaining_ms=timing.estimate_remaining_at((release+origin)/1e9) if release is not None else None)
                measured_duration=(input_sent_at-cast_started_at)*1000
                if not dry_run and (10 if pickup_confirmed else 50)<=measured_duration<=(250 if pickup_confirmed else 500):
                    if pickup_confirmed:target_budget_ms=.5*target_budget_ms+.5*measured_duration
                    else:input_budget_ms=.5*input_budget_ms+.5*measured_duration
                    emit('input_latency_sample',action_id=action['id'],measured_ms=measured_duration,
                         next_input_budget_ms=target_budget_ms if pickup_confirmed else input_budget_ms,
                         input_kind='target_only' if pickup_confirmed else 'full_cast')
                # 图像已在内存保留；释放输入前不执行 JPEG 编码或磁盘写入。
                save_image(f'{index+1:03d}-before.jpg',image)
                if dry_run:
                    # 演练只预览接续决策，不宣称上一条已在游戏中施放。
                    handoff=dict(action_id=action['id'],image=image,observation=obs,captured_at=captured_at,
                                 verified_at=time.monotonic(),input_sent_at=input_sent_at,simulated=True)
                    continue
                before=obs
                from .cast_verification import CastVerifier
                verifier=CastVerifier(action,before)
                verified=False
                verification_samples=[]
                first_after_image=None
                first_after_at=None
                end=time.monotonic()+3
                while time.monotonic()<end:
                    check()
                    driver.guard()
                    after_image,after,after_age=capture(capture_device)
                    after_captured_at=time.monotonic()-after_age
                    if first_after_image is None:
                        first_after_image=after_image
                        first_after_at=after_captured_at
                    verification_samples.append(after)
                    emit('verification_observation',action_id=action['id'],observation=after,
                         elapsed_since_input_ms=round((time.monotonic()-input_sent_at)*1000))
                    evidence=verifier.update(after,after_captured_at)
                    if evidence:
                        handoff=dict(action_id=action['id'],image=after_image,observation=after,
                                     captured_at=after_captured_at,verified_at=time.monotonic(),
                                     input_sent_at=input_sent_at,simulated=False)
                        save_image(f'{index+1:03d}-after.jpg',after_image)
                        emit('visually_supported',action_id=action['id'],observation=after,
                             evidence_capture_t_ns=round(after_captured_at*1e9)-origin,recognition_end_t_ns=time.monotonic_ns()-origin,
                             since_input_end_ms=(time.monotonic()-input_sent_at)*1000,
                             observed_after_remaining_ms=timer_ms(after['timer']['value']) if after['timer']['valid'] else None,
                             evidence=evidence)
                        verified=True
                        break
                    stop.wait(.03)
                if not verified:
                    from .replay_diagnostics import failure_diagnostic
                    selections=[]
                    proofs=[]
                    if first_after_image is not None:
                        proofs=[('first_after',first_after_image,first_after_at),('last_after',after_image,after_captured_at)]
                    for name,proof,at in proofs:
                        try:selection=reader.selected_card(reader.normalize_image(proof),action['skill']['slot'])
                        except Exception as error:selection=dict(valid=False,value=None,error=str(error))
                        selections.append(dict(sample=name,capture_t_ns=round(at*1e9)-origin,
                                               state=selection))
                    diagnostic=failure_diagnostic(action,before,verification_samples,selections,pickup_confirmed=bool(pickup_confirmed))
                    diagnostic['evidence_images']=[f'{index+1:03d}-before.jpg']
                    if proofs:diagnostic['evidence_images'] += [f'{index+1:03d}-first-after.jpg',f'{index+1:03d}-unconfirmed.jpg']
                    emit('cast_failure_diagnostic',action_id=action['id'],**diagnostic)
                    if proofs:
                        save_image(f'{index+1:03d}-first-after.jpg',first_after_image)
                        save_image(f'{index+1:03d}-unconfirmed.jpg',after_image)
                    raise ReplayStopped(f"动作 {action['id']} 输入已发送，但未确认技能释放；诊断：{diagnostic['reason_text']}；已停止，避免重复施放")
        status,reason='completed','技能轴执行结束' if not dry_run else '技能轴演练结束，未发送输入'
    except ReplayStopped as error:
        status,reason='stopped',str(error)
    except BaseException as error:
        status,reason='failed',str(error)
    finally:
        stop.set()
        listener.stop()
        if listener.ident is not None:
            listener.join(timeout=2)
        image_pool.shutdown(wait=True)
        try:
            for job in image_jobs:job.result()
        except Exception as error:
            status,reason='failed',f'执行证据保存失败：{error}'
        emit('termination',status=status,reason_text=reason)
        log.close()
        (root/'summary.json').write_text(json.dumps(dict(status=status,reason_text=reason,dry_run=dry_run),
                                                   ensure_ascii=False,indent=2),encoding='utf-8')
        print(f'回放结束：{reason}\n日志目录：{root.resolve()}',flush=True)
    return 0 if status=='completed' else 1
