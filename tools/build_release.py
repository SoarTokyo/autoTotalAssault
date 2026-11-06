# SPDX-License-Identifier: GPL-3.0-or-later
"""按白名单生成可审查的源码运行包，不包含本机数据、测试或环境。"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path,PurePosixPath
from zipfile import ZipFile,ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1]
FORBIDDEN={'tests','dev','__pycache__','.local','.venv','.tools','.research','recordings','scripts','analyses','replays','logs','exports'}
DEMO_SCRIPT='scripts/tor格里高利礼奈春锅轴4001w.json'


def release_entries(root=ROOT):
    root=Path(root).resolve();config=json.loads((root/'release_files.json').read_text(encoding='utf-8'))
    paths={p:p for p in config['files']}
    for pattern in config['patterns']:
        for path in root.glob(pattern):paths[path.relative_to(root).as_posix()]=path.relative_to(root).as_posix()
    paths.update(config.get('renamed_files',{}))
    result={}
    for source,destination in paths.items():
        path=(root/source).resolve();parts=PurePosixPath(destination).parts
        if not path.is_relative_to(root) or not path.is_file():raise ValueError('发布文件缺失或路径无效：'+source)
        is_demo=source==destination==DEMO_SCRIPT
        checked_parts=(*parts,*path.relative_to(root).parts)
        if '..' in parts or PurePosixPath(destination).is_absolute() or (not is_demo and any(
                p.casefold() in FORBIDDEN or p.casefold().startswith('.venv') for p in checked_parts)):
            raise ValueError('发布白名单包含受保护或测试路径：'+destination)
        if destination.casefold() in {v.casefold() for v in result}:raise ValueError('发布目标路径重复：'+destination)
        result[destination]=path
    return result


def build_release(destination,root=ROOT):
    destination=Path(destination)
    if destination.exists():raise ValueError('发布包已存在，不覆盖已有文件')
    entries=release_entries(root);destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_suffix('.zip.partial');manifest=[]
    try:
        with ZipFile(temporary,'w',ZIP_DEFLATED) as archive:
            for name,path in sorted(entries.items()):
                raw=path.read_bytes();archive.writestr(name,raw)
                manifest.append(dict(path=name,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
        temporary.replace(destination)
    finally:temporary.unlink(missing_ok=True)
    destination.with_suffix('.files.json').write_text(json.dumps(dict(files=manifest),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return destination,manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'dist'/f'BlueArchiveBattleRecorder-{datetime.now():%Y%m%d-%H%M%S}-source.zip')
    parser.add_argument('--list',action='store_true',help='只查看发布白名单展开结果')
    args=parser.parse_args()
    if args.list:
        for name in sorted(release_entries()):print(name)
    else:
        path,files=build_release(args.output);print(f'已生成发布包：{path}，共 {len(files)} 个文件。')
