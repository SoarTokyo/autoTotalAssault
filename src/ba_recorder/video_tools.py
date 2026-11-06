# SPDX-License-Identifier: GPL-3.0-or-later
"""导出视频副本或单帧，不删除、不修改历史录制。"""
from dataclasses import replace
from types import SimpleNamespace

import cv2

from .analysis_io import load_session, read_jsonl, safe_media
from .config import Config
from .media import FrameSource
from .storage import SessionStore
from .video import VideoEncoder


def convert(session,output,codec='auto',crf=18):
    manifest,events=load_session(session)
    output=output.resolve()
    if output.is_relative_to(session.resolve()):
        raise ValueError('视频副本不能写入原始会话内部')
    frames=sorted(read_jsonl(safe_media(session,manifest['frames_path'])),key=lambda f:f['t_ns'])
    if not frames:
        raise ValueError('原会话没有可转换的图像')
    # 不虚构高帧率：旧录制只有约5Hz，转码不会凭空新增真实采样帧。
    fps=max(1,min(60,round(manifest['config'].get('sample_hz',5))))
    config=replace(Config(**manifest['config']),video_enabled=True,video_fps=fps,video_codec=codec,
                   video_crf=crf,all_frames=False,save_event_png=False)
    store=SessionStore(output,config.to_dict(),SimpleNamespace(origin_ns=manifest['clock']['origin_ns']),manifest['window'])
    store.manifest['derived_from']={'session_id':manifest['session_id'],'created_utc':manifest['created_utc'],
                                    'operation':'video_conversion','source_frames':len(frames)}
    encoder=VideoEncoder(store.path,fps,codec,crf)
    status,reason='failed','视频副本转换未完成'
    try:
        for event in events:
            store.event(event)
        with FrameSource(session) as source:
            for i,frame in enumerate(frames):
                image=source.read(frame)
                reference=encoder.write(image,frame['t_ns'])
                store.video_frame({**frame,'schema_version':'1.1',**reference})
                if i%100==0:
                    print(f'视频副本转换：{i+1}/{len(frames)}',flush=True)
        encoder.close()
        span=(frames[-1]['t_ns']-frames[0]['t_ns'])/1e9
        store.manifest['video']={'requested_fps':fps,'captured_frames':len(frames),'dropped_frames':0,
                                 'observed_fps_including_gaps':(len(frames)-1)/span if span else 0,
                                 'segments':encoder.segments}
        paths={safe_media(session,frame['path']) for frame in frames}
        source_bytes=sum(path.stat().st_size for path in paths)
        video_bytes=sum(s['bytes'] for s in encoder.segments)
        store.manifest['derived_from'].update(source_media_bytes=source_bytes,video_bytes=video_bytes)
        status,reason=manifest['status'],manifest.get('termination_reason','video_conversion')
        return store.path,source_bytes,video_bytes
    except Exception as error:
        reason=str(error)
        raise
    finally:
        try:
            encoder.close()
        finally:
            store.finish(status,reason,manifest.get('end_ns',frames[-1]['capture_end_ns']))


def extract(session,seconds,output):
    manifest,events=load_session(session)
    output=output.resolve()
    if output.is_relative_to(session.resolve()):
        raise ValueError('提取结果不能写入原始录制目录')
    if output.exists():
        raise ValueError('输出文件已存在，请选择新文件名')
    frames=read_jsonl(safe_media(session,manifest['frames_path']))
    if not frames or seconds<0:
        raise ValueError('录制没有画面，或提取时间无效')
    origin=next(e['t_ns'] for e in events if e['kind']=='recording_start')
    target=origin+int(seconds*1e9)
    frame=min(frames,key=lambda f:abs(f['t_ns']-target))
    with FrameSource(session) as source:
        image=source.read(frame)
    extension=output.suffix.lower()
    if extension not in ('.png','.jpg','.jpeg'):
        raise ValueError('输出图片必须使用 PNG 或 JPEG 扩展名')
    ok,data=cv2.imencode(extension,image)
    if not ok:
        raise RuntimeError('单帧图片编码失败')
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as handle:
        handle.write(data.tobytes())
    return frame
