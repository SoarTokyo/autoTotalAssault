# SPDX-License-Identifier: GPL-3.0-or-later
"""整理、校验本地特殊技能库，以及按截图计划登记用户自制素材。"""
import argparse
import json
from pathlib import Path
from ba_recorder.paths import PROJECT_ROOT
from ba_recorder.skill_library import (DEFAULT_ROOT, checked_slug, compile_capture, crop_capture, init_capture,
                                      inventory, migrate_library)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=DEFAULT_ROOT,help='素材 packs 根目录，默认项目 .local/special-skills/packs')
    commands=parser.add_subparsers(dest='command',required=True)
    migrate=commands.add_parser('migrate',help='预览或执行旧库整理，执行前自动整库备份')
    migrate.add_argument('--apply',action='store_true',help='实际迁移；不传此项仅预览')
    migrate.add_argument('--mapping',type=Path,default=PROJECT_ROOT/'examples/special_skill_student_slugs.json')
    init=commands.add_parser('init',help='创建截图计划和分组目录；不会生成可加载 manifest')
    init.add_argument('student',help='内置截图模板名，如 toki、hina-dress')
    init.add_argument('--plan',type=Path,help='使用自己编写的截图计划 JSON')
    compile_=commands.add_parser('compile',help='检查裁剪图尺寸、生成哈希并提交本地清单，不覆盖已有清单')
    compile_.add_argument('student')
    crop=commands.add_parser('crop',help='按明确客户区和裁剪框生成计划中的一张图，不修改源截图')
    crop.add_argument('student')
    crop.add_argument('--asset',required=True,help='capture-plan 中的 image 路径')
    crop.add_argument('--source',required=True,type=Path)
    crop.add_argument('--roi',required=True,nargs=4,type=int,metavar=('X1','Y1','X2','Y2'))
    crop.add_argument('--viewport',nargs=4,type=int,help='源截图中游戏客户区范围，默认整张图')
    commands.add_parser('validate',help='校验全部已登记学生、图片哈希、尺寸及规则引用')
    commands.add_parser('list',help='显示已登记学生与素材数量')
    args=parser.parse_args()
    try:
        if args.command=='migrate':
            from ba_recorder.analysis_team import available_students
            mapping=json.loads(args.mapping.read_text(encoding='utf-8'))
            result=migrate_library(args.root,students=available_students(),slug_overrides=mapping,apply=args.apply)
            print(('已迁移：' if args.apply else '迁移预览：')+json.dumps(result,ensure_ascii=False))
        elif args.command=='init':
            slug=checked_slug(args.student)
            plan=args.plan or PROJECT_ROOT/'examples/special_skill_templates'/f'{slug}.json'
            data=json.loads(plan.read_text(encoding='utf-8'))
            if data.get('student_slug')!=slug:raise ValueError('计划中的 student_slug 与命令指定学生不一致')
            print('截图目录已创建，请按 capture-plan.json 放置裁剪图：'+str(init_capture(plan,args.root)))
        elif args.command=='compile':
            print('本地清单已登记：'+str(compile_capture(args.root/checked_slug(args.student))))
            print('文件校验通过不代表识别准确率已验证，请用自己的短录制离线检查。')
        elif args.command=='crop':
            print('裁剪图已保存：'+str(crop_capture(args.root/checked_slug(args.student),args.asset,args.source,args.roi,args.viewport)))
        else:
            rows=inventory(args.root)
            for row in rows:print(json.dumps(row,ensure_ascii=False))
            print(f'已校验 {len(rows)} 名学生，{sum(r["assets"] for r in rows)} 张素材，{sum(r["rules"] for r in rows)} 条规则。')
        return 0
    except (OSError,ValueError,KeyError,TypeError) as error:
        print('素材处理未完成：'+str(error))
        return 1


if __name__=='__main__':raise SystemExit(main())
