# SPDX-License-Identifier: GPL-3.0-or-later
"""图库文件命名与已有清单迁移；保持学生 ID 和图片内容不变。"""
import hashlib
import json
import shutil
from pathlib import Path
from .analysis_io import safe_media
from .student_names import image_filename,resolve_name


def image_owners(root):
    owners={}
    for path in Path(root).glob('manifest-*.json'):
        for asset in json.loads(path.read_text(encoding='utf-8')).get('assets',[]):
            if asset.get('status')!='downloaded':continue
            key=asset['image'].casefold();value=(asset['content_id'],asset['sha256'],asset.get('image_url'))
            if key in owners and owners[key][:2]!=value[:2]:raise ValueError('图库清单存在冲突的图片归属')
            owners[key]=value
    return owners


def rename_catalog_images(root,*,apply=False):
    root=Path(root).resolve();manifests=[];copies={};destinations={}
    for path in sorted(root.glob('manifest-*.json')):
        raw=path.read_bytes();data=json.loads(raw.decode('utf-8'));updated=json.loads(raw.decode('utf-8'))
        for asset in updated.get('assets',[]):
            if asset.get('status')!='downloaded':continue
            old=safe_media(root,asset['image'])
            if not old.is_file() or hashlib.sha256(old.read_bytes()).hexdigest()!=asset['sha256']:
                raise ValueError('迁移前图片校验失败：'+asset['image'])
            relative='images/'+image_filename(asset['name'],old.suffix)
            owner=(asset['content_id'],asset['sha256'])
            if relative.casefold() in destinations and destinations[relative.casefold()]!=owner:
                raise ValueError('学生英文名称重名，请先补全版本区别：'+relative)
            destinations[relative.casefold()]=owner
            target=safe_media(root,relative)
            if old in copies and copies[old]!=(target,asset['sha256']):
                raise ValueError('同一图片对应不同英文名称，请先核对清单')
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()!=asset['sha256']:
                raise ValueError('英文名目标文件已存在且内容不同，未覆盖：'+relative)
            copies[old]=(target,asset['sha256'])
            asset.update(image=relative,english_name=resolve_name(asset['name'])[0])
        updated['image_naming']='english-v1'
        if updated!=data:manifests.append((path,raw,updated))
    plan=dict(images=sum(a!=b for a,(b,_) in copies.items()),manifests=len(manifests),
              changes=[dict(old=a.relative_to(root).as_posix(),new=b.relative_to(root).as_posix())
                       for a,(b,_) in copies.items() if a!=b])
    if not apply:return plan
    created=[];committed=[]
    try:
        for source,(target,digest) in copies.items():
            if source==target or target.exists():continue
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target);created.append(target)
            if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise ValueError('复制后的图片校验失败')
        for path,raw,data in manifests:
            temp=path.with_suffix('.json.rename-partial')
            temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            temp.replace(path);committed.append((path,raw))
    except Exception:
        for path,raw in reversed(committed):path.write_bytes(raw)
        for target in created:target.unlink(missing_ok=True)
        raise
    # 全部新图片和清单提交后，才移除不再引用的旧命名文件。
    targets={b for b,_ in copies.values()}
    for source,(target,_) in copies.items():
        if source!=target and source not in targets:source.unlink()
    return plan
