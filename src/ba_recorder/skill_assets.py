# SPDX-License-Identifier: GPL-3.0-or-later
"""构建本地技能素材包；不修改源视觉配置、录制或现有素材包。"""
import hashlib
import json
from pathlib import Path
from .analysis_io import safe_media
from .analysis_team import normalize
from .skill_variants import variant_spec
from .paths import portable_path
from .skill_library import canonicalize,write_pack,checked_slug


def import_profile(profile_path,output):
    from .analysis_team import available_students
    from .student_names import resolve_name
    from .paths import PROJECT_ROOT
    import re
    profile_path=Path(profile_path);profile=json.loads(profile_path.read_text(encoding='utf-8'))
    actors={e['actor'] for e in profile.get('banners',[])}
    students=available_students()
    mapping_path=PROJECT_ROOT/'examples/special_skill_student_slugs.json'
    mapping=json.loads(mapping_path.read_text(encoding='utf-8')) if mapping_path.exists() else {}
    paths=[]
    for actor in sorted(actors):
        matches=[s for s in students if normalize(actor) in {normalize(s.get('name','')),normalize(s.get('recognition_name',''))}]
        if len(matches)>1:raise ValueError('既有配置中的学生身份不唯一：'+actor)
        student=matches[0] if matches else {}
        english=student.get('english_name') or resolve_name(actor)[0]
        ident=mapping.get(str(student.get('content_id'))) or re.sub(r'[^a-z0-9]+','-',english.lower()).strip('-')
        checked_slug(ident)
        root=Path(output)/ident
        if root.exists():continue
        data=dict(schema_version='1.0',id=ident,revision=1,actor_aliases=[actor],
                  reference_size=profile['reference_size'],assets=[],variants=[],
                  scope_note='由本机既有校准配置迁移；仅在所选队伍包含该角色时加载，未证明跨语言泛化。')
        if student:data['content_id']=student['content_id']
        images=[]
        for group in ('banners','portraits'):
            for entry in profile.get(group,[]):
                if group=='banners' and entry['actor']!=actor:continue
                if group=='portraits' and not entry['label'].startswith(actor):continue
                tag=entry['label'][len(actor):].lstrip('·') or 'EX启动'
                filename=f'{group}-{len(data["assets"])}.png'
                raw=safe_media(profile_path.resolve().parent,entry['image']).read_bytes()
                images.append(raw)
                suffix='·射击卡' if entry.get('phase')=='followup_shot' or entry['label'].endswith('·射击卡') else ''
                data['assets'].append(dict(entry,group=group,tag=tag,image=filename,
                    sha256=hashlib.sha256(raw).hexdigest(),card_identity_suffix=suffix,
                    source=dict(profile=portable_path(profile_path),image=entry['image'],frame=entry.get('source_frame'),roi=entry.get('source_roi'))))
                if group=='banners':
                    spec=variant_spec(entry,profile)
                    if spec:
                        spec.update(banner_tags=[tag],card_identity_suffix=suffix,
                                    card_rotation='optional',validation_status='legacy_calibrated')
                        data['variants'].append(spec)
        data,files=canonicalize(data,images,ident)
        paths.append(write_pack(root,data,files))
    return paths


def build_recorded_pack(spec_path,session,output):
    """显式标注帧 ID、裁剪框与阶段；不按动作序号生成识别答案。"""
    import cv2
    from .analysis_io import load_session,read_jsonl
    from .media import FrameSource
    spec=json.loads(Path(spec_path).read_text(encoding='utf-8'))
    session=Path(session);manifest,_=load_session(session)
    if spec['source_session_id']!=manifest['session_id']:raise ValueError('素材标注与录制会话不匹配')
    root=(Path(output)/spec.get('student_slug',spec['id'])).resolve()
    if not root.is_relative_to(Path(output).resolve()) or root==Path(output).resolve():
        raise ValueError('素材包输出路径越界')
    if root.resolve().is_relative_to(session.resolve()):raise ValueError('素材库不能写入原始录制目录')
    if root.exists():raise ValueError('素材包目录已存在，请使用新的素材包 ID 或版本目录')
    frames={f['id']:f for f in read_jsonl(safe_media(session,manifest['frames_path']))}
    encoded=[]
    with FrameSource(session) as source:
        for asset in sorted(spec['assets'],key=lambda a:frames[a['source_frame']]['t_ns']):
            image=cv2.resize(source.read(frames[asset['source_frame']]),tuple(spec['reference_size']))
            x,y,x2,y2=asset['source_roi']
            if not 0<=x<x2<=image.shape[1] or not 0<=y<y2<=image.shape[0]:raise ValueError('技能素材裁剪框无效')
            ok,raw=cv2.imencode('.png',image[y:y2,x:x2])
            if not ok:raise ValueError('技能素材编码失败')
            encoded.append((asset,raw.tobytes()))
    data=dict(spec,assets=[])
    files={}
    for n,(asset,raw) in enumerate(encoded):
        filename=f'{asset["group"]}-{n}.png';files[filename]=raw
        data['assets'].append(dict(asset,image=filename,sha256=hashlib.sha256(raw).hexdigest()))
    if spec.get('student_slug'):
        data,files=canonicalize(data,[raw for _,raw in encoded],checked_slug(spec['student_slug']))
    return write_pack(root,data,files)


def build_image_pack(spec_path,source_root,output):
    """从带哈希的外部截图标注构建素材；先裁视频有效区域，再归一化。"""
    import cv2
    import numpy as np
    import jsonschema
    from .paths import PROJECT_ROOT
    spec=json.loads(Path(spec_path).read_text(encoding='utf-8'))
    source_root=Path(source_root).resolve();output=Path(output).resolve()
    root=(output/spec.get('student_slug',spec['id'])).resolve()
    if not root.is_relative_to(output) or root==output or root.is_relative_to(source_root):
        raise ValueError('截图素材输出路径无效，不能写入源截图目录')
    if root.exists():raise ValueError('素材包目录已存在，不覆盖已有素材')
    images={};encoded=[]
    for asset in spec['assets']:
        source=asset['source_image']
        if source not in images:
            raw=safe_media(source_root,source).read_bytes()
            image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
            if image is None:raise ValueError('源截图无法解码：'+source)
            images[source]=(hashlib.sha256(raw).hexdigest(),image)
        digest,image=images[source]
        if digest!=asset['source_sha256']:raise ValueError('源截图校验失败：'+source)
        x,y,x2,y2=asset['source_viewport']
        if not 0<=x<x2<=image.shape[1] or not 0<=y<y2<=image.shape[0]:raise ValueError('截图有效画面范围无效')
        width,height=spec['reference_size']
        if abs((x2-x)/(y2-y)-width/height)>.015:raise ValueError('截图有效画面比例与参考尺寸不符')
        normalized=cv2.resize(image[y:y2,x:x2],(width,height))
        x,y,x2,y2=asset['source_roi']
        if not 0<=x<x2<=width or not 0<=y<y2<=height:raise ValueError('截图素材裁剪框无效')
        ok,raw=cv2.imencode('.png',normalized[y:y2,x:x2])
        if not ok:raise ValueError('截图素材编码失败')
        encoded.append((asset,raw.tobytes()))
    data=dict(spec,assets=[])
    for i,(asset,raw) in enumerate(encoded):
        data['assets'].append(dict(asset,image=f'{asset["group"]}-{i}.png',sha256=hashlib.sha256(raw).hexdigest()))
    jsonschema.validate(data,json.loads((PROJECT_ROOT/'schemas/special_skill_pack.schema.json').read_text(encoding='utf-8')))
    files={asset['image']:raw for asset,(_,raw) in zip(data['assets'],encoded)}
    if spec.get('student_slug'):
        data,files=canonicalize(data,[raw for _,raw in encoded],checked_slug(spec['student_slug']))
    return write_pack(root,data,files)
