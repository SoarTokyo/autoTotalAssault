# SPDX-License-Identifier: GPL-3.0-or-later
"""单键映射的显示、规范化和冲突检查。"""
from dataclasses import replace

ACTION_LABELS={
    'select_slot_0':'选择技能卡 1','select_slot_1':'选择技能卡 2','select_slot_2':'选择技能卡 3',
    'toggle_auto':'切换 AUTO','cycle_speed':'切换倍速','context_escape':'取消技能 / 菜单（共用键）',
    'cancel_skill':'单独取消技能（可选）','menu_toggle':'单独打开 / 关闭菜单（可选）',
}
OPTIONAL_ACTIONS={'context_escape','cancel_skill','menu_toggle'}
SPECIAL_VK={'esc':27,'space':32,'tab':9,'enter':13,'backspace':8,'delete':46,'insert':45,
            'home':36,'end':35,'page_up':33,'page_down':34,'left':37,'up':38,'right':39,'down':40}
SPECIAL_LABELS={'esc':'Esc','space':'空格','tab':'Tab','enter':'Enter','backspace':'Backspace',
                'delete':'Delete','insert':'Insert','home':'Home','end':'End','page_up':'Page Up',
                'page_down':'Page Down','left':'←','up':'↑','right':'→','down':'↓'}
REPLAY_CONTROL_KEYS={'f8','f9'}


def virtual_key(value):
    if value in SPECIAL_VK:return SPECIAL_VK[value]
    if value.startswith('f') and value[1:].isdigit() and 1<=int(value[1:])<=24:return 111+int(value[1:])
    if value.startswith('vk:') and value[3:].isdigit() and 1<=int(value[3:])<=255:return int(value[3:])
    raise ValueError('请选择支持的单个按键，不支持组合键')


def canonical_key(value):
    vk=virtual_key(value)
    if 112<=vk<=135:return f'f{vk-111}'
    for key,code in SPECIAL_VK.items():
        if vk==code:return key
    return f'vk:{vk}'


def key_choices():
    return ([(str(i),f'vk:{48+i}') for i in range(10)] +
            [(chr(i),f'vk:{i}') for i in range(65,91)] +
            [(f'F{i}',f'f{i}') for i in range(1,13)] +list((label,key) for key,label in SPECIAL_LABELS.items()) +
            [(label,f'vk:{code}') for label,code in [('; / :',186),('= / +',187),(', / <',188),('- / _',189),
                ('. / >',190),('/ / ?',191),('` / ~',192),('[ / {',219),('\\ / |',220),('] / }',221),("' / \"",222)]] +
            [(f'小键盘 {i}',f'vk:{96+i}') for i in range(10)])


def validate_bindings(config):
    from .analysis_profile import AnalysisProfile
    mapping={action:canonical_key(key) for action,key in config.keymap.items()}
    required=set(ACTION_LABELS)-OPTIONAL_ACTIONS
    if not required.issubset(mapping):raise ValueError('请设置三张选卡键、AUTO 键和倍速键')
    AnalysisProfile(keymap=mapping)  # 包含可选上下文动作；重复键不能静默覆盖。
    record_controls={config.start_key,config.stop_key,config.abort_key}
    if 'f10' in record_controls:raise ValueError('F10 是游戏退出键，请为录制控制选择其他按键')
    for action,key in mapping.items():
        label=ACTION_LABELS.get(action,action)
        if key in record_controls:raise ValueError(f'{label} 与录制热键冲突，请选择不同按键')
        if key in REPLAY_CONTROL_KEYS:raise ValueError(f'{label} 与回放固定热键 F8/F9 冲突')
    allowed=list(config.allowed_keys)
    if allowed:
        normalized=[]
        for key in allowed:
            try:normalized.append(canonical_key(key))
            except ValueError:normalized.append(key)
        allowed=list(dict.fromkeys(normalized+list(mapping.values())))
    return replace(config,keymap=mapping,allowed_keys=allowed)
