# SPDX-License-Identifier: GPL-3.0-or-later
"""选人目录与所选学生缺图补齐；可作为 GUI 的独立后台进程运行。"""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .analysis_io import safe_media
from .analysis_team import available_students,validate_team
from .gamekee_catalog import CATALOG_URL,INDEX_URL,STUDENT_PID,decode_image,download,fetch_bytes,parse_catalog
from .paths import PROJECT_ROOT
from .student_names import normalize,resolve_name

DEFAULT_OUTPUT=PROJECT_ROOT/'.local/gamekee/students'


def atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.partial')
    try:
        temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)


def manifests(output):return sorted(Path(output).glob('manifest-*.json'))


def catalog_payload(output=DEFAULT_OUTPUT,*,refresh=False,fetcher=fetch_bytes,progress=print):
    """只获取文字目录；已有完整本地目录时不额外请求网络。"""
    output=Path(output);cache=output/'catalog-index.json'
    cached=None
    if cache.exists():
        try:
            payload=json.loads(cache.read_text(encoding='utf-8'));parse_catalog(payload)
            cached=payload
            if not refresh:return payload
        except (OSError,ValueError,TypeError):
            progress('学生文字目录缓存无效，将重新获取。')
    rows={};expected=0
    for path in manifests(output):
        data=json.loads(path.read_text(encoding='utf-8'));expected=max(expected,data.get('catalog_count',0))
        for asset in data.get('assets',[]):
            if asset.get('image_url'):
                rows[asset['content_id']]=dict(id=asset.get('entry_id'),content_id=asset['content_id'],
                    pid=asset.get('category_pid',STUDENT_PID),name=asset['name'],icon=asset['image_url'])
    if not refresh and expected and len(rows)>=expected:
        payload=dict(code=0,data=list(rows.values()));parse_catalog(payload);atomic_json(cache,payload)
        return payload
    try:
        progress('正在读取 GameKee 学生文字目录；此步不下载图片。')
        payload=json.loads(fetcher(CATALOG_URL,catalog=True).decode('utf-8'))
        parse_catalog(payload);atomic_json(cache,payload)
        return payload
    except (OSError,ValueError) as error:
        if cached is not None:
            progress('目录刷新失败，继续使用已有缓存：'+str(error));return cached
        if rows:
            progress('无法获取完整学生目录，暂使用已有本地条目：'+str(error))
            payload=dict(code=0,data=list(rows.values()));parse_catalog(payload)
            return payload
        raise ValueError('无法读取学生文字目录，请检查网络后重试：'+str(error)) from error


def selection_students(payload,output=DEFAULT_OUTPUT):
    local={s['content_id']:s for s in available_students(manifests(output))}
    students=[]
    for row in parse_catalog(payload):
        english,words,forms=resolve_name(row['name'])
        previous=local.get(row['content_id'],{})
        students.append(dict(content_id=row['content_id'],name=row['name'],english_name=english,
            recognition_name=previous.get('recognition_name') or row['name'],
            downloaded=bool(previous),search_text=normalize(' '.join([row['name'],english,*words,*forms,
                previous.get('recognition_name','')]))))
    return sorted(students,key=lambda s:(s['name'],s['content_id']))


def ensure_team_images(team,output=DEFAULT_OUTPUT,*,fetcher=fetch_bytes,progress=print):
    output=Path(output)
    team=validate_team(team)
    assets={}
    for path in manifests(output):
        data=json.loads(path.read_text(encoding='utf-8'))
        for asset in data.get('assets',[]):
            if asset.get('status')=='downloaded':assets[asset['content_id']]=(asset,path.parent)
    missing=[]
    for student in team['students']:
        found=assets.get(student['content_id'])
        if found:
            asset,parent=found;path=safe_media(parent,asset['image'])
            if path.exists():
                raw=path.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=asset['sha256']:
                    raise ValueError('本地学生图片校验失败，未覆盖，请先核对：'+student.get('name',str(student['content_id'])))
                decode_image(raw)
                continue
        missing.append(student)
    if missing:
        payload=catalog_payload(output,fetcher=fetcher,progress=progress)
        team=validate_team(team,selection_students(payload,output))
        wanted={s['content_id'] for s in missing};missing=[s for s in team['students'] if s['content_id'] in wanted]
        by_id={row['content_id']:row for row in parse_catalog(payload)}
        progress('所选队伍缺少 '+str(len(missing))+' 名学生图片，将仅下载：'+'、'.join(s['name'] for s in missing))
        progress('图片来源：'+INDEX_URL+'；感谢 Wiki 维护方与贡献者。项目维护者已就此下载方式沟通并获得同意。')
        # 使用本次已核对的目录快照，避免名称歧义；只请求所选缺图学生。
        cache=output/'catalog-index.json';atomic_json(cache,payload)
        mapping=dict(schema_version='1.0',students=[dict(gamekee_content_id=s['content_id'],
            gamekee_name=by_id[s['content_id']]['name'],local_name=s['name']) for s in missing])
        _,result=download(output,team=mapping,catalog_json=cache,fetcher=fetcher,progress=progress)
        failed=[a for a in result['assets'] if a['status']!='downloaded']
        if failed or len(result['assets'])!=len(missing):
            raise ValueError('所选学生图片未能全部下载，已保留完成部分，请检查网络或限流提示后重试')
    else:progress('所选六名学生图片已齐全且校验通过，无需下载。')
    validated=validate_team(team,available_students(manifests(output)))
    return dict(team=validated,downloaded_ids=[s['content_id'] for s in missing])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library',type=Path,default=DEFAULT_OUTPUT)
    sub=parser.add_subparsers(dest='command',required=True)
    listing=sub.add_parser('list');listing.add_argument('--output',type=Path,required=True);listing.add_argument('--refresh',action='store_true')
    ensure=sub.add_parser('ensure');ensure.add_argument('--team',type=Path,required=True);ensure.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    progress=lambda value:print(value,flush=True)
    try:
        if args.command=='list':
            payload=catalog_payload(args.library,refresh=args.refresh,progress=progress)
            data=dict(students=selection_students(payload,args.library))
        else:
            data=ensure_team_images(json.loads(args.team.read_text(encoding='utf-8')),args.library,progress=progress)
        atomic_json(args.output,data)
        return 0
    except (OSError,ValueError,TypeError,KeyError) as error:
        progress('队伍素材准备失败：'+str(error));return 1


if __name__=='__main__':raise SystemExit(main())
