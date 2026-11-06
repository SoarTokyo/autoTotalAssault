# SPDX-License-Identifier: GPL-3.0-or-later
"""释放后的视觉证据；费用、对应横幅或连续卡槽换牌均可提供支持。"""


class CastVerifier:
    def __init__(self,action,before):
        self.action=action;self.before=before
        self.replacement=None;self.replacement_at=None

    def update(self,after,captured_at):
        before=self.before
        if (after['pause']['valid'] or not after['auto']['valid'] or after['auto']['value']!='off'
                or not before['auto']['valid'] or before['auto']['value']!='off'):
            self.replacement=None;self.replacement_at=None
            return None
        if before['cost']['valid'] and after['cost']['valid'] and after['cost']['value']<before['cost']['value']:
            return 'cost_drop'
        banner=after['banner'];old=before['banner'];entry=banner.get('entry') or {}
        identity=self.action.get('skill',{}).get('identity')
        matches=(banner.get('value')==self.action.get('notes') or
                 (identity and (entry.get('card_identity') or entry.get('actor'))==identity))
        if banner['valid'] and matches and (not old['valid'] or banner['value']!=old['value']):
            return 'new_skill_banner'
        slot=self.action['skill']['slot']
        pre=before['portraits'][slot];post=after['portraits'][slot]
        if (not identity or not pre['valid'] or pre['value']!=identity or not post['valid']
                or not post.get('value') or post['value']==identity):
            self.replacement=None;self.replacement_at=None
            return None
        # AUTO 已关闭，选定卡槽由预期角色变成另一身份，且两张连续新截图一致。
        if (self.replacement==post['value'] and self.replacement_at is not None
                and 0<captured_at-self.replacement_at<=1):return 'card_rotation'
        self.replacement=post['value'];self.replacement_at=captured_at
        return None
