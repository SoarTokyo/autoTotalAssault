# SPDX-License-Identifier: GPL-3.0-or-later
"""固定 HUD 的本地字形/模板识别，无网络、无大模型调用。"""
import json

import cv2
import numpy as np

from .analysis_io import safe_media


def normalize_glyph(mask):
    points = cv2.findNonZero(mask)
    if points is None:
        return None
    x, y, w, h = cv2.boundingRect(points)
    glyph = mask[y:y+h, x:x+w]
    scale = min(22 / w, 32 / h)
    resized = cv2.resize(glyph, (max(1, round(w*scale)), max(1, round(h*scale))), interpolation=cv2.INTER_AREA)
    result = np.zeros((36, 26), np.float32)
    top, left = (36-resized.shape[0])//2, (26-resized.shape[1])//2
    result[top:top+resized.shape[0], left:left+resized.shape[1]] = resized / 255
    return result


def glyphs(image, font, threshold=None):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    _, mask = cv2.threshold(gray, 0 if threshold is None else threshold, 255,
                            cv2.THRESH_BINARY + (cv2.THRESH_OTSU if threshold is None else 0))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    components = []
    for ident in range(1, count):
        x, y, w, h, area = map(int, stats[ident])
        if font == 'timer':
            keep = 15 <= h <= 30 and 6 <= w <= 23 and area >= 65
        elif font == 'cost':
            keep = 15 <= h <= 44 and 5 <= w <= 40 and area >= 70
            # 费用数字在圆形HUD内部；同时贴住底边和侧边的亮块是框外背景，
            # 例如大型Boss的白色身体，不能作为第二个数字参与匹配。
            if y+h>=gray.shape[0]-1 and (x<=1 or x+w>=gray.shape[1]-1):keep=False
        else:
            keep = 17 <= h <= 29 and 5 <= w <= 24 and area >= 65
        if keep:
            component = np.where(labels[y:y+h, x:x+w] == ident, 255, 0).astype(np.uint8)
            components.append((x, normalize_glyph(component)))
    return [glyph for _, glyph in sorted(components, key=lambda pair: pair[0])]


class VisualReader:
    def __init__(self, profile_path, *, team=None,allow_missing_special=False):
        self.allow_missing_special=allow_missing_special
        self.root = profile_path.resolve().parent
        self.profile = json.loads(profile_path.read_text(encoding='utf-8'))
        if self.profile.get('schema_version') != '1.0':
            raise ValueError('不支持的视觉配置版本')
        self.width, self.height = self.profile['reference_size']
        self.bank = np.load(safe_media(self.root, self.profile['glyph_bank']), allow_pickle=False)
        from .hud_glyphs import load_glyphs
        self.extra_glyphs=load_glyphs(self)
        self.templates = {}
        self.stage_templates=[]
        for signal in ([] if team is not None else self.profile.get('stage_signals',[])):
            if signal['field'] not in ('target_mode','skill_mode','charge_level','shot_count'):
                raise ValueError('阶段信号名称不受支持，不能覆盖通用识别字段')
            templates=[]
            for entry in signal['templates']:
                template=cv2.imdecode(np.fromfile(safe_media(self.root,entry['image']),dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
                if template is None:raise ValueError('无法读取技能阶段模板')
                templates.append((entry['value'],template))
            self.stage_templates.append((signal,templates))
        for group in ('banners', 'portraits', 'pause', 'auto', 'speed', 'roster'):
            self.templates[group] = []
            if team is not None and group in ('banners','portraits','roster'):
                self.profile[group]=[]
                continue
            for entry in self.profile.get(group, []):
                image = cv2.imdecode(np.fromfile(safe_media(self.root, entry['image']), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
                if image is None:
                    raise ValueError('无法读取本地识别模板')
                self.templates[group].append((entry, image))
        if team is not None:
            self.prepare_team(team)

    def prepare_team(self,team):
        from .analysis_team import available_students,validate_team
        from .catalog_analysis import default_manifests
        from .catalog_cards import CatalogCardReader
        from .special_skills import load_packs
        students=available_students();team=validate_team(team,students)
        selected=[s for s in students if s['content_id'] in {v['content_id'] for v in team['students']}]
        self.selected_team=team
        self.profile['selected_team']=team
        self.catalog_reader=CatalogCardReader(default_manifests(),content_ids=[s['content_id'] for s in selected])
        self.catalog_cache={}
        self.catalog_fast=[]
        self.context_templates=[]
        self.profile['skill_variants']=[]
        for group in ('portraits','roster','banners'):
            self.templates[group]=[];self.profile[group]=[]
        load_packs(self,selected,allow_missing=getattr(self,'allow_missing_special',False))

    def skill_copy_state(self,image,slot):
        from .copy_card import copy_frame_state
        x,y,x2,_=self.profile['rois']['portrait'];width=x2-x
        shift=round(self.profile['slot_stride']*slot);x+=shift;x2+=shift
        y-=round(width*.08);height=round(width*1.36)
        offset=0
        if hasattr(self,'bank') and 'card_selected_cost' in self.profile['rois']:
            selected=self.selected_card(image,slot)
            if not selected['valid']:return dict(value=None,valid=False,score=0.,reason='card_position_unclear')
            offset=selected.get('offset',0) if selected['value'] else 0
        top=y+offset
        if x<0 or top<0 or x2>image.shape[1] or top+height>image.shape[0]:
            return dict(value=None,valid=False,score=0.,reason='copy_frame_outside_viewport')
        return dict(copy_frame_state(image[top:top+height,x:x2]),card_offset=offset)

    def observe_skill_card(self,image,slot):
        result=self.catalog_observation(self.crop(image,'portrait',slot))
        return dict(result,copy_state=self.skill_copy_state(image,slot))

    def catalog_observation(self,crop):
        import hashlib
        key=(crop.shape,hashlib.sha256(crop.tobytes()).digest())
        if key not in self.catalog_cache:
            gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY) if crop.ndim==3 else crop
            known={}
            for entry,template in getattr(self,'templates',{}).get('portraits',[]):
                if not entry.get('offline_identity'):continue
                if min(gray.shape)<2 or gray.shape[0]<template.shape[0] or gray.shape[1]<template.shape[1]:continue
                score=float(cv2.matchTemplate(gray,template,cv2.TM_CCOEFF_NORMED).max())
                if score>known.get(entry['actor'],(-1,None))[0]:known[entry['actor']]=(score,entry)
            ranked=sorted(known.values(),key=lambda v:v[0],reverse=True)
            if ranked and ranked[0][0]>=.95 and (len(ranked)==1 or ranked[0][0]-ranked[1][0]>=.03):
                score,entry=ranked[0]
                return dict(valid=True,value=entry['actor'],content_id=entry['student_content_id'],score=score,
                    inliers=0,inlier_gap=0,reason='verified_local_portrait',form_tag=entry['tag'],
                    card_identity=entry['card_identity'],evidence_source='special_skill_portrait',candidates=[])
            # 只复用已经通过图库几何验证的近乎相同卡面，不生成新身份。
            for previous,observation in reversed(self.catalog_fast):
                if previous.shape!=gray.shape:continue
                score=float(cv2.matchTemplate(gray,previous,cv2.TM_CCOEFF_NORMED)[0,0])
                if score>=.99:
                    return dict(observation,evidence_source='downloaded_catalog_verified_template',reference_match_score=score)
            result=self.catalog_reader.recognize(crop)
            best=result['candidates'][0] if result['candidates'] else {}
            self.catalog_cache[key]=dict(valid=result['valid'],value=result.get('name'),content_id=result['content_id'],
                score=best.get('inlier_ratio',0),inliers=best.get('inliers',0),inlier_gap=result.get('inlier_gap',0),
                reason=result['reason'],candidates=result['candidates'][:2],evidence_source='downloaded_catalog_sift')
            observation=self.catalog_cache[key]
            if observation['valid'] and observation['inliers']>=20 and observation['inlier_gap']>=8:
                self.catalog_fast.append((gray.copy(),dict(observation)))
                self.catalog_fast=self.catalog_fast[-48:]
        return dict(self.catalog_cache[key])

    def roster_crop(self,image,slot):
        explicit=self.profile.get('catalog_roster_rois')
        if explicit:
            x,y,x2,y2=explicit[slot]
        else:
            x,y,x2,y2=self.profile['roster_rois'][slot]
            w,h=x2-x,y2-y
            # 旧模板只取面部窄条；局部特征需要包含头发与完整脸部。
            x,y,x2,y2=x-round(w*.2857),y-round(h*.1064),x2+round(w*.3571),y2+round(h*1.064)
        return image[max(0,y):min(image.shape[0],y2),max(0,x):min(image.shape[1],x2)]

    def image(self, path):
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f'无法读取截图：{path.name}')
        return self.normalize_image(image)

    def prepare_catalog(self,identities):
        """复现前预加载所需身份，避免首次匹配在发送输入的循环中初始化。"""
        if hasattr(self,'selected_team'):
            from .analysis_team import normalize
            available={normalize(asset.get('local_name') or asset['name']) for asset,*_ in self.catalog_reader.references}
            available.update(normalize(entry['label']) for entry in self.profile.get('portraits',[]))
            missing={v for v in identities if v and normalize(v) not in available}
            if missing:raise ValueError('技能轴包含所选队伍与特殊技能库以外的卡牌身份：'+'、'.join(sorted(missing)))
            return
        known={entry['label'] for entry in self.profile.get('portraits',[])}
        unknown=set(identities)-known-{None,''}
        if not unknown:return
        from .catalog_analysis import default_manifests
        from .catalog_cards import CatalogCardReader
        paths=default_manifests()
        if not paths:raise ValueError('技能轴包含视觉配置外的角色，请先下载学生图库以进行身份校验')
        cv2.setNumThreads(1)
        self.catalog_reader=CatalogCardReader(paths,names=unknown|known)
        available={asset.get('local_name') or asset['name'] for asset,*_ in self.catalog_reader.references}
        missing=unknown-available
        if missing:raise ValueError('本地图库缺少技能轴中的身份：'+'、'.join(sorted(missing)))

    def normalize_image(self,image):
        h, w = image.shape[:2]
        if abs(w/h - self.width/self.height) > .015:
            raise ValueError('截图宽高比与视觉校准不匹配，需要重新校准')
        return cv2.resize(image, (self.width, self.height)) if (w, h) != (self.width, self.height) else image

    def crop(self, image, name, slot=0):
        x1, y1, x2, y2 = self.profile['rois'][name]
        if name in ('card_cost', 'card_selected_cost', 'portrait'):
            shift = round(self.profile['slot_stride'] * slot)
            x1, x2 = x1+shift, x2+shift
        return image[y1:y2, x1:x2]

    def number(self, image, font, slot=0):
        if font == 'card':
            results = [self.decode_number(self.crop(image, name, slot), font)
                       for name in ('card_cost', 'card_selected_cost')]
            valid = sorted([r for r in results if r['valid']], key=lambda r:r['score'], reverse=True)
            if valid:
                return valid[0]
            return max(results, key=lambda r:r['score'])
        return self.decode_number(self.crop(image, font, slot), font)

    def decode_number(self, crop, font):
        original=self._decode_number(crop,font)
        if original['valid']:return original
        # 缩放和暗化会粘连数字。仅在原识别失败时尝试不同二值化阈值，
        # 保持字形分数/歧义门槛不变，并要求至少两个阈值独立得到同一结果。
        gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY) if crop.ndim==3 else crop
        baseline,_=cv2.threshold(gray,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)
        candidates=[self._decode_number(crop,font,max(1,min(254,baseline+offset)))
                    for offset in (-5,5,10,15,20,25,30)]
        if font=='card':
            # 选卡后的亮蓝圆底可能让卡费呈现暗字，不能沿用白字二值化极性。
            inverted=255-crop
            inverse_gray=255-gray
            inverse_baseline,_=cv2.threshold(inverse_gray,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)
            candidates.extend(self._decode_number(inverted,font,max(1,min(254,inverse_baseline+offset)))
                              for offset in (-10,0,10,15))
        valid=[r for r in candidates if r['valid']]
        if len(valid)>=2 and len({r['value'] for r in valid})==1:
            return dict(max(valid,key=lambda r:r['score']),method='threshold_consensus',votes=len(valid))
        if font=='timer' and not valid:
            from .timer_enhancement import enhanced_timer
            enhanced=enhanced_timer(gray,self._decode_number)
            if enhanced is not None:return enhanced
        return original

    def _decode_number(self,crop,font,threshold=None):
        components = glyphs(crop, font, threshold)
        required = 7 if font == 'timer' else None
        if not components or (required is not None and len(components) != required) or len(components) > (7 if font == 'timer' else 2):
            return {'value': None, 'score': 0, 'valid': False}
        values, confidences = [], []
        for glyph in components:
            scores = {}
            extra=getattr(self,'extra_glyphs',{})
            for key in list(self.bank.files)+list(extra):
                if key.startswith(font + '_'):
                    digit = key.split('_')[1]
                    template = extra[key] if key in extra else self.bank[key]
                    score = float(2 * np.minimum(glyph, template).sum() / max(.001, (glyph+template).sum()))
                    scores[digit] = max(scores.get(digit, 0), score)
            ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
            if not ranked or ranked[0][1] < .75 or (len(ranked) > 1 and ranked[0][1] - ranked[1][1] < .025):
                return {'value': None, 'score': ranked[0][1] if ranked else 0, 'valid': False}
            values.append(ranked[0][0])
            confidences.append(ranked[0][1])
        text = ''.join(values)
        if font == 'timer':
            if int(text[2:4]) >= 60:
                return {'value': None, 'score': min(confidences), 'valid': False}
            value = text[:2] + ':' + text[2:4] + '.' + text[4:]
        else:
            value = int(text)
            if value > 11:
                return {'value': None, 'score': min(confidences), 'valid': False}
        return {'value': value, 'score': min(confidences), 'valid': True}

    def match(self, image, group, slot=0):
        if group=='roster':
            x,y,x2,y2=self.profile['roster_rois'][slot]
            roi=image[y:y2,x:x2]
        else:
            roi = self.crop(image, {'banners': 'banner', 'portraits': 'portrait', 'pause': 'pause', 'auto':'auto', 'speed':'speed'}[group], slot)
        if hasattr(self,'selected_team') and group=='roster':return self.catalog_observation(self.roster_crop(image,slot))
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        scores = {}
        records = {}
        locations = {}
        for entry, template in self.templates[group]:
            if gray.shape[0] < template.shape[0] or gray.shape[1] < template.shape[1]:
                continue
            result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
            finite = np.where(np.isfinite(result), result, -1)
            score = float(finite.max())
            if group == 'auto' and entry['label'] == 'off':
                # AUTO 的字样开关时相同，不能只用灰度相关性判定关闭。
                y,x=np.unravel_index(int(finite.argmax()),finite.shape)
                color=roi[y:y+template.shape[0],x:x+template.shape[1]]
                saturation=float(np.median(cv2.cvtColor(color,cv2.COLOR_BGR2HSV)[:,:,1]))
                if saturation > self.profile.get('max_auto_off_saturation',50):
                    score=-1
            label = entry['label']
            if score > scores.get(label, -1):
                scores[label], records[label] = score, entry
                if group=='auto':
                    yy,xx=np.unravel_index(int(finite.argmax()),finite.shape)
                    ax,ay,_,_=self.profile['rois']['auto']
                    locations[label]=[(ax+xx+template.shape[1]/2)/image.shape[1],(ay+yy+template.shape[0]/2)/image.shape[0]]
        ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
        label,score=ranked[0] if ranked else (None,0)
        threshold = self.profile.get('thresholds', {}).get(group, .85)
        valid = bool(ranked and score >= threshold and (len(ranked) == 1 or score-ranked[1][1] >= .025))
        if group=='auto' and not valid:
            # 旧配置通常只有关闭模板；开启时字形相同，但按钮为黄色。
            hsv=cv2.cvtColor(roi,cv2.COLOR_BGR2HSV)
            yellow=float(((hsv[:,:,0]>=15)&(hsv[:,:,0]<=40)&(hsv[:,:,1]>120)&(hsv[:,:,2]>140)).mean())
            for entry,template in self.templates[group]:
                if entry['label']!='off' or any(a<b for a,b in zip(gray.shape,template.shape)):continue
                similarity=float(cv2.matchTemplate(gray,template,cv2.TM_CCOEFF_NORMED).max())
                if yellow>.30 and similarity>=threshold:
                    match=cv2.matchTemplate(gray,template,cv2.TM_CCOEFF_NORMED)
                    yy,xx=np.unravel_index(int(match.argmax()),match.shape)
                    ax,ay,_,_=self.profile['rois']['auto']
                    return dict(value='on',valid=True,score=similarity,entry=None,method='auto_yellow_and_glyph',
                                button_point=[(ax+xx+template.shape[1]/2)/image.shape[1],(ay+yy+template.shape[0]/2)/image.shape[0]])
        if group=='portraits' and not valid and hasattr(self,'catalog_reader'):
            if hasattr(self,'selected_team'):return self.catalog_observation(roi)
            result=self.catalog_reader.recognize(self.crop(image,'portrait',slot))
            if result['valid']:
                return dict(value=result['name'],valid=True,score=result['candidates'][0]['inlier_ratio'],
                            evidence_source='downloaded_catalog_sift')
        result={'value': label if valid else None, 'score': score, 'valid': valid,
                'entry': records[label] if valid else None}
        if group=='auto' and valid:result['button_point']=locations[label]
        return result

    def frame(self, path, *, include_portraits=False):
        image = self.image(path)
        return self.features(image,include_portraits=include_portraits)

    def selected_card(self,image,slot):
        """提牌后卡费上移16或32像素，并出现技能说明面板；双证据确认。"""
        x,y,x2,y2=self.profile['rois']['card_cost']
        shift=round(self.profile['slot_stride']*slot)
        x,x2=x+shift,x2+shift
        normal=self.decode_number(image[y:y2,x:x2],'card')
        h,w=image.shape[:2]
        panel=cv2.cvtColor(image[round(h*.035):round(h*.12),round(w*.04):round(w*.4)],cv2.COLOR_BGR2GRAY)
        panel_visible=float(np.mean(panel>160))>.55
        raised=[dict(self.decode_number(image[y-d:y2-d,x:x2],'card'),offset=-d) for d in (16,32)]
        candidates=[r for r in raised if r['valid'] and r['score']>=.85]
        if panel_visible and not normal['valid'] and candidates:
            best=max(candidates,key=lambda r:r['score'])
            return dict(valid=True,value=True,score=best['score'],cost=best['value'],offset=best['offset'])
        return dict(valid=normal['valid'] or not panel_visible,value=False,score=normal['score'],cost=None)

    def read_stage_signals(self,image):
        result={}
        for signal,templates in self.stage_templates:
            x,y,x2,y2=signal['roi']
            gray=cv2.cvtColor(image[y:y2,x:x2],cv2.COLOR_BGR2GRAY)
            scores={}
            for value,template in templates:
                if gray.shape[0]<template.shape[0] or gray.shape[1]<template.shape[1]:continue
                raw=cv2.matchTemplate(gray,template,cv2.TM_CCOEFF_NORMED)
                score=float(np.where(np.isfinite(raw),raw,-1).max())
                scores[value]=max(scores.get(value,-1),score)
            ranked=sorted(scores.items(),key=lambda p:p[1],reverse=True)
            valid=bool(ranked and ranked[0][1]>=signal.get('threshold',.92) and
                       (len(ranked)==1 or ranked[0][1]-ranked[1][1]>=.025))
            result[signal['field']]=dict(valid=valid,value=ranked[0][0] if valid else None,
                                        score=ranked[0][1] if ranked else 0)
        return result

    def read_special_contexts(self,image):
        result={}
        for entry,template in getattr(self,'context_templates',[]):
            x,y,x2,y2=entry.get('search_roi',[0,0,min(1000,image.shape[1]),min(230,image.shape[0])])
            roi=cv2.cvtColor(image[y:y2,x:x2],cv2.COLOR_BGR2GRAY)
            if roi.shape[0]<template.shape[0] or roi.shape[1]<template.shape[1]:continue
            score=float(cv2.matchTemplate(roi,template,cv2.TM_CCOEFF_NORMED).max())
            result[entry['label']]=dict(valid=score>=entry.get('threshold',.94),score=score)
        return result

    def hand_card(self,image,slot):
        """离线手牌轮转用边缘模板，减弱费用不足的半卡明暗遮罩影响；不用于放行输入。"""
        if hasattr(self,'selected_team'):
            return self.observe_skill_card(image,slot)
        if not hasattr(self,'hand_templates'):
            self.hand_templates=[]
            for entry,template in self.templates['portraits']:
                edges=cv2.Canny(template,40,100)
                if cv2.countNonZero(edges)>=12:self.hand_templates.append((entry,edges))
        roi=cv2.Canny(cv2.cvtColor(self.crop(image,'portrait',slot),cv2.COLOR_BGR2GRAY),40,100)
        if cv2.countNonZero(roi)<12:return dict(value=None,valid=False,score=0,evidence_source='offline_card_edges')
        scores={}
        for entry,template in self.hand_templates:
            if roi.shape[0]<template.shape[0] or roi.shape[1]<template.shape[1]:continue
            raw=cv2.matchTemplate(roi,template,cv2.TM_CCOEFF_NORMED)
            score=float(np.where(np.isfinite(raw),raw,-1).max())
            label=entry['label']
            scores[label]=max(scores.get(label,-1),score)
        ranked=sorted(scores.items(),key=lambda pair:pair[1],reverse=True)
        valid=bool(ranked and ranked[0][1]>=self.profile.get('hand_edge_threshold',.65) and
                   (len(ranked)==1 or ranked[0][1]-ranked[1][1]>=.08))
        return dict(value=ranked[0][0] if valid else None,valid=valid,score=ranked[0][1] if ranked else 0,
                    evidence_source='offline_card_edges')

    def features(self, image, *, include_portraits=False, include_selection=False, include_readiness=False, include_roster=False, include_stage_signals=False, include_hand=False, focus_slot=None):
        result = {'timer': self.number(image, 'timer'), 'cost': self.number(image, 'cost'),
                'card_costs': [self.number(image, 'card', slot) if focus_slot is None or slot==focus_slot else
                               {'value':None,'valid':False,'score':0} for slot in range(3)],
                'portraits': ([self.match(image, 'portraits', slot) if focus_slot is None or slot==focus_slot else
                               {'value':None,'valid':False,'score':0} for slot in range(3)] if include_portraits else
                              [{'value':None,'valid':False,'score':0} for _ in range(3)]),
                'banner': self.match(image, 'banners'), 'pause': self.match(image, 'pause'),
                'auto': self.match(image, 'auto'), 'speed': self.match(image, 'speed')}
        from .transition import screen_signals
        result.update(screen_signals(image))
        if include_stage_signals and result['skill_banner_visible']['valid'] and hasattr(self,'catalog_reader'):
            h,w=image.shape[:2]
            found=self.catalog_reader.recognize(image[round(h*.122):round(h*.250),:round(w*.133)])
            result['auto_banner']=dict(valid=found['valid'],value=found.get('name'),
                                     score=found.get('candidates',[{}])[0].get('inlier_ratio',0) if found.get('candidates') else 0)
        if include_selection:
            result['selected_cards']=[self.selected_card(image,slot) for slot in range(3)]
        if include_roster:
            result['roster']=([self.match(image,'roster',slot) for slot in range(6)]
                              if result['pause']['valid'] and len(self.profile.get('roster_rois',[]))==6 else [])
        if include_stage_signals:
            result.update(self.read_stage_signals(image))
            if getattr(self,'context_templates',[]):result['special_contexts']=self.read_special_contexts(image)
        if include_hand:result['hand_cards']=[self.hand_card(image,slot) for slot in range(3)]
        if include_readiness:
            from .card_state import shooting_card_state
            result['card_readiness']=[shooting_card_state(self.crop(image,'portrait',slot)) for slot in range(3)]
        return result
