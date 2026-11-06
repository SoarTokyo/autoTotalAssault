# SPDX-License-Identifier: GPL-3.0-or-later
"""下载 GameKee 实装学生栏目图标到独立本地素材目录。"""
import argparse
import json
from pathlib import Path
import sys

from ba_recorder.gamekee_catalog import download


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--names',nargs='+',help='精确指定目录中的学生名称')
    group.add_argument('--all',action='store_true',help='下载实装学生栏目全部图标（不包括 NPC）')
    group.add_argument('--limit',type=int,default=6,help='默认先下载目录前6名供验证')
    group.add_argument('--team-file',type=Path,help='使用本地名称与网站条目 ID 映射配置')
    parser.add_argument('--catalog-json',type=Path,help='使用本地保存的该栏目接口 JSON')
    parser.add_argument('--batch-size',type=int,default=5,help='每组请求几张图片，默认5；已有图片不重复请求')
    parser.add_argument('--batch-delay',type=float,default=30,help='组间等待秒数，默认30')
    parser.add_argument('--request-delay',type=float,default=1,help='组内相邻图片请求等待秒数，默认1')
    parser.add_argument('--output',type=Path,
        default=Path(__file__).resolve().parents[1]/'.local/gamekee/students')
    args = parser.parse_args()
    try:
        print('学生图片来源：GameKee 蔚蓝档案 Wiki https://www.gamekee.com/ba/second/23941；感谢维护者与贡献者。')
        print('项目维护者已就此下载方式与 Wiki 维护方沟通并获得同意；请保持已沟通的低频分批方案并尊重限流，不扩大为素材再分发授权。')
        team=json.loads(args.team_file.read_text(encoding='utf-8')) if args.team_file else None
        path,manifest = download(args.output,names=args.names or (),limit=args.limit,
                                 all_students=args.all,catalog_json=args.catalog_json,team=team,
                                 batch_size=args.batch_size,batch_delay=args.batch_delay,request_delay=args.request_delay)
        ok = sum(row['status']=='downloaded' for row in manifest['assets'])
        total=manifest['requested_count']
        print(f'完成：{ok}/{total}；来源清单：{path}')
        print('有效学生图库可用于分析时选择六人队伍；多形态技能阶段仍需独立素材，本次下载不会自动补齐。')
        return 0 if ok == total else 1
    except (OSError,ValueError) as error:
        print(f'下载未完成：{error}',file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
