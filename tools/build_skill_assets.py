# SPDX-License-Identifier: GPL-3.0-or-later
"""从已有配置、录制帧或外部截图构建本地特殊技能素材库。"""
import argparse
from pathlib import Path
from ba_recorder.skill_assets import import_profile,build_recorded_pack,build_image_pack

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--profile',type=Path,help='迁移已有视觉配置中的角色素材')
parser.add_argument('--spec',type=Path,help='技能素材标注 JSON')
parser.add_argument('--session',type=Path,help='包含 session.json 的录制目录')
parser.add_argument('--images',type=Path,help='外部截图源目录，配合含截图哈希与有效画面范围的标注')
parser.add_argument('--output',type=Path,default=Path('.local/special-skills/packs'))
args=parser.parse_args()
if args.profile:
    for path in import_profile(args.profile,args.output):print('已迁移技能素材包：',path)
elif args.spec and args.images and not args.session:
    print('已建立截图技能素材包：',build_image_pack(args.spec,args.images,args.output))
elif args.spec and args.session and not args.images:
    print('已建立技能素材包：',build_recorded_pack(args.spec,args.session,args.output))
else:parser.error('请指定 --profile，或指定 --spec 并选择 --session / --images 其中一种来源')
