# SPDX-License-Identifier: GPL-3.0-or-later
"""在低级键鼠钩子安装前完成原生媒体依赖加载。"""
import importlib.util
import os
import sys


def prepare_media(config):
    if not (config.video_enabled or config.capture_backend=='wgc' or config.screenshot_format=='jpg'):
        return None,None
    if 'numpy' not in sys.modules:
        for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
            os.environ[name]='1'
    import numpy as np
    import cv2
    cv2.setNumThreads(1)
    # 编码线程和采样循环开始后只能使用已加载模块，不并发首次加载 DLL。
    if config.video_enabled and config.video_codec in ('auto','libx264') and importlib.util.find_spec('av'):
        import av
    return cv2,np
