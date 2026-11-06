# SPDX-License-Identifier: GPL-3.0-or-later
"""特殊技能库的目录规范、离线校验、截图登记与可回滚迁移。"""
from collections import Counter
import copy
from datetime import datetime
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import tempfile
from uuid import uuid4

import cv2
import jsonschema
import numpy as np

from .analysis_io import safe_media
from .paths import PROJECT_ROOT
from .storage import write_json
from .student_names import normalize

DEFAULT_ROOT = PROJECT_ROOT / '.local/special-skills/packs'
GROUPS = ('banners', 'portraits', 'tooltips', 'cards', 'copy_cards')


def checked_slug(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', value):
        raise ValueError('目录或素材键必须使用小写英文字母、数字和单个连字符')
    if value in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}:
        raise ValueError('目录或素材键不能使用 Windows 保留名称')
    return value


def asset_key(asset, variants=()):
    if asset.get('asset_key'):
        return checked_slug(asset['asset_key'])
    group, tag = asset['group'], asset['tag']
    if group == 'banners':
        phases = {v['phase'] for v in variants if tag in v['banner_tags']}
        phase = next(iter(phases)) if len(phases) == 1 else asset.get('phase', 'shared-skill')
        return checked_slug(phase.replace('_', '-'))
    if group == 'tooltips':
        return 'skill-context'
    if group == 'copy_cards':
        return 'copied-skill'
    for word, key in [('射击', 'shooting-card'), ('装甲', 'armor-card'), ('花苞', 'bud-card'),
                      ('双人', 'linked-card'), ('搭乘', 'linked-card'), ('基础', 'base-card'),
                      ('普通', 'base-card'), ('EX启动', 'base-card')]:
        if word in tag:
            return key
    return 'form-card'


def asset_path(asset, number, variants=()):
    if asset['group'] not in GROUPS:
        raise ValueError('特殊技能素材分组无效')
    return f"{asset['group']}/{asset_key(asset, variants)}-{number:03d}.png"


@lru_cache(maxsize=1)
def pack_validator():
    schema=json.loads((PROJECT_ROOT/'schemas/special_skill_pack.schema.json').read_text(encoding='utf-8'))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def validate_pack(data, folder=None, *, check_images=False):
    try:
        pack_validator().validate(data)
    except jsonschema.ValidationError as error:
        raise ValueError('特殊技能素材包配置无效：' + error.message) from error
    tags = {a['tag'] for a in data['assets'] if a['group'] == 'banners'}
    contexts = {a['tag'] for a in data['assets'] if a['group'] == 'tooltips'}
    ids = [v['id'] for v in data['variants']]
    if len(ids) != len(set(ids)):
        raise ValueError('特殊技能规则 ID 重复')
    for rule in data['variants']:
        if not set(rule['banner_tags']) <= tags:
            raise ValueError('规则引用了不存在的技能名称条：' + rule['id'])
        if rule.get('required_context_tag') and rule['required_context_tag'] not in contexts:
            raise ValueError('规则引用了不存在的提牌说明：' + rule['id'])
    if data.get('layout_version') == '2.0':
        if not data['assets']:
            raise ValueError('新版素材包至少需要一张素材，空目录不能登记为可用')
        if type(data.get('content_id')) is not int or data['content_id']<=0:
            raise ValueError('新版素材包必须声明有效的学生 content_id')
        images=[a['image'] for a in data['assets']]
        if len(images)!=len(set(images)):
            raise ValueError('素材图片路径重复，请为每个样本使用独立文件')
        slug=checked_slug(data.get('student_slug'))
        if data['id']!=slug:
            raise ValueError('新版素材包 id 必须与 student_slug 一致')
        if folder is not None and Path(folder).name != slug:
            raise ValueError('学生目录名与 student_slug 不一致')
        for asset in data['assets']:
            if not asset.get('expected_size'):
                raise ValueError('新版素材必须声明 expected_size')
            key=checked_slug(asset.get('asset_key'))
            pattern=re.escape(asset['group']+'/'+key)+r'-[0-9]{3,}\.png'
            if not re.fullmatch(pattern,asset['image']):
                raise ValueError('素材路径不符合新版分组命名规则：'+asset['image'])
    if folder is not None:
        for asset in data['assets']:
            path=safe_media(Path(folder),asset['image'])
            if check_images:
                raw=path.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=asset['sha256']:
                    raise ValueError('特殊技能图片校验失败：'+asset['image'])
                image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_UNCHANGED)
                if image is None:
                    raise ValueError('特殊技能图片无法解码：'+asset['image'])
                if asset.get('expected_size') and list(image.shape[1::-1])!=asset['expected_size']:
                    raise ValueError('特殊技能图片尺寸不符：'+asset['image'])
    return data


def canonicalize(data, raw_assets, slug):
    """只调整组织方式，保留标签、身份、阶段和判定规则。"""
    checked_slug(slug)
    result=copy.deepcopy(data)
    result.update(layout_version='2.0', student_slug=slug, id=slug)
    counters=Counter();files={};assets=[]
    if len(raw_assets)!=len(result['assets']):
        raise ValueError('素材与图片数量不一致')
    for asset,raw in zip(result['assets'],raw_assets):
        key=asset_key(asset,result['variants']);counters[(asset['group'],key)]+=1
        asset['asset_key']=key
        asset['image']=asset_path(asset,counters[(asset['group'],key)],result['variants'])
        image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_UNCHANGED)
        if image is None:raise ValueError('迁移图片无法解码')
        asset['expected_size']=list(image.shape[1::-1])
        asset['sha256']=hashlib.sha256(raw).hexdigest()
        assets.append(asset);files[asset['image']]=raw
    result['assets']=assets
    validate_pack(result)
    return result,files


def write_pack(folder, data, files):
    folder=Path(folder)
    if folder.exists():raise ValueError('素材包目录已存在，不覆盖已有素材')
    # 在临时目录校验完成后整目录提交，失败不留下半个可加载包。
    folder.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder.parent,prefix='.skill-build-') as temporary:
        staging=Path(temporary)/folder.name;staging.mkdir()
        for name,raw in files.items():
            path=safe_media(staging,name);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        write_json(staging/'manifest.json',data)
        validate_pack(data,staging,check_images=True)
        staging.rename(folder)
    return folder/'manifest.json'


def migrate_library(root=DEFAULT_ROOT, *, students, slug_overrides=None, apply=False):
    """按学生合并旧包；原库整目录备份，切换失败时恢复原库。"""
    root=Path(root).resolve();overrides=slug_overrides or {}
    if root.name!='packs' or not root.is_dir() or root.is_symlink():
        raise ValueError('迁移目标必须是现有、非链接的 packs 目录')
    grouped={};before=[]
    from .student_names import resolve_name
    for path in sorted(root.glob('*/manifest.json')):
        data=json.loads(path.read_text(encoding='utf-8'));validate_pack(data,path.parent,check_images=True)
        aliases={normalize(v) for v in data.get('actor_aliases',[])}
        matches=[s for s in students if s['content_id']==data.get('content_id') or any(
            normalize(s.get(k,'')) in aliases for k in ('name','recognition_name','english_name'))]
        if len(matches)!=1:raise ValueError('素材学生身份无法唯一确定：'+path.parent.name)
        student=matches[0]
        english=student.get('english_name') or resolve_name(student['name'])[0]
        slug=overrides.get(str(student['content_id'])) or re.sub(r'[^a-z0-9]+','-',english.lower()).strip('-')
        checked_slug(slug)
        group=grouped.setdefault(slug,[])
        if group and (group[0][1]['reference_size']!=data['reference_size'] or
                      group[0][2]['content_id']!=student['content_id']):
            raise ValueError('同一学生目录有不同参考尺寸或学生 ID：'+slug)
        group.append((path,data,student));before.append(path.parent.name)
    if not grouped:raise ValueError('没有找到可迁移素材包')
    plans=[]
    for slug,entries in grouped.items():
        data=copy.deepcopy(entries[0][1]);data['assets']=[];data['variants']=[];raw_assets=[];aliases=[]
        data['content_id']=entries[0][2]['content_id']
        data['source_packs']=[]
        for path,old,_ in entries:
            aliases.extend(old.get('actor_aliases',[]))
            data['source_packs'].append({k:v for k,v in old.items() if k not in ('assets','variants')})
            for asset in old['assets']:
                data['assets'].append(dict(asset,source_pack_id=old['id']))
                raw_assets.append(safe_media(path.parent,asset['image']).read_bytes())
            data['variants'].extend(copy.deepcopy(old['variants']))
        data['actor_aliases']=list(dict.fromkeys(aliases))
        data['revision']=max(entry[1].get('revision',1) for entry in entries)+1
        data,files=canonicalize(data,raw_assets,slug)
        for path,old,_ in entries:
            declared={a['image'] for a in old['assets']}|{'manifest.json'}
            for extra in path.parent.rglob('*'):
                if not extra.is_file():continue
                name=extra.relative_to(path.parent).as_posix()
                if name in declared:continue
                raw=safe_media(path.parent,name).read_bytes()
                if name in files and files[name]!=raw:
                    raise ValueError('合并时附属文件内容冲突：'+slug+'/'+name)
                files[name]=raw
        plans.append((slug,data,files))
    summary=dict(old_packs=len(before),students=len(plans),assets=sum(len(d['assets']) for _,d,_ in plans),
                 directories=[slug for slug,_,_ in plans],backup=None)
    if not apply:return summary
    if all(d.get('layout_version')=='2.0' for _,d,_ in (entry for entries in grouped.values() for entry in entries)):
        summary['unchanged']=True
        return summary
    with tempfile.TemporaryDirectory(dir=root.parent,prefix='.skill-migrate-') as temporary:
        staging=Path(temporary)/'packs';staging.mkdir()
        for slug,data,files in plans:write_pack(staging/slug,data,files)
        backup_parent=root.parent/'backups'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:8])
        if not backup_parent.resolve().is_relative_to(root.parent):raise ValueError('迁移备份路径越界')
        backup_parent.mkdir(parents=True);backup=backup_parent/'packs'
        root.rename(backup)
        try:staging.rename(root)
        except Exception:
            backup.rename(root)
            raise
        summary['backup']=str(backup)
    return summary


def init_capture(template, root=DEFAULT_ROOT):
    data=json.loads(Path(template).read_text(encoding='utf-8'))
    slug=checked_slug(data.get('student_slug'));folder=Path(root)/slug
    if folder.exists():raise ValueError('学生素材目录已存在，请先备份或选择新目录')
    folder.mkdir(parents=True)
    for group in (*GROUPS,'sources'):(folder/group).mkdir()
    write_json(folder/'capture-plan.json',data)
    return folder


def compile_capture(folder):
    folder=Path(folder).resolve()
    data=json.loads((folder/'capture-plan.json').read_text(encoding='utf-8'))
    checked_slug(data.get('student_slug'))
    if folder.name!=data['student_slug']:raise ValueError('截图目录名与 student_slug 不一致')
    assets=[]
    source_log=folder/'capture-sources.json'
    sources=json.loads(source_log.read_text(encoding='utf-8')) if source_log.exists() else {}
    for entry in data['assets']:
        asset=dict(entry);path=safe_media(folder,asset['image'])
        if not path.exists() and asset.get('optional') and asset.get('reference_only'):continue
        raw=path.read_bytes();asset['sha256']=hashlib.sha256(raw).hexdigest()
        provenance=sources.get(asset['image'],{})
        if provenance.get('output_sha256') and provenance['output_sha256']!=asset['sha256']:
            raise ValueError('裁剪图已更改，与来源记录不符：'+asset['image'])
        asset['source']=sources.get(asset['image']) or dict(local_image=asset['image'],note='用户提供的本地截图裁剪；文件校验不等于识别准确率验收')
        assets.append(asset)
    data.update(assets=assets,layout_version='2.0',id=data['student_slug'])
    validate_pack(data,folder,check_images=True)
    path=folder/'manifest.json'
    if path.exists():
        raise ValueError('manifest.json 已存在，请先备份并移走后再编译，避免覆盖可用版本')
    write_json(path,data)
    return path


def crop_capture(folder, image_path, source, roi, viewport=None):
    """从用户明确指定的画面范围取图，不猜测角色或阶段。"""
    folder=Path(folder).resolve();source=Path(source).resolve()
    plan=json.loads((folder/'capture-plan.json').read_text(encoding='utf-8'))
    matches=[asset for asset in plan['assets'] if asset['image']==image_path]
    if len(matches)!=1:raise ValueError('目标图片不在截图计划中或路径重复')
    target=safe_media(folder,image_path)
    if target.exists():raise ValueError('目标裁剪图已存在，请先备份并移走，避免覆盖')
    raw=source.read_bytes();image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
    if image is None:raise ValueError('源截图无法解码')
    x,y,x2,y2=viewport or [0,0,image.shape[1],image.shape[0]]
    if not 0<=x<x2<=image.shape[1] or not 0<=y<y2<=image.shape[0]:raise ValueError('源截图客户区范围无效')
    width,height=plan['reference_size']
    if abs((x2-x)/(y2-y)-width/height)>.015:raise ValueError('客户区比例不符，请先排除标题栏、黑边和视频播放器边框')
    normalized=cv2.resize(image[y:y2,x:x2],(width,height))
    rx,ry,rx2,ry2=roi
    if not 0<=rx<rx2<=width or not 0<=ry<ry2<=height:raise ValueError('裁剪框超出参考画面')
    if matches[0].get('expected_size') and [rx2-rx,ry2-ry]!=matches[0]['expected_size']:
        raise ValueError('裁剪框尺寸与计划 expected_size 不符，不应拉伸图块凑尺寸')
    ok,encoded=cv2.imencode('.png',normalized[ry:ry2,rx:rx2])
    if not ok:raise ValueError('裁剪图片编码失败')
    source_log=folder/'capture-sources.json'
    sources=json.loads(source_log.read_text(encoding='utf-8')) if source_log.exists() else {}
    from .paths import portable_path
    sources[image_path]=dict(image=portable_path(source),sha256=hashlib.sha256(raw).hexdigest(),
                            viewport=[x,y,x2,y2],reference_size=[width,height],roi=list(roi),
                            output_sha256=hashlib.sha256(encoded.tobytes()).hexdigest())
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('xb') as handle:handle.write(encoded.tobytes())
    try:write_json(source_log,sources)
    except Exception:
        target.unlink()
        raise
    return target


def inventory(root=DEFAULT_ROOT):
    rows=[];seen=set();students={}
    for path in sorted(Path(root).glob('*/manifest.json')):
        data=json.loads(path.read_text(encoding='utf-8'));validate_pack(data,path.parent,check_images=True)
        ident=data.get('content_id');canonical=data.get('layout_version')=='2.0'
        if ident is not None:
            if ident in students and (canonical or students[ident]):
                raise ValueError('同一学生存在多份活动素材包，请将旧版本移到 backups')
            students[ident]=canonical
        for rule in data['variants']:
            if rule['id'] in seen:raise ValueError('跨素材包规则 ID 重复：'+rule['id'])
            seen.add(rule['id'])
        rows.append(dict(directory=path.parent.name,content_id=data.get('content_id'),
            actor_aliases=data.get('actor_aliases',[]),assets=len(data['assets']),rules=len(data['variants']),
            groups=dict(Counter(a['group'] for a in data['assets'])),layout=data.get('layout_version','legacy')))
    return rows
