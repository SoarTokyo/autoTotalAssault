# SPDX-License-Identifier: GPL-3.0-or-later
"""未确认施放的诊断摘要；区分观测事实与可能原因，不据此自动重试。"""


def failure_diagnostic(action,before,observations,selections,*,pickup_confirmed=False):
    slot=action['skill']['slot'];identity=action['skill'].get('identity')
    costs=[o['cost']['value'] for o in observations if o['cost']['valid']]
    cards=[o['portraits'][slot]['value'] for o in observations if o['portraits'][slot]['valid']]
    selected=[s['state'] for s in selections if s['state'].get('valid')]
    saw_selection=pickup_confirmed or any(s.get('value') is True for s in selected)
    changed=bool(identity and any(c!=identity for c in cards))
    interrupted=any(o['pause']['valid'] or (o['auto']['valid'] and o['auto']['value']!='off') for o in observations)
    if interrupted:
        code='state_interrupted';text='确认期间出现暂停或 AUTO 状态变化，无法可靠归因本次施放'
    elif selected and selected[-1].get('value') is True:
        code='selection_still_active';text='目标输入后技能牌仍被选中，可能目标点击未被接受；未确认施放'
    elif changed:
        code='unstable_card_change';text='曾观察到卡槽身份变化，但未形成稳定确认，需核对证据'
    elif saw_selection:
        code='selection_observed_unconfirmed';text='已观察到提牌，但目标点击后未确认施放；可能取消、目标无效或视觉证据不足'
    elif costs and cards and identity and all(c==identity for c in cards):
        code='no_cast_evidence';text='未观察到提牌成功或换牌；无法区分选卡未受理与目标点击无效'
    else:
        code='recognition_insufficient';text='施放后识别证据不足，不能确定技能未释放，也不能直接重试'
    return dict(reason_code=code,reason_text=text,expected_identity=identity,slot=slot+1,
                target=action.get('target'),pickup_confirmed_before_target=True if pickup_confirmed else None,
                selection_samples=selections,observed_cost_values=costs,observed_slot_identities=cards,
                before_cost=before['cost'],observation_count=len(observations))
