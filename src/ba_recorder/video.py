# SPDX-License-Identifier: GPL-3.0-or-later
"""有界异步视频编码；每个真实采样帧保留单调时间及视频位置映射。"""
from fractions import Fraction
import importlib.util
from pathlib import Path
import queue
import threading

from .storage import FrameBuffer


class VideoEncoder:
    def __init__(self, root, fps=30, codec='auto', crf=18):
        self.root, self.fps, self.codec, self.crf=Path(root),fps,codec,crf
        (self.root/'video').mkdir(exist_ok=True)
        self.backend=None
        self.writer=None
        self.container=None
        self.stream=None
        self.size=None
        self.path=None
        self.segments=[]
        self.last_image=None
        self.last_capture=None
        self.start_ns=None
        self.encoded_index=-1
        self.closed=False

    def _start(self, size, t_ns):
        import cv2
        self._close_segment()
        width,height=size
        self.size=size
        encoded_size=(width+(width%2),height+(height%2))
        relative=f'video/segment-{len(self.segments):04d}.mp4'
        path=self.root/relative
        if path.exists():
            raise FileExistsError('视频分段已存在，不能覆盖')
        candidates=['libx264','h264_msmf','mp4v'] if self.codec=='auto' else [self.codec]
        failures=[]
        for codec in candidates:
            if codec=='libx264':
                if importlib.util.find_spec('av') is None:
                    failures.append('未安装 PyAV')
                    continue
                try:
                    import av
                    self.container=av.open(str(path),'w',options={'movflags':'frag_keyframe+empty_moov+default_base_moof'})
                    self.stream=self.container.add_stream('libx264',rate=self.fps)
                    self.stream.width,self.stream.height=encoded_size
                    self.stream.pix_fmt='yuv420p'
                    self.stream.time_base=Fraction(1,1_000_000)
                    self.stream.codec_context.time_base=Fraction(1,1_000_000)
                    self.stream.codec_context.max_b_frames=0
                    self.stream.codec_context.thread_count=2
                    self.stream.codec_context.gop_size=self.fps
                    self.stream.options={'crf':str(self.crf),'preset':'veryfast','tune':'zerolatency'}
                    self.stream.codec_context.open()
                    self.backend='libx264'
                    break
                except Exception as error:
                    failures.append(f'PyAV H.264：{error}')
                    if self.container:
                        self.container.close()
                    self.container=self.stream=None
                    if path.exists():
                        path.unlink()
            else:
                api=cv2.CAP_MSMF if codec=='h264_msmf' else cv2.CAP_FFMPEG
                fourcc='H264' if codec=='h264_msmf' else 'mp4v'
                self.writer=cv2.VideoWriter(str(path),api,cv2.VideoWriter_fourcc(*fourcc),self.fps,encoded_size)
                if self.writer.isOpened():
                    self.backend=codec
                    break
                self.writer.release()
                self.writer=None
                failures.append(f'{codec} 无法打开')
                if path.exists():
                    path.unlink()
        else:
            raise RuntimeError('视频编码器不可用：'+'；'.join(failures))
        self.path=relative
        self.start_ns=t_ns
        self.encoded_index=-1
        self.last_image=None
        self.segments.append({'path':relative,'codec':self.backend,'content_width':width,'content_height':height,
                              'encoded_width':encoded_size[0],'encoded_height':encoded_size[1],
                              'start_ns':t_ns,'end_ns':t_ns,'captured_frames':0,'encoded_frames':0,
                              'repeated_playback_frames':0,'timing_mode':'variable_pts' if self.backend=='libx264' else 'cfr_with_capture_index'})

    def write(self, image, t_ns):
        import cv2
        if self.closed:
            raise RuntimeError('编码器已经关闭')
        height,width=image.shape[:2]
        size=(width,height)
        if self.size!=size or self.last_capture is None or t_ns-self.last_capture>500_000_000:
            self._start(size,t_ns)
        if self.last_capture is not None and t_ns<=self.last_capture:
            raise ValueError('视频采集时间必须递增')
        pad_bottom,pad_right=height%2,width%2
        padded=cv2.copyMakeBorder(image,0,pad_bottom,0,pad_right,cv2.BORDER_CONSTANT,value=(0,0,0)) if pad_bottom or pad_right else image
        segment=self.segments[-1]
        if self.backend=='libx264':
            import av
            frame=av.VideoFrame.from_ndarray(padded,format='bgr24')
            frame.pts=(t_ns-self.start_ns)//1000
            frame.time_base=Fraction(1,1_000_000)
            for packet in self.stream.encode(frame):
                self.container.mux(packet)
            self.encoded_index+=1
            video_time_ns=(t_ns-self.start_ns)//1000*1000
        else:
            target=max(self.encoded_index+1,round((t_ns-self.start_ns)*self.fps/1e9))
            # 短间隔用重复画面维持播放时长；它们不冒充真实采样帧。
            while self.last_image is not None and self.encoded_index+1<target:
                self.writer.write(self.last_image)
                self.encoded_index+=1
                segment['repeated_playback_frames']+=1
            self.writer.write(padded)
            self.encoded_index+=1
            video_time_ns=round(self.encoded_index*1e9/self.fps)
            self.last_image=padded
        segment.update(end_ns=t_ns,encoded_frames=self.encoded_index+1)
        segment['captured_frames']+=1
        self.last_capture=t_ns
        return {'path':self.path,'video_frame_index':self.encoded_index,'video_time_ns':video_time_ns,
                'content_width':width,'content_height':height}

    def _close_segment(self):
        if self.container:
            try:
                for packet in self.stream.encode():
                    self.container.mux(packet)
            finally:
                self.container.close()
                self.container=self.stream=None
        if self.writer:
            self.writer.release()
            self.writer=None
        if self.segments:
            path=self.root/self.segments[-1]['path']
            self.segments[-1]['bytes']=path.stat().st_size if path.exists() else 0
            if self.segments[-1]['captured_frames']:
                import cv2
                check=cv2.VideoCapture(str(path))
                try:
                    if not check.isOpened() or not check.read()[0]:
                        raise RuntimeError('编码后的视频无法解码，不能标记为完整录制')
                finally:
                    check.release()

    def close(self):
        if not self.closed:
            self.closed=True
            self._close_segment()


class VideoWorker:
    def __init__(self, store, config):
        self.store,self.config=store,config
        self.queue=queue.Queue(maxsize=config.video_queue_size)
        self.operations=queue.SimpleQueue()
        self.stopping=threading.Event()
        self.error=None
        self.written=0
        self.dropped=0
        self.first_ns=self.last_ns=None
        self.encoder=VideoEncoder(store.path,config.video_fps,config.video_codec,config.video_crf)
        self.png_buffer=FrameBuffer(int(config.pre_seconds*1e9),int(config.post_seconds*1e9),
                                   config.max_buffer_frames,store.extra_frame,config.all_frames)
        self.thread=threading.Thread(target=self._run,daemon=True,name='battle-video-encoder')
        self.thread.start()

    def submit(self, record, bgra):
        if self.error:
            raise RuntimeError(self.error)
        try:
            self.queue.put_nowait((record,bgra))
            return True
        except queue.Full:
            self.dropped+=1
            return False

    def operation(self, t_ns):
        if self.config.save_event_png or self.config.all_frames:
            self.operations.put(t_ns)

    def _run(self):
        import cv2
        import numpy as np
        next_png=0
        try:
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    record,bgra=self.queue.get(timeout=.05)
                except queue.Empty:
                    continue
                record=dict(record)
                if record.pop('segment_start',False):
                    self.encoder.last_capture=None
                image=cv2.cvtColor(np.asarray(bgra),cv2.COLOR_BGRA2BGR)
                reference=self.encoder.write(image,record['t_ns'])
                video_record={**record,'schema_version':'1.1',**reference}
                self.store.video_frame(video_record)
                self.written+=1
                self.first_ns=record['t_ns'] if self.first_ns is None else self.first_ns
                self.last_ns=record['t_ns']
                while not self.operations.empty():
                    self.png_buffer.operation(self.operations.get_nowait())
                if (self.config.save_event_png or self.config.all_frames) and record['t_ns']>=next_png:
                    ok,png=cv2.imencode('.png',image)
                    if not ok:
                        raise RuntimeError('操作截图编码失败')
                    png_record={**record,'schema_version':'1.0','path':f"frames/{int(record['id'][1:]):08d}.png"}
                    self.png_buffer.add(png_record,png.tobytes())
                    next_png=record['t_ns']+int(1e9/self.config.sample_hz)
            while not self.operations.empty():
                self.png_buffer.operation(self.operations.get_nowait())
        except Exception as error:
            self.error=f'视频编码失败：{type(error).__name__}：{error}'
        finally:
            try:
                self.encoder.close()
            except Exception as error:
                self.error=f'视频结束写入失败：{error}'

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=30)
        if self.thread.is_alive():
            raise RuntimeError('视频编码线程未及时退出，视频可能不完整')
        if self.error:
            raise RuntimeError(self.error)
        span=(self.last_ns-self.first_ns)/1e9 if self.written>1 else 0
        return {'requested_fps':self.config.video_fps,'captured_frames':self.written,
                'dropped_frames':self.dropped,'observed_fps_including_gaps':(self.written-1)/span if span else 0,
                'segments':self.encoder.segments}
