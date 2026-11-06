# SPDX-License-Identifier: GPL-3.0-or-later
"""执行时间事件的中文显示；只格式化已有证据，不把输入时刻当成游戏成功时刻。"""
from datetime import datetime

from .replay_script import timer_text

DISPLAY_EVENT_KINDS={
    'cast_failure_diagnostic',
    'auto_input_sent','auto_confirmed',
    'transition_armed','transition_finished',
    'action_wait','strict_pickup_sent','strict_pickup_confirmed','strict_pickup_clock_calibrated',
    'release_forecast','planned','chain_trigger','strict_early_wait','strict_timing_check',
    'input_sent','dry_run','visually_supported','termination',
}


def game_time(value):
    return '未识别' if value is None else timer_text(max(0,round(value)))


def deviation(value):
    if value is None:return '未知'
    return f'提前 {value:.3f}ms' if value>0 else f'延后 {-value:.3f}ms' if value<0 else '到点'


def relative_time(ns):
    return '未记录' if ns is None else f'T+{ns/1e9:.6f}s'


def format_event(event):
    kind=event['kind']
    if kind not in DISPLAY_EVENT_KINDS:return ''
    wall=event.get('wall_time_ns')
    clock=(datetime.fromtimestamp(wall/1e9).astimezone().strftime('%H:%M:%S.%f')[:-3]+' | ') if wall else ''
    head=f'[{clock}{relative_time(event.get("t_ns"))}] {event.get("action_id", "回放")}'
    target=event.get('planned_remaining_ms')
    if kind=='action_wait':
        return (f'\n{head} · {event.get("action_name", "")}\n'
                f'  计划倒计时 {game_time(target)} ｜ {event.get("mode_text", "定时释放")}')
    if kind=='cast_failure_diagnostic':
        text=(f'未确认施放的诊断：{event["reason_text"]}\n'
              f'  卡槽 {event.get("slot")}，预期 {event.get("expected_identity") or "未限定"}；'
              f'观测费用 {event.get("observed_cost_values",[])}，卡面 {event.get("observed_slot_identities",[])}\n'
              f'  证据文件：{"、".join(event.get("evidence_images",[])) or "未取得额外画面"}；'
              '诊断不等于已确定具体失败原因，不会自动重试。')
    elif kind=='auto_input_sent':
        text='已点击 AUTO 按钮，等待画面确认'+('开启' if event['auto'] else '关闭')
    elif kind=='auto_confirmed':
        text='AUTO 已确认'+('开启' if event['auto'] else '关闭')+('（本来已是目标状态，未点击）' if not event.get('input_sent') else '')
    elif kind=='transition_armed':
        text=f'已进入转场检测窗口：{game_time(event.get("remaining_ms"))}；HUD连续消失后开始连点'
    elif kind=='transition_finished':
        text='倒计时已恢复，结束转场并停止连点'
    elif kind=='strict_pickup_sent':
        text=f'已发送提牌，截图倒计时 {game_time(event.get("remaining_ms"))}，费用 {event.get("observed_ex_cost")} / 卡费 {event.get("card_cost")}'
    elif kind=='strict_pickup_confirmed':
        text=(f'画面确认提牌：倒计时 {game_time(event.get("remaining_ms"))}，'
              f'采集 {relative_time(event.get("capture_t_ns"))}，开始重新测量子弹时间')
    elif kind=='strict_pickup_clock_calibrated':
        text=f'计时校准：提牌前 {event.get("previous_game_rate",0):.4f}x → 提牌后 {event.get("game_rate",0):.4f}x（实测计时流速）'
    elif kind=='release_forecast':
        text=(f'定时计划：{event.get("cast_start_delay_ms",0):.3f}ms 后开始'
              f'{"目标点击" if event.get("input_kind")=="target_only" else "选卡与点击"}，'
              f'输入预算 {event.get("input_duration_ms",0):.3f}ms，期限 {relative_time(event.get("forecast_cast_start_t_ns"))}')
    elif kind=='planned':
        text=f'发送前截图：倒计时 {game_time(event.get("observed_remaining_ms"))}；计划 {game_time(target)}（截图读数不是实际释放时间）'
    elif kind=='chain_trigger':
        text=f'秒接 {event.get("previous_action_id")}：距上一条输入结束 {event.get("since_previous_input_ms")}ms，距确认 {event.get("since_previous_confirmation_ms")}ms'
    elif kind=='strict_early_wait':
        text=f'仍需等待：预计按下会提前 {event.get("predicted_press_early_ms",0):.3f}ms，提前容差 {event.get("early_tolerance_ms",0)}ms'
    elif kind=='strict_timing_check':
        text=(f'严格卡点检查：目标 {game_time(target)}\n'
              f'  预计按下 {game_time(event.get("predicted_press_remaining_ms"))}（{deviation(event.get("predicted_press_early_ms"))}）\n'
              f'  预计点击结束 {game_time(event.get("predicted_release_remaining_ms"))}（{deviation(event.get("deviation_ms"))}）\n'
              f'  提前上限 {event.get("early_tolerance_ms",0)}ms，延后上限 {event.get("late_tolerance_ms")}ms；以上均为预测')
    elif kind in ('input_sent','dry_run'):
        if kind=='dry_run':return f'{head} 演练完成，未发送输入'
        times=event.get('input_timing',{})
        text=(f'输入已发送：开始 {relative_time(event.get("input_start_t_ns"))} → 结束 {relative_time(event.get("input_end_t_ns"))}\n'
              f'  选卡按下 {relative_time(times.get("key_down_t_ns"))}，选卡松开 {relative_time(times.get("key_up_t_ns"))}\n'
              f'  目标按下 {relative_time(times.get("target_press_t_ns"))}，目标松开 {relative_time(times.get("target_release_t_ns"))}\n'
              f'  按发送前流速估算：按下时 {game_time(event.get("estimated_press_remaining_ms"))}，'
              f'松开时 {game_time(event.get("estimated_release_remaining_ms"))}（不是游戏实测时刻）')
    elif kind=='visually_supported':
        evidence={'cost_drop':'费用下降','new_skill_banner':'新技能名称条','card_rotation':'连续画面确认选定卡槽换牌'}.get(event.get('evidence'),'视觉变化')
        text=(f'画面确认成功：{evidence}\n'
              f'  证据帧采集 {relative_time(event.get("evidence_capture_t_ns"))}，'
              f'识别结束 {relative_time(event.get("recognition_end_t_ns",event.get("t_ns")))}\n'
              f'  证据帧倒计时 {game_time(event.get("observed_after_remaining_ms"))}，'
              f'输入结束后 {event.get("since_input_end_ms",0):.3f}ms 获得确认；不等同于精确释放时刻')
    else:text=f'结束：{event.get("reason_text",event.get("status", ""))}'
    return f'{head} {text}'
