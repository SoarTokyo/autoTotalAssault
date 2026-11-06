# SPDX-License-Identifier: GPL-3.0-or-later
"""按所选队伍加载本地特殊技能素材；图片与来源均保留在本地。"""
import hashlib
import json
from pathlib import Path
import cv2
import numpy as np
from .analysis_io import safe_media
from .analysis_team import normalize
from .paths import PROJECT_ROOT,portable_path
from .skill_library import validate_pack


def team_identity_options(students,root=None):
    """编辑器只读取所选学生和其特殊卡面元数据，不加载图像。"""
    root=Path(root or PROJECT_ROOT/'.local/special-skills/packs')
    selected={s['content_id']:s for s in students}
    by_name={normalize(s.get('recognition_name') or s['name']):s for s in students}
    values={s.get('recognition_name') or s['name'] for s in students}
    for path in root.glob('*/manifest.json'):
        data=json.loads(path.read_text(encoding='utf-8'))
        student=selected.get(data.get('content_id')) or next((by_name[normalize(n)] for n in data.get('actor_aliases',[]) if normalize(n) in by_name),None)
        if student is None:continue
        validate_pack(data,path.parent)
        actor=student.get('recognition_name') or student['name']
        values.update(actor+a.get('card_identity_suffix','') for a in data.get('assets',[]) if a['group']=='portraits')
    return sorted(values)


def load_packs(reader,students,root=None,*,allow_missing=False):
    root=Path(root or PROJECT_ROOT/'.local/special-skills/packs')
    selected={s['content_id']:s for s in students}
    by_name={normalize(s.get('recognition_name') or s['name']):s for s in students}
    loaded=[];seen=set();student_layouts={};loaded_students=set();warnings=[]
    for path in sorted(root.glob('*/manifest.json')):
        data=json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema_version')!='1.0':raise ValueError('特殊技能素材包版本不支持')
        student=selected.get(data.get('content_id'))
        if student is None:
            student=next((by_name[normalize(n)] for n in data.get('actor_aliases',[]) if normalize(n) in by_name),None)
        if student is None:continue
        validate_pack(data,path.parent)
        ident=student['content_id'];canonical=data.get('layout_version')=='2.0'
        if ident in student_layouts and (canonical or student_layouts[ident]):
            raise ValueError('同一学生存在多份活动素材包，请将旧版本移到 backups')
        student_layouts[ident]=canonical
        if data.get('reference_size')!=reader.profile['reference_size']:
            raise ValueError('特殊技能素材包的参考分辨率与视觉配置不一致：'+path.parent.name)
        actor=student.get('recognition_name') or student['name']
        decoded=[]
        try:
            for asset in data.get('assets',[]):
                # 完整卡、复制卡和其他 reference_only 图片只供人工查阅，不是运行依赖。
                if asset['group'] in ('cards','copy_cards') or asset.get('reference_only'):continue
                raw=safe_media(path.parent,asset['image']).read_bytes()
                if hashlib.sha256(raw).hexdigest()!=asset['sha256']:raise ValueError('特殊技能图片校验失败：'+asset['image'])
                image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_GRAYSCALE)
                if image is None:raise ValueError('特殊技能图片无法解码：'+asset['image'])
                decoded.append((asset,image))
        except FileNotFoundError as error:
            if not allow_missing:raise
            warnings.append(actor+' 的专用素材不完整，本次跳过该学生专包并继续分析：'+str(error.filename))
            continue
        for asset,image in decoded:
            if asset['group'] not in ('banners','portraits','tooltips','cards','copy_cards'):raise ValueError('特殊技能素材分组无效')
            entry=dict(asset,actor=actor,label=actor+'·'+asset['tag'])
            entry['student_content_id']=student['content_id']
            entry['card_identity']=actor+asset.get('card_identity_suffix','')
            if asset['group']=='tooltips':
                if not hasattr(reader,'context_templates'):reader.context_templates=[]
                reader.context_templates.append((entry,image));continue
            if asset['group']=='portraits':entry['label']=entry['card_identity']
            reader.templates[asset['group']].append((entry,image))
            reader.profile.setdefault(asset['group'],[]).append(entry)
        for rule in data.get('variants',[]):
            if rule['id'] in seen:raise ValueError('特殊技能规则 ID 重复')
            seen.add(rule['id'])
            spec=dict(rule,actor=actor,card_identity=actor+rule.get('card_identity_suffix',''),
                      banner_labels=[actor+'·'+tag for tag in rule['banner_tags']])
            if rule.get('required_context_tag'):spec['required_context_tag']=actor+'·'+rule['required_context_tag']
            reader.profile.setdefault('skill_variants',[]).append(spec)
        loaded.append(dict(pack=data['id'],revision=data.get('revision',1),manifest=portable_path(path),
                           sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        loaded_students.add(student['content_id'])
    reader.profile['special_skill_packs']=loaded
    if allow_missing:
        for plan_path in sorted((PROJECT_ROOT/'examples/special_skill_templates').glob('*.json')):
            plan=json.loads(plan_path.read_text(encoding='utf-8'));ident=plan.get('content_id')
            if plan.get('special_mechanics') and ident in selected and ident not in loaded_students:
                warnings.append((selected[ident].get('recognition_name') or selected[ident]['name'])+
                                ' 缺少可用特殊技能素材，普通图库识别继续，无法确认的阶段保留待核对。')
    reader.profile['special_skill_warnings']=warnings
    return loaded
