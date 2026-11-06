# SPDX-License-Identifier: GPL-3.0-or-later
"""转场的界面几何信号与可离线测试的点击状态机，不依赖美术素材。"""
import time


def screen_signals(image):
    h,w=image.shape[:2]
    def crop(x,y,x2,y2):return image[round(y*h):round(y2*h),round(x*w):round(x2*w)]
    title=crop(.09,.24,.27,.28)
    white=((title.min(2)>185)&(title.max(2)-title.min(2)<55)).mean()
    strip=crop(.09,.285,.25,.293)
    cyan=((strip[:,:,0]>150)&(strip[:,:,1]>100)&(strip[:,:,0].astype(float)-strip[:,:,2]>35)).mean()
    panel=crop(.30,.30,.69,.60)
    panel_white=((panel.min(2)>180)&(panel.max(2)-panel.min(2)<60)).mean()
    button=crop(.52,.66,.68,.73)
    blue=((button[:,:,0]>160)&(button[:,:,1]>120)&(button[:,:,0].astype(float)-button[:,:,2]>40)).mean()
    return dict(skill_banner_visible=dict(valid=bool(white>.65 and cyan>.35),score=float(min(white,cyan))),
                transition_confirm=dict(valid=bool(panel_white>.85 and blue>.40),score=float(min(panel_white,blue))))


def hud_absent(observation):
    return (not observation['timer']['valid'] and not observation['pause']['valid']
            and not any(v['valid'] for v in observation['card_costs']))


class TransitionGate:
    """倒计时首次恢复立即禁止点击，连续恢复才完成；暂停与可见HUD均禁止点击。"""
    def __init__(self,missing_frames=3,recovery_frames=2):
        self.missing_frames=missing_frames;self.recovery_frames=recovery_frames
        self.missing=0;self.restored=0;self.entered=False

    def update(self,obs):
        if obs['timer']['valid']:
            self.missing=0
            self.restored+=1
            return 'done' if self.entered and self.restored>=self.recovery_frames else 'wait'
        self.restored=0
        if not hud_absent(obs):
            self.missing=0
            return 'wait'
        self.missing+=1
        if self.missing>=self.missing_frames:
            self.entered=True
            return 'click'
        return 'wait'


def replay_transition(action,capture,click,check,wait,emit,clock=time.monotonic):
    gate=TransitionGate()
    deadline=clock()+action['trigger']['timeout_ms']/1000
    armed=False
    clicks=0
    while clock()<deadline:
        check()
        obs=capture()
        if not armed:
            if obs['timer']['valid']:
                from .replay_script import timer_ms
                remaining=timer_ms(obs['timer']['value'])
                if remaining>action['trigger']['remaining_ms']:
                    wait(.05);continue
                if remaining<action['trigger']['remaining_ms']-action.get('transition',{}).get('window_ms',15000):
                    raise RuntimeError('已越过指定转场时间窗口，未开始连点')
                armed=True
                emit('transition_armed',action_id=action['id'],remaining_ms=remaining)
            else:
                # 没有看过时间窗口，不能因为启动时黑屏直接点击。
                wait(.05);continue
        state=gate.update(obs)
        if state=='done':
            emit('transition_finished',action_id=action['id'],click_count=clicks)
            return
        if state=='click':
            click();clicks+=1
            emit('transition_click',action_id=action['id'],click_count=clicks)
        wait(.10)
    raise RuntimeError('转场等待或跳过超时，已停止复现')
