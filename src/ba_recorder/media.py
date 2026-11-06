# SPDX-License-Identifier: GPL-3.0-or-later
"""统一读取旧 PNG 和新视频帧；按索引解码，不将整段视频拆成图片。"""
from pathlib import Path
import bisect
import cv2
import numpy as np

from .analysis_io import safe_media


class FrameSource:
    def __init__(self,session):
        self.session=Path(session)
        self.capture=None
        self.current_path=None
        self.next_index=0

    def read(self,frame):
        path=safe_media(self.session,frame['path'])
        if 'video_frame_index' not in frame:
            image=cv2.imdecode(np.fromfile(path,dtype=np.uint8),cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError(f'无法读取截图：{frame["path"]}')
            return image
        index=frame['video_frame_index']
        if type(index) is not int or index<0:
            raise ValueError('视频帧索引无效')
        # 顺序解码保证可变时间戳视频也按真实显示帧编号取图，不用 FPS 猜时间。
        if self.current_path!=path or index<self.next_index:
            self.close()
            self.capture=cv2.VideoCapture(str(path))
            if not self.capture.isOpened():
                raise ValueError(f'无法打开视频：{frame["path"]}')
            self.current_path=path
            self.next_index=0
        image=None
        while self.next_index<=index:
            ok,image=self.capture.read()
            if not ok:
                raise ValueError(f'视频缺少索引所指帧：{frame["path"]} / {index}')
            self.next_index+=1
        height,width=frame['content_height'],frame['content_width']
        if image is None or image.shape[0]<height or image.shape[1]<width:
            raise ValueError('视频尺寸小于原始客户区，不能恢复图像')
        return image[:height,:width]

    def close(self):
        if self.capture is not None:
            self.capture.release()
        self.capture=None
        self.current_path=None
        self.next_index=0

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.close()


def analysis_frames(frames,hz=5,events=()):
    if not any('video_frame_index' in f for f in frames):
        return frames
    indexes=set()
    next_ns=-1
    for i,frame in enumerate(frames):
        if frame['t_ns']>=next_ns:
            indexes.add(i)
            next_ns=frame['t_ns']+int(1e9/hz)
    times=[f['t_ns'] for f in frames]
    for event in events:
        offsets={'key_down':[-120_000_000],'mouse_down':[-40_000_000],'mouse_up':[0,250_000_000]}.get(event['kind'],[])
        for offset in offsets:
            target=event['t_ns']+offset
            index=bisect.bisect_left(times,target)
            nearest=min((i for i in (index-1,index) if 0<=i<len(frames)),key=lambda i:abs(times[i]-target))
            indexes.add(nearest)
    return [frames[i] for i in sorted(indexes)]
