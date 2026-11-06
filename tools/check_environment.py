# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI 启动前检查依赖；仅在项目虚拟环境内自动补齐依赖。"""
import argparse
from importlib import metadata
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
LOCK_NAME = 'requirements-win64.lock.txt'
MODULES = {
    # 先预加载 PyAV 的编码 DLL，顺序与正式录制入口保持一致。
    'av': 'av', 'mss': 'mss', 'pynput': 'pynput', 'pywin32': 'win32gui',
    'opencv-python': 'cv2', 'numpy': 'numpy', 'jsonschema': 'jsonschema',
    'PySide6': 'PySide6.QtWidgets',
}


def project_requirements(root=ROOT):
    project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    return project['dependencies'] + project['optional-dependencies']['gui']


def requirements(root=ROOT):
    path = root / LOCK_NAME
    if not path.is_file():
        raise ValueError('运行包缺少依赖锁定文件：' + LOCK_NAME)
    pins = []
    names = set()
    for line in path.read_text(encoding='utf-8').splitlines():
        value = line.strip()
        if not value or value.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_-]+)==(\d+(?:\.\d+)*)', value)
        if not match:
            raise ValueError('锁定文件包含无效依赖：' + value)
        name = re.sub(r'[-_.]+', '-', match[1]).lower()
        if name in names:
            raise ValueError('锁定文件包含重复依赖：' + match[1])
        names.add(name)
        pins.append(value)
    by_name={re.sub(r'[-_.]+','-',value.split('==')[0]).lower():value.split('==')[1] for value in pins}
    for requirement in project_requirements(root):
        name=re.match(r'[A-Za-z0-9_-]+',requirement)[0]
        version=by_name.get(re.sub(r'[-_.]+','-',name).lower())
        if version is None or not version_matches(version,requirement):
            raise ValueError('锁定版本缺失或不符合项目声明：'+requirement)
    return pins


def version_matches(version, requirement):
    """处理本项目声明的数字版本范围；未知语法直接报错，避免误判。"""
    match = re.match(r'^\s*[A-Za-z0-9_-]+(.*)$', requirement.split(';')[0])
    if not match:
        raise ValueError('无法解析依赖声明：' + requirement)
    # 预发布版不作为普通用户环境的稳定依赖。
    if not re.fullmatch(r'\d+(?:\.\d+)*', version):
        return False
    actual = tuple(map(int, version.split('.')))
    for clause in match[1].split(','):
        condition = re.fullmatch(r'\s*(>=|<=|==|<|>)\s*(\d+(?:\.\d+)*)\s*', clause)
        if not condition:
            raise ValueError('不支持的依赖版本约束：' + requirement)
        expected = tuple(map(int, condition[2].split('.')))
        length = max(len(actual), len(expected))
        left = actual + (0,) * (length - len(actual))
        right = expected + (0,) * (length - len(expected))
        if not {'>=': left >= right, '<=': left <= right, '==': left == right,
                '<': left < right, '>': left > right}[condition[1]]:
            return False
    return True


def dependency_issues():
    issues = []
    for requirement in requirements():
        name = re.match(r'[A-Za-z0-9_-]+', requirement)[0]
        try:
            version = metadata.version(name)
            if not version_matches(version, requirement):
                issues.append(f'{name} 版本 {version} 不满足 {requirement}')
        except metadata.PackageNotFoundError:
            issues.append(f'未安装 {name}')
    return issues


def probe():
    """单独进程加载原生库，DLL 错误或崩溃不会直接带走启动器。"""
    source = '''import importlib, json
failures=[]
for name, module in json.loads(__import__('sys').argv[1]).items():
    print('正在加载：'+name, flush=True)
    try:
        importlib.import_module(module)
    except Exception as error:
        failures.append(name+': '+str(error))
if not failures:
    try:
        from PySide6.QtWidgets import QApplication
        app=QApplication([])
        import av
        av.CodecContext.create('libx264', 'w')
        app.quit()
    except Exception as error:
        failures.append('Qt 或 H.264 编码器初始化失败：'+str(error))
for failure in failures: print(failure, flush=True)
raise SystemExit(bool(failures))
'''
    return subprocess.run([sys.executable, '-u', '-c', source,
                           json.dumps(MODULES)], cwd=ROOT).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='自动安装缺少或版本不符的依赖')
    args = parser.parse_args(argv)
    if sys.platform != 'win32' or sys.maxsize <= 2**32 or sys.version_info < (3, 11):
        print('需要 Windows、64 位 Python 3.11 或更高版本。', flush=True)
        return 1
    try:
        print('正在校验启动环境……', flush=True)
        issues = dependency_issues()
        if issues:
            print('\n'.join(issues), flush=True)
            if not args.install:
                return 1
            if Path(sys.prefix).resolve() != (ROOT / '.venv').resolve() or sys.prefix == sys.base_prefix:
                raise RuntimeError('自动安装只允许写入本项目 .venv，请运行 tools/setup.ps1 -WithGui。')
            print('正在补齐项目依赖，首次下载可能需要几分钟……', flush=True)
            result = subprocess.run([sys.executable, '-m', 'pip', 'install', '--only-binary=:all:',
                                     '--disable-pip-version-check', '-r', str(ROOT / LOCK_NAME)], cwd=ROOT)
            if result.returncode or dependency_issues():
                raise RuntimeError('依赖安装未完成，请检查网络、代理及可用磁盘空间。')
        if subprocess.run([sys.executable, '-m', 'pip', 'check'], cwd=ROOT).returncode:
            raise RuntimeError('依赖之间存在冲突，请根据上方信息修复项目环境。')
        if probe():
            raise RuntimeError('原生库、Qt 或编码器加载失败；请查看上方错误。可尝试重建环境，或安装微软 VC++ x64 运行库。')
        print('启动依赖校验通过。', flush=True)
        return 0
    except Exception as error:
        print('环境检查失败：' + str(error), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
