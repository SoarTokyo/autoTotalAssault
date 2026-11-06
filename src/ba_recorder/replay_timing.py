# SPDX-License-Identifier: GPL-3.0-or-later
"""回放的短期视觉记忆与基于采集时间的延迟预算。"""
import copy
from collections import deque

from .replay_script import timer_ms


class TimingTracker:
    def __init__(self, *, slow_time=False):
        self.slow_time=slow_time
        self.previous = None
        self.rate = 1.0
        self.samples=deque(maxlen=48 if slow_time else 8)
        self.last_advance=None
        self.known_speed=None
        self.advanced_sample=None

    def update(self, observation, captured_at):
        timer = observation['timer']
        speed=observation.get('speed',{})
        changed_speed=(speed.get('valid') and self.known_speed is not None and speed['value']!=self.known_speed)
        if speed.get('valid'):self.known_speed=speed['value']
        if observation.get('pause',{}).get('valid') or changed_speed:
            self.previous=None
            self.samples.clear()
            self.last_advance=None
            self.advanced_sample=None
            self.rate=1.0
            return False
        if not timer['valid']:
            self.previous = None
            self.samples.clear()
            self.last_advance=None
            self.advanced_sample=None
            return False
        current = timer_ms(timer['value'])
        previous = self.previous
        # Windows的时钟读数可能在高频采集中重复；跳过本次采样，不能清空已校准时钟。
        if previous is not None and captured_at==previous[1]:
            return False
        self.previous = current, captured_at
        if previous is None:
            self.samples.append((current,captured_at))
            return False
        elapsed = captured_at - previous[1]
        drop = previous[0] - current
        if not 0 < elapsed <= .5 or not 0 <= drop <= elapsed * 6000 + 100:
            self.samples.clear()
            self.samples.append((current,captured_at))
            self.last_advance=None
            self.advanced_sample=None
            return False
        if drop > 0:
            measured = drop / elapsed / 1000
            if (.01 if self.slow_time else .1) <= measured <= 6:
                # 明显变速时清除旧拟合，不沿用提牌前或变速前的预测。
                if not self.slow_time and len(self.samples)>=3 and abs(measured-self.rate)>max(.6,self.rate*.5):
                    self.samples.clear()
                    self.samples.append(previous)
                self.last_advance=captured_at
                self.advanced_sample=(current,captured_at)
                self.rate = measured
        self.samples.append((current,captured_at))
        if self.slow_time:
            while len(self.samples)>2 and captured_at-self.samples[0][1]>.8:self.samples.popleft()
        if len(self.samples)>=3:
            first,last=self.samples[0],self.samples[-1]
            if last[1]-first[1]>=(.15 if self.slow_time else .06):
                self.rate=(first[0]-last[0])/(last[1]-first[1])/1000
        return True

    def lead_ms(self, age_seconds, input_lead_ms):
        return max(0, age_seconds * 1000 + input_lead_ms) * self.rate

    def predict_start_at(self,target_remaining_ms,now,*,input_duration_ms=130,horizon_ms=80):
        """预测整次选卡和点击的开始时刻，使释放尽量落在目标倒计时。"""
        if not self.reliable(now):
            return None
        remaining,captured_at=self.advanced_sample if self.slow_time else self.previous
        at=captured_at+(remaining-target_remaining_ms)/self.rate/1000-input_duration_ms/1000
        return at if 0<=at-now<=horizon_ms/1000 else None

    def reliable(self,now):
        return (len(self.samples)>=3 and self.previous is not None and self.rate>(.01 if self.slow_time else .05) and
                (not self.slow_time or self.samples[-1][1]-self.samples[0][1]>=.15) and
                self.last_advance is not None and 0<=now-self.last_advance<=(.5 if self.slow_time else .15))

    def estimate_remaining_at(self,at):
        sample=self.advanced_sample if self.slow_time else self.previous
        return None if sample is None else sample[0]-(at-sample[1])*self.rate*1000


class CardIdentityMemory:
    """只在同一待执行动作内使用；有新身份冲突、卡费变化或过期立即失效。"""
    def __init__(self, action, max_age_seconds=5, max_game_age_ms=6000):
        self.action = action
        self.max_age = max_age_seconds
        self.max_game_age = max_game_age_ms
        self.candidate = None
        self.evidence = None

    def apply(self, observation, now):
        result = copy.deepcopy(observation)
        if self.action['kind'] != 'cast_skill' or not observation['timer']['valid']:
            self.evidence = self.candidate = None
            return result
        slot = self.action['skill']['slot']
        expected = self.action['skill'].get('identity')
        portrait = observation['portraits'][slot]
        remaining = timer_ms(observation['timer']['value'])
        card_costs = observation.get('card_costs', [])
        cost = card_costs[slot]['value'] if card_costs and card_costs[slot]['valid'] else None
        if observation['pause']['valid'] or (portrait['valid'] and portrait['value'] != expected):
            self.evidence = self.candidate = None
            return result
        if self.evidence:
            old = self.evidence
            if (not 0 <= now-old['at'] <= self.max_age or
                not 0 <= old['remaining']-remaining <= self.max_game_age or
                (cost is not None and old['cost'] is not None and cost != old['cost'])):
                self.evidence = self.candidate = None
        if portrait['valid'] and portrait['value'] == expected:
            if self.candidate is not None and 0 < now-self.candidate <= .5:
                self.evidence = dict(at=now, remaining=remaining, cost=cost, portrait=copy.deepcopy(portrait))
            self.candidate = now
        else:
            self.candidate = None
        if not portrait['valid'] and self.evidence:
            result['portraits'][slot] = dict(self.evidence['portrait'],
                evidence_source='recent_same_action', evidence_age_ms=round((now-self.evidence['at'])*1000))
        return result
