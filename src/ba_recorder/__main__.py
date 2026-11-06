# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import json
from pathlib import Path
import sys

from .config import Config


def main():
    # Windows 重定向输出也使用 UTF-8，避免中文日志在工具终端中乱码。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description="蔚蓝档案视频录制、技能轴分析与回放工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser('gui', help='打开中文录制界面')
    sub.add_parser('doctor', help='检查 Python 与已安装依赖，不启动录制')
    replay=sub.add_parser('replay',help='按游戏倒计时复现已核对技能轴，默认仅演练')
    replay.add_argument('script',type=Path)
    replay.add_argument('--hwnd',required=True,type=lambda value:int(value,0))
    replay.add_argument('--visual-profile',required=True,type=Path)
    replay.add_argument('--output',type=Path,default=Path('replays'))
    replay.add_argument('--execute',action='store_true',help='发送实际键鼠输入；不指定则仅演练')
    replay.add_argument('--input-lead-ms',type=int,default=130,help='选卡和点击的提前预算，现实毫秒，默认130；另计识别耗时')
    replay.add_argument('--late-tolerance-ms',type=int,default=1500,help='最小迟到容差，游戏毫秒，默认1500；保留轴中更大的容差')
    replay.add_argument('--no-predictive-timing',action='store_true',help='关闭短期预判定时释放，改为逐帧判断')
    conversion=sub.add_parser('convert-video',help='将已有录制导出为独立视频副本，不删除原PNG')
    conversion.add_argument('session',type=Path)
    conversion.add_argument('--output',type=Path,default=Path('exports/compact'))
    conversion.add_argument('--codec',choices=['auto','libx264','h264_msmf','mp4v'],default='auto')
    conversion.add_argument('--crf',type=int,default=18)
    extraction=sub.add_parser('extract-frame',help='按录制相对秒数提取单帧，供校准使用')
    extraction.add_argument('session',type=Path)
    extraction.add_argument('--seconds',type=float,required=True)
    extraction.add_argument('--output',type=Path,required=True)
    automatic = sub.add_parser('auto-analyze', help='使用本地字形和模板自动生成有效操作表，不调用大模型')
    automatic.add_argument('session', type=Path)
    automatic.add_argument('--visual-profile', type=Path, required=True)
    automatic.add_argument('--output', type=Path, default=Path('analyses'))
    automatic.add_argument('--scripts-output',type=Path,default=Path('scripts'),help='自动导出的待核对技能轴目录')
    automatic.add_argument('--team',type=Path,help='六人队伍配置，只加载这六名学生的图库图片')
    calibration = sub.add_parser('calibrate', help='在录制截图上手动框选卡槽、目标区及控件')
    calibration.add_argument('--frame', type=Path, required=True)
    calibration.add_argument('--output', type=Path, required=True)
    calibration.add_argument('--base', type=Path)
    template = sub.add_parser('add-template', help='从实际截图框选并追加状态模板')
    template.add_argument('--frame', type=Path, required=True)
    template.add_argument('--config', type=Path, required=True)
    template.add_argument('--field', choices=['auto', 'speed', 'menu_open', 'selected_slot'], required=True)
    template.add_argument('--value', type=json.loads, required=True, help='JSON 状态值，例如 true、false、1、null')
    analysis = sub.add_parser('analyze', help='离线解析战斗动作，输出独立中文报告')
    analysis.add_argument('session', type=Path, help='录制会话目录')
    analysis.add_argument('--profile', type=Path, help='按键及校准区域配置')
    analysis.add_argument('--observations', type=Path, help='识别结果或人工校正后的独立 JSONL')
    analysis.add_argument('--output', type=Path, default=Path('analyses'))
    recognition = sub.add_parser('recognize', help='用已校准模板离线识别 AUTO、倍速、菜单及选卡状态')
    recognition.add_argument('session', type=Path)
    recognition.add_argument('--templates', type=Path, required=True)
    recognition.add_argument('--output', type=Path, default=Path('analyses'))
    sub.add_parser("windows", help="列出可见窗口及其窗口句柄（HWND）")
    start = sub.add_parser("record", help="为指定窗口准备录制")
    start.add_argument("--hwnd", required=True, type=lambda value: int(value, 0), help="目标窗口句柄，支持十进制和 0x 十六进制")
    start.add_argument("--config", type=Path, help="录制配置文件路径")
    start.add_argument("--output", type=Path, default=Path("recordings"), help="录制输出目录（默认：recordings）")
    start.add_argument('--control-stdin', action='store_true', help=argparse.SUPPRESS)
    start.add_argument('--name', help='录制名称（支持中文，作为本次会话目录名；重名不覆盖）')
    start.add_argument('--start-when-focused', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.command == 'gui':
            from .gui import launch
            return launch()
        if args.command == 'doctor':
            from .doctor import doctor
            return doctor()
        if args.command=='replay':
            from .replay import run
            return run(args.script,args.visual_profile,args.hwnd,args.output,dry_run=not args.execute,
                       input_lead_ms=args.input_lead_ms,late_tolerance_ms=args.late_tolerance_ms,
                       predictive_timing=not args.no_predictive_timing)
        if args.command=='convert-video':
            from .video_tools import convert
            path,source_bytes,video_bytes=convert(args.session,args.output,args.codec,args.crf)
            print(f'视频副本：{path}\n媒体大小：{source_bytes/1024**2:.1f} MiB → {video_bytes/1024**2:.1f} MiB；原始录制未删除。')
            return 0
        if args.command=='extract-frame':
            from .video_tools import extract
            frame=extract(args.session,args.seconds,args.output)
            print(f"已提取 {frame['id']}，录制时钟 {frame['t_ns']/1e9:.3f} 秒，保存至 {args.output}")
            return 0
        if args.command == 'auto-analyze':
            from .deterministic import run
            folder, summary = run(args.session, args.visual_profile, args.output,team_path=args.team)
            from .replay_script import export_analysis_script
            session_manifest=json.loads((args.session/'session.json').read_text(encoding='utf-8'))
            keymap=session_manifest.get('config',{}).get('keymap') or Config.load(
                Path(__file__).resolve().parents[2]/'examples/recorder.json').keymap.copy()
            script_path=export_analysis_script(folder/'auto_actions.json',args.session,keymap,args.scripts_output)
            print(f'已导出待核对技能轴：{script_path}',flush=True)
            print('@analysis ' + json.dumps({'output': str(folder), 'script_path':str(script_path),
                                             'counts': summary['counts']}, ensure_ascii=False), flush=True)
            return 0
        if args.command == 'calibrate':
            from .calibration import calibrate
            print(f'校准配置已保存：{calibrate(args.frame, args.output, args.base)}')
            return 0
        if args.command == 'add-template':
            from .template_setup import add_template
            print(f'状态模板已保存：{add_template(args.frame, args.config, args.field, args.value)}')
            return 0
        if args.command == 'analyze':
            from .analysis_io import analyze
            from .analysis_profile import AnalysisProfile
            profile = AnalysisProfile.load(args.profile)
            if not args.profile:
                manifest = json.loads((args.session / 'session.json').read_text(encoding='utf-8'))
                profile.keymap.update(manifest.get('config', {}).get('keymap', {}))
                profile.__post_init__()
            folder, count = analyze(args.session, profile, args.observations, args.output)
            print(f'已生成 {count} 项动作候选。分析结果：{folder}')
            return 0
        if args.command == 'recognize':
            from .recognition import recognize
            path, count = recognize(args.session, args.templates, args.output)
            print(f'已生成 {count} 条识别结果：{path}')
            return 0
        from .windows import initialize_dpi, list_windows
        initialize_dpi()
        if args.command == "windows":
            print(json.dumps(list_windows(), indent=2, ensure_ascii=False))
            return 0
        config = Config.load(args.config).without_game_exit_hotkey()
        config = config.for_recording('video' if config.video_enabled else 'screenshots')
        from .recorder import record
        return record(args.hwnd, config, args.output, control_stdin=args.control_stdin,
                      start_when_focused=args.start_when_focused,session_name=args.name)
    except (Exception, KeyboardInterrupt) as error:
        print(f"录制器错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
