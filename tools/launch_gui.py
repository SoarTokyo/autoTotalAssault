# SPDX-License-Identifier: GPL-3.0-or-later
"""双击入口：独立启动日志及失败弹窗，避免 pythonw 隐藏异常。"""
from datetime import datetime
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
import traceback


def main():
    root = Path(__file__).resolve().parents[1]
    log_path = root / 'logs' / f"gui-startup-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('x', encoding='utf-8', buffering=1) as log, redirect_stdout(log), redirect_stderr(log):
            print(f'启动时间：{datetime.now().isoformat()}\n解释器：{sys.executable}\n项目目录：{root}')
            # 直接从项目源码导入，双击启动不依赖当前目录或 editable 安装记录。
            sys.path.insert(0, str(root / 'src'))
            try:
                from ba_recorder.gui import launch
                result = launch(smoke_test='--smoke-test' in sys.argv)
                print(f'界面正常退出，退出码：{result}')
                return result
            except Exception:
                traceback.print_exc(file=log)
                raise
    except Exception as error:
        message = f'录制界面启动失败：{error}\n\n错误日志：{log_path}'
        if sys.platform == 'win32' and '--smoke-test' not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, '蔚蓝档案录制器启动失败', 0x10)
        elif sys.__stderr__ is not None:
            sys.__stderr__.write(message + '\n')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
