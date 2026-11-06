# SPDX-License-Identifier: GPL-3.0-or-later
"""技能阶段的声明式证据规则；不按角色名字猜测阶段。"""


def variant_spec(entry,profile,visual=None):
    matches=[s for s in profile.get('skill_variants',[])
             if entry['label'] in s.get('banner_labels',[]) and s['actor']==entry['actor']]
    if len(matches)>1 and visual is not None:
        matches=[s for s in matches if (s.get('required_visual_fields') or s.get('required_context_tag')) and all(
            visual.get(field,{}).get('valid') and visual[field].get('value')==value
            for field,value in s.get('required_visual_fields',{}).items()) and
            (not s.get('required_context_tag') or visual.get('special_contexts',{}).get(s['required_context_tag'],{}).get('valid'))]
        return matches[0] if len(matches)==1 else None
    if len(matches)==1:return matches[0]
    if matches:return None
    # 已验证旧配置的兼容规则。其他阶段必须显式配置，不套用零费射击规则。
    phase=entry.get('phase')
    if phase not in ('ex_activation','followup_shot'):return None
    return dict(id=entry.get('variant_id',entry['label']),actor=entry['actor'],phase=phase,
                card_identity=entry.get('card_identity',entry['actor']+('·射击卡' if phase=='followup_shot' else '')),
                cost_kind='zero' if phase=='followup_shot' else 'positive',
                requires_cost_drop=phase=='ex_activation',required_visual_fields={})


def variant_cost_matches(spec,card_cost):
    if spec is None or not card_cost.get('valid'):return False
    value=card_cost['value']
    rule=spec.get('cost_kind')
    return ((rule=='zero' and value==0) or (rule=='positive' and value>0) or
            (rule=='any' and value>=0))


def variant_confirmed(spec,card_cost,before,after,cost_drop,*,catalog=None):
    if not variant_cost_matches(spec,card_cost):return False
    if spec.get('requires_cost_drop',True) and not cost_drop:return False
    if spec.get('required_context_tag') and not before.get('special_contexts',{}).get(spec['required_context_tag'],{}).get('valid'):return False
    if spec.get('card_rotation')=='required' and not (catalog or {}).get('rotation'):return False
    if spec.get('requires_selected_actor'):
        from .analysis_team import normalize
        identity=(catalog or {}).get('identity')
        if not identity or normalize(identity)!=normalize(spec['actor']):return False
    for field,value in spec.get('required_visual_fields',{}).items():
        # 配置可要求目标类型、形态、蓄力等独立视觉信号；缺失时不推测。
        actual=after.get(field,{})
        if not actual.get('valid') or actual.get('value')!=value:return False
    return before['auto']['valid'] and before['auto']['value']=='off'
