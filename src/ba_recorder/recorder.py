# SPDX-License-Identifier: GPL-3.0-or-later
import itertools
import queue
import threading
import time
import json
import sys

from .model import Clock, in_scope
from .messages import reason_text
from .storage import FrameBuffer, SessionStore
from .windows import Window
from .recording_media import prepare_media


def key_name(key):
    name = getattr(key, "name", None)
    if name:
        return name
    vk = getattr(key, "vk", None)
    # 使用 Windows 虚拟键码，确保按住或松开 Shift 时按键标识保持一致。
    if vk is not None:
        if 112 <= vk <= 135:
            return f'f{vk-111}'
        return f"vk:{vk}"
    return "unknown"


def listener_callbacks(on_key, on_mouse, guard):
    # pynput 1.8 增加 injected 参数。guard 使用 *args，会阻止库自动裁剪参数，
    # 因此这里显式接收可选参数，同时兼容旧版/不同后端的较短调用。
    return {
        'keyboard': {
            'on_press': guard(lambda key, injected=None: on_key(key, True)),
            'on_release': guard(lambda key, injected=None: on_key(key, False)),
        },
        'mouse': {
            'on_move': guard(lambda x, y, injected=None: on_mouse('mouse_move', x, y)),
            'on_click': guard(lambda x, y, button, pressed, injected=None: on_mouse(
                'mouse_down' if pressed else 'mouse_up', x, y, button=button.name)),
            'on_scroll': guard(lambda x, y, dx, dy, injected=None: on_mouse('scroll', x, y, delta=[dx, dy])),
        },
    }


def record(hwnd, config, output, *, control_stdin=False, start_when_focused=False, session_name=None):
    loading=time.monotonic()
    print('正在预加载截图与编码依赖；完成前尚未安装输入监听，也尚未开始录制。',flush=True)
    if control_stdin:
        print('@status '+json.dumps(dict(state='initializing',reason_text='正在预加载截图与编码依赖'),ensure_ascii=False),flush=True)
    cv2,np=prepare_media(config)
    print(f'媒体依赖预加载完成，用时 {time.monotonic()-loading:.2f} 秒。',flush=True)
    if config.capture_backend=='wgc':
        from .wgc_capture import require_wgc
        require_wgc()
    from pynput import keyboard, mouse
    import mss
    import mss.tools

    window = Window(hwnd)
    if hasattr(window,'follow_game_window'):
        window.follow_game_window=True  # 仅录制跟随游戏实例；复现仍固定其明确选择的窗口。
    clock = Clock()
    store = SessionStore(output, config.to_dict(), clock,
                         {**window.snapshot().to_dict(), "title": window.title},session_name=session_name)
    buffer = FrameBuffer(int(config.pre_seconds * 1e9), int(config.post_seconds * 1e9),
                         config.max_buffer_frames, store.frame, config.all_frames)
    events = queue.Queue(maxsize=config.queue_size)
    commands = queue.SimpleQueue()
    active = threading.Event()
    paused = False
    resume_pending = False
    segment_pending = False
    held_inputs = set()
    suspended_inputs = set()
    aborted = threading.Event()
    abort_reason = ['abort_hotkey']
    failed = threading.Event()
    failure = []
    sequence = itertools.count()
    event_lock = threading.Lock()
    buffer_lock = threading.RLock()
    writer_stop=threading.Event()
    input_writer=None
    watchdog_stop=threading.Event()
    watchdog=None
    restarting=threading.Event()
    main_tick=[clock.now()]
    controls_down = set()
    controls_lock = threading.Lock()
    listeners = []
    reason, status = "unknown", "failed"
    frame_index = 0
    capture_after_ns=0
    last_state = None
    final_state = window.snapshot()
    stopping_at = None
    stopping_reason='stop_hotkey'
    started = False
    input_count = 0
    last_progress_ns = -1_000_000_000
    start_pending = start_when_focused
    video = None
    last_drop_log = -1_000_000_000

    def status_message(state, **details):
        if control_stdin:
            print('@status ' + json.dumps({'state': state, **details}, ensure_ascii=False), flush=True)

    def read_controls():
        try:
            from .control_pipe import control_lines
            for line in control_lines(sys.stdin):
                command = json.loads(line).get('command')
                if command == 'stop':
                    commands.put((config.stop_key, clock.now()))
                    return  # 正常停止后不再等待 EOF，也不把它覆盖成连接断开。
                elif command in ('pause', 'resume','restart'):
                    commands.put((command, clock.now()))
                elif command == 'abort':
                    abort_reason[0] = 'abort_request'
                    aborted.set()
            print('控制管道诊断：读取结束，Windows 管道状态或输入流确认 EOF。',flush=True)
            commands.put(('controller_disconnected',clock.now()))
        except Exception as error:
            fail(error)

    def fail(error):
        if not failed.is_set():
            failure.append(f"{type(error).__name__}: {error}")
        active.clear()
        failed.set()

    def input_state():
        snapshot=getattr(window,'input_snapshot',None)
        return snapshot() if snapshot is not None else window.snapshot()

    def emit(kind, payload, t_ns=None, state=None):
        # 入队前检查录制范围，不保存输入法转换后的文本或字符。
        t_ns = clock.now() if t_ns is None else t_ns
        state = input_state() if state is None else state
        point = payload.get("desktop")
        if not in_scope(state, point):
            return
        with event_lock:
            if t_ns<capture_after_ns:return
            token=(('key',payload.get('key')) if kind.startswith('key_') else
                   ('mouse',payload.get('button')) if kind in ('mouse_down','mouse_up') else None)
            if token in suspended_inputs:
                if kind.endswith('_up'):suspended_inputs.discard(token)
                return
            if not active.is_set():
                if token is not None and kind.endswith('_down'):suspended_inputs.add(token)
                return
            if token is not None:
                if kind.endswith('_down'):held_inputs.add(token)
                elif kind.endswith('_up'):held_inputs.discard(token)
            try:
                events.put_nowait({"schema_version": "1.0", "id": f"e{next(sequence)}",
                                   "t_ns": t_ns, "kind": kind, "window": state.to_dict(),
                                   "data": payload})
            except queue.Full as error:
                raise RuntimeError(f'输入队列已满（{config.queue_size} 条），写盘跟不上；已停止并保留已接收数据') from error

    def guard(callback):
        def guarded(*args):
            try:
                callback(*args)
            except Exception as error:
                fail(error)
        return guarded

    def on_key(key, pressed):
        t_ns = clock.now()
        name = key_name(key)
        if name in (config.start_key, config.stop_key, config.abort_key):
            with controls_lock:
                if pressed and name not in controls_down:
                    controls_down.add(name)
                    if name == config.abort_key:
                        commands.put(('restart',t_ns))
                    else:
                        diagnostics={}
                        if name==config.start_key:
                            # 控制键回调只读取已有 Python 对象，不调用任何 Win32 接口。
                            pressed_state=getattr(window,'_input_state',None)
                            if pressed_state:
                                diagnostics={'target_hwnd':pressed_state.hwnd,'target_pid':pressed_state.pid,
                                             'target_focused_at_keypress':pressed_state.focused,'geometry_source':'capture_cache'}
                        commands.put((name, t_ns,diagnostics))
                elif not pressed:
                    controls_down.discard(name)
            return
        if config.allowed_keys and name not in config.allowed_keys:
            return
        emit("key_down" if pressed else "key_up", {"key": name}, t_ns)

    def on_mouse(kind, x, y, **extra):
        t_ns = clock.now()
        if not active.is_set() and kind!='mouse_up':return
        state = input_state()
        if in_scope(state, (x, y)):
            emit(kind, {**state.point(x, y), **extra}, t_ns, state)

    def lifecycle(kind, data, state=None, t_ns=None):
        store.event({"schema_version": "1.0", "id": f"e{next(sequence)}",
                     "t_ns": clock.now() if t_ns is None else t_ns, "kind": kind,
                     "window": (state or window.snapshot()).to_dict(), "data": data})

    def persist_input(event):
        nonlocal input_count
        store.event(event)
        input_count += 1
        if event['kind'] in ('key_down','mouse_down','mouse_up','scroll'):
            if video:video.operation(event['t_ns'])
            else:
                with buffer_lock:buffer.operation(event['t_ns'])

    def write_inputs():
        try:
            while not writer_stop.is_set() or not events.empty():
                try:event=events.get(timeout=.03)
                except queue.Empty:continue
                try:persist_input(event)
                finally:events.task_done()
        except Exception as error:
            fail(RuntimeError(f'输入写入线程失败：{type(error).__name__}：{error}'))

    def drain():
        # 只用于写入线程退出后的最终补存，不在截图主循环中同步写盘。
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            persist_input(event)
            events.task_done()

    def watch_capture():
        reported=False
        while not watchdog_stop.wait(.25):
            stalled=not restarting.is_set() and clock.now()-main_tick[0]>3_000_000_000
            if stalled and not reported:
                reported=True
                try:
                    import faulthandler
                    path=store.path/'capture-stall.log'
                    with path.open('a',encoding='utf-8') as handle:
                        handle.write(f'录制主循环停顿超过3秒；已开始 {started}；时钟 {clock.now()}；输入队列 {events.qsize()}\n')
                        handle.flush()
                        faulthandler.dump_traceback(file=handle,all_threads=True)
                    print(f'录制主循环已停顿超过3秒，线程诊断已保存：{path}',flush=True)
                except OSError as error:
                    print(f'采样停顿诊断保存失败：{error}',flush=True)
            elif not stalled:
                reported=False

    print(f"录制会话：{store.path}", flush=True)
    print('正在初始化输入监听和截图组件，完成后会显示就绪提示。',flush=True)
    status_message('initializing',session_path=str(store.path))
    watchdog=threading.Thread(target=watch_capture,daemon=True,name='battle-capture-watchdog')
    watchdog.start()
    try:
        if control_stdin:
            threading.Thread(target=read_controls, daemon=True).start()
        callbacks = listener_callbacks(on_key, on_mouse, guard)
        listeners = [keyboard.Listener(**callbacks['keyboard']), mouse.Listener(**callbacks['mouse'])]
        for listener in listeners:
            listener.start()
        # 先安装监听，再初始化视频/截图，初始化期间按下的热键也保留在队列。
        if config.video_enabled:
            from .video import VideoWorker
            video=VideoWorker(store,config)
            print(f'连续视频已启用，目标 {config.video_fps} FPS；额外 PNG：'
                  f'{"全部采样" if config.all_frames else "操作附近" if config.save_event_png else "不保留"}',flush=True)
        else:
            print(f'连续截图已启用：{config.sample_hz:g} 张/秒，{config.screenshot_format.upper()}，'
                  '每次采样均保存；请按开始热键后操作。',flush=True)
        input_writer=threading.Thread(target=write_inputs,daemon=True,name='battle-input-writer')
        input_writer.start()
        next_capture = clock.now()
        if config.capture_backend=='wgc':
            from .wgc_capture import WgcCapture
            capture_context=WgcCapture(window,clock,config.video_fps)
            store.manifest['capture']={'backend':'wgc','isolation':'process',
                'timestamp_kind':'callback_arrival_qpc','client_area_only':True}
        else:
            capture_context=mss.mss()
            store.manifest['capture']={'backend':'mss','timestamp_kind':'grab_start_qpc','client_area_only':True}
        print(f'画面采集后端：{config.capture_backend}'+('（独立进程）' if config.capture_backend=='wgc' else '（兼容模式）'),flush=True)
        with capture_context as capture:
            main_tick[0]=clock.now()
            start_hint=('已接收开始请求，切回游戏后自动录制，无需再次按开始热键；' if start_when_focused else
                        f'请切换到游戏窗口；{config.start_key.upper()} 开始录制，')
            print(f"已准备就绪。{start_hint}{config.stop_key.upper()} 停止并保存，"
                  f"{config.abort_key.upper()} 重启录制并丢弃本段已有内容。",flush=True)
            status_message('armed',session_path=str(store.path),start_request_pending=start_when_focused)
            while True:
                main_tick[0]=clock.now()
                if aborted.is_set():
                    status, reason = "aborted", abort_reason[0]
                    break
                if failed.is_set():
                    raise RuntimeError("输入处理失败：" + "；".join(failure))
                if video and video.error:
                    raise RuntimeError(video.error)
                if any(not listener.is_alive() for listener in listeners):
                    raise RuntimeError("输入监听器意外停止")
                state = window.snapshot()
                final_state = state
                if start_pending and not started and in_scope(state):
                    commands.put((config.start_key, clock.now()))
                    start_pending = False
                if resume_pending and paused and in_scope(state):
                    commands.put(('resume',clock.now()))
                while not commands.empty():
                    packet=commands.get_nowait()
                    command,t_ns=packet[:2]
                    # 重启清理期间按下的停止等命令仍有效，但时间不能落到新片段开始之前。
                    t_ns=max(t_ns,capture_after_ns)
                    if command=='restart' and started and stopping_at is None:
                        restarting.set()
                        status_message('restarting')
                        with event_lock:
                            active.clear()
                            suspended_inputs.update(held_inputs)
                            held_inputs.clear()
                        writer_stop.set()
                        input_writer.join(timeout=15)
                        if input_writer.is_alive():raise RuntimeError('输入写入线程未退出，不能安全重启录制')
                        if video:video.close()
                        store.restart({**state.to_dict(),'title':window.title})
                        buffer=FrameBuffer(int(config.pre_seconds*1e9),int(config.post_seconds*1e9),
                                           config.max_buffer_frames,store.frame,config.all_frames)
                        video=VideoWorker(store,config) if config.video_enabled else None
                        sequence=itertools.count()
                        frame_index=input_count=0
                        paused=resume_pending=segment_pending=False
                        last_state=None
                        capture_after_ns=clock.now()
                        next_capture=capture_after_ns
                        last_progress_ns=-1_000_000_000
                        lifecycle('recording_start',{},state,capture_after_ns)
                        writer_stop.clear()
                        input_writer=threading.Thread(target=write_inputs,daemon=True,name='battle-input-writer')
                        input_writer.start()
                        active.set()
                        restarting.clear()
                        main_tick[0]=clock.now()
                        status_message('recording' if state.focused else 'unfocused',reset=True,
                                       session_path=str(store.path),recording_started=True,captured_frames=0,input_events=0)
                        print('录制已重启：之前的内容已清空，保留同一录制名称；切回游戏后直接采集，无需再次按开始热键。',flush=True)
                        continue
                    if command=='controller_disconnected':
                        print('控制界面连接已关闭，正在停止并保留已采集的数据。',flush=True)
                        command=config.stop_key
                        stopping_reason='controller_disconnected'
                    if command==config.start_key:
                        # 处理队列期间窗口也可能发生切换，不沿用循环开头的旧焦点快照。
                        state=window.snapshot()
                        final_state=state
                        print('启动请求诊断：'+json.dumps(dict(processing_delay_ms=round((clock.now()-t_ns)/1e6),
                            keypress=packet[2] if len(packet)>2 else {},
                            current=window.focus_diagnostics() if hasattr(window,'focus_diagnostics') else {}),ensure_ascii=False),flush=True)
                        print(f'已收到 {config.start_key.upper()}：'+('目标窗口在前台。' if in_scope(state) else '目标窗口尚未在前台，已保留开始请求。'),flush=True)
                    if command == 'pause' and started and stopping_at is None and not paused:
                        with event_lock:
                            active.clear()
                            suspended_inputs.update(held_inputs)
                            held_inputs.clear()
                        paused=True
                        resume_pending=False
                        lifecycle('recording_pause',{},state,t_ns)
                        print('录制已暂停；输入和画面暂停采集，继续后写入同一会话。暂停录制不会暂停游戏。',flush=True)
                        status_message('paused')
                    elif command == 'resume' and paused and stopping_at is None:
                        if not in_scope(state):
                            resume_pending=True
                            status_message('resume_pending')
                            continue
                        lifecycle('recording_resume',{},state,t_ns)
                        capture_after_ns=t_ns
                        paused=False
                        resume_pending=False
                        segment_pending=True
                        next_capture=clock.now()
                        active.set()
                        print('已继续录制，接续当前会话。',flush=True)
                        status_message('recording')
                    if command == config.start_key and not started:
                        if in_scope(state):
                            started = True
                            capture_after_ns=t_ns
                            store.manifest['window']={**state.to_dict(),'title':window.title}
                            lifecycle("recording_start", {}, state, t_ns)
                            active.set()
                            print("正在录制", flush=True)
                            status_message('recording')
                        else:
                            start_pending=True
                            status_message('armed',start_request_pending=True,
                                reason_text=f'已收到 {config.start_key.upper()}，请切回选中的游戏窗口；进入前台后开始。')
                    elif command == config.stop_key and not started:
                        status, reason = 'aborted', 'cancel_before_start'
                        aborted.set()
                        abort_reason[0] = reason
                    elif command == config.stop_key and started and stopping_at is None:
                        active.clear()
                        resume_pending=False
                        lifecycle("recording_stop", {"post_seconds": config.post_seconds}, state, t_ns)
                        stopping_at = clock.now() + int(config.post_seconds * 1e9)
                        print("正在停止并保存录制……", flush=True)
                        status_message('stopping')
                progress_now=clock.now()
                if stopping_at is None and progress_now-last_progress_ns>=1_000_000_000:
                    progress_state=('armed' if not started else 'paused' if paused else
                                    'recording' if state.focused else 'unfocused')
                    status_message(progress_state,recording_started=started,
                        captured_frames=video.written if video else len(buffer.saved),
                        input_events=input_count,wait_seconds=round(progress_now/1e9) if not started else None,
                        target_hwnd=state.hwnd,target_focused=state.focused,
                        start_request_pending=start_pending,
                        focus_diagnostics=window.focus_diagnostics() if hasattr(window,'focus_diagnostics') else {})
                    last_progress_ns=progress_now
                if started:
                    if state != last_state:
                        lifecycle("window_state", {}, state)
                        last_state = state
                        if stopping_at is None and not paused:
                            status_message('recording' if state.focused else 'unfocused')
                    now = clock.now()
                    if not paused and now >= next_capture and in_scope(state):
                        start = clock.now()
                        shot = capture.grab({"left": state.left, "top": state.top,
                                             "width": state.width, "height": state.height})
                        end = clock.now()
                        if shot is not None and config.capture_backend=='wgc':
                            start,end=shot.capture_start_ns,shot.capture_end_ns
                            if start<capture_after_ns:shot=None
                        after = window.snapshot()
                        if shot is None:
                            pass  # 等待异步首帧或新帧；控制命令和停止仍照常处理。
                        elif after != state:
                            lifecycle("capture_gap", {"reason": "window_changed_during_capture",
                                                      "reason_text": reason_text("window_changed_during_capture"),
                                                      "capture_start_ns": start, "capture_end_ns": end}, after)
                        else:
                            frame = {"schema_version": "1.0", "id": f"f{frame_index}",
                                     "t_ns": start, "capture_end_ns": end,
                                     "window": state.to_dict(), "path": f"frames/{frame_index:08d}.{'png' if video else config.screenshot_format}"}
                            if video:
                                if segment_pending:frame['segment_start']=True
                                submitted=video.submit(frame,shot.image if config.capture_backend=='wgc' else np.asarray(shot))
                                if submitted:segment_pending=False
                                if not submitted and start-last_drop_log>=1_000_000_000:
                                    lifecycle('capture_gap',{'reason':'video_queue_full',
                                              'reason_text':'编码队列已满，本帧未写入视频；累计数会保存在会话中。',
                                              'dropped_frames':video.dropped},state,start)
                                    last_drop_log=start
                            else:
                                if config.screenshot_format=='jpg':
                                    image=shot.image if config.capture_backend=='wgc' else np.asarray(shot)
                                    ok,encoded=cv2.imencode('.jpg',image[:,:,:3],[cv2.IMWRITE_JPEG_QUALITY,config.jpeg_quality])
                                    if not ok:raise RuntimeError('JPEG 截图编码失败')
                                    png=encoded.tobytes()
                                elif config.capture_backend=='wgc':
                                    ok,encoded=cv2.imencode('.png',shot.image)
                                    if not ok:raise RuntimeError('采集画面 PNG 编码失败')
                                    png=encoded.tobytes()
                                else:png = mss.tools.to_png(shot.rgb, shot.size)
                                with buffer_lock:buffer.add(frame, png)
                            frame_index += 1
                        rate=config.video_fps if video else config.sample_hz
                        next_capture = start + int(1e9 / rate)
                    if stopping_at is not None and clock.now() >= stopping_at:
                        status, reason = "completed", stopping_reason
                        break
                delay=min(.005,max(0,(next_capture-clock.now())/1e9)) if started and not paused and in_scope(state) else .01
                time.sleep(delay)
    except KeyboardInterrupt:
        status, reason = "aborted", "keyboard_interrupt"
    except Exception as error:
        status, reason = "failed", f"{type(error).__name__}: {error}"
    finally:
        active.clear()
        watchdog_stop.set()
        if watchdog is not None:watchdog.join(timeout=1)
        for listener in listeners:
            listener.stop()
        for listener in listeners:
            try:
                listener.join(timeout=2)
                if listener.is_alive():
                    status, reason = "failed", "输入监听器未能退出"
            except Exception as error:
                status, reason = "failed", f"输入监听器出错：{error}"
        try:
            writer_stop.set()
            if input_writer is not None:
                input_writer.join(timeout=15)
                if input_writer.is_alive():
                    raise RuntimeError('输入写入线程未及时退出，请检查存储设备；已写入的输入和视频保留')
            drain()
            if failed.is_set():
                status, reason = "failed", "输入处理失败：" + "；".join(failure)
        except Exception as error:
            status, reason = "failed", f"保存会话结束信息时出错：{error}"
        if video:
            try:
                store.manifest['video']=video.close()
                info=store.manifest['video']
                print(f"视频保存：{info['captured_frames']} 个真实采样帧，编码队列丢帧 {info['dropped_frames']}，"
                      f"实际频率（含失焦间隙）{info['observed_fps_including_gaps']:.1f} FPS。",flush=True)
            except Exception as error:
                status,reason='failed',str(error)
        try:
            saved_frames=video.written if video else len(buffer.saved)
            store.manifest['recording_summary']={'started':started,'captured_frames':saved_frames,'input_events':input_count}
            lifecycle('session_end',{'status':status,'reason':reason,'reason_text':reason_text(reason)},final_state)
        finally:
            store.finish(status, reason, clock.now())
    status_text = {"completed": "录制完成", "aborted": "录制已中止", "failed": "录制失败"}.get(status, status)
    if not started:
        print('本次尚未开始录制：没有采集到视频；请切回选中的游戏窗口，确认进入“正在录制”再操作。',flush=True)
    print(f"{status_text}：{reason_text(reason)}\n已保留 {saved_frames} 帧画面、{input_count} 条输入。\n已保存至：{store.path}", flush=True)
    status_message(status, session_path=str(store.path), reason_text=reason_text(reason),
                   recording_started=started,captured_frames=saved_frames,input_events=input_count)
    return 1 if status == "failed" else 0
