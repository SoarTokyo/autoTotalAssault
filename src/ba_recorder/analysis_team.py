# SPDX-License-Identifier: GPL-3.0-or-later
"""分析队伍的轻量名称索引与校验，不构建图片特征。"""
import json
from pathlib import Path

from .paths import PROJECT_ROOT
from .analysis_io import safe_media
from .student_names import normalize,resolve_name


def available_students(manifests=None,names_root=None):
    if manifests is None:
        from .catalog_analysis import default_manifests
        manifests=default_manifests()
    names_root=Path(names_root or PROJECT_ROOT/'examples')
    assets={}
    for path in manifests:
        data=json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('schema_version')!='1.0':raise ValueError('不支持的学生图库清单版本')
        for asset in data['assets']:
            if asset['status']!='downloaded':continue
            if not safe_media(Path(path).resolve().parent,asset['image']).is_file():continue
            ident=asset['content_id'];old=assets.get(ident,{})
            if old and old.get('sha256')!=asset.get('sha256'):
                raise ValueError('同一学生在不同图库清单中的图片不一致，请核对图库')
            assets[ident]=dict(asset,local_name=asset.get('local_name') or old.get('local_name'))
    students=[]
    for ident,asset in assets.items():
        cn=asset['name'];english,words,form_words=resolve_name(cn,names_root)
        search=[cn,english,*words,*form_words]
        students.append(dict(content_id=ident,name=cn,english_name=english,
            recognition_name=asset.get('local_name') or cn,
            search_text=normalize(' '.join(search))))
    return sorted(students,key=lambda s:(s['name'],s['content_id']))


def validate_team(team,students=None):
    if not isinstance(team,dict) or team.get('schema_version')!='1.0':raise ValueError('分析队伍配置版本无效')
    entries=team.get('students')
    if not isinstance(entries,list) or len(entries)!=6:raise ValueError('请完整选择六名学生')
    ids=[row.get('content_id') if isinstance(row,dict) else None for row in entries]
    if any(type(ident)is not int or ident<=0 for ident in ids):raise ValueError('队伍中有未选中的学生')
    if len(set(ids))!=6:raise ValueError('同一学生不能重复选择，请检查六个位置')
    if students is not None:
        by_id={s['content_id']:s for s in students}
        if any(ident not in by_id for ident in ids):raise ValueError('所选学生不在当前可选目录或本地图库中，请刷新目录后重试')
        return dict(schema_version='1.0',students=[dict(position=i+1,content_id=ident,
            name=by_id[ident]['name'],english_name=by_id[ident]['english_name']) for i,ident in enumerate(ids)])
    return team
