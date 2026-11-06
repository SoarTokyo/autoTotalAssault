# SPDX-License-Identifier: GPL-3.0-or-later
"""仅凭下载的角色图识别局部卡面；不读取旧角色模板、战斗轴或历史答案。"""
import hashlib
from collections import Counter
import json
from pathlib import Path

import cv2
import numpy as np

from .analysis_io import safe_media


class CatalogCardReader:
    def __init__(self, manifest_path, *, names=None,content_ids=None,progress=None):
        paths=[Path(manifest_path)] if isinstance(manifest_path,(str,Path)) else list(map(Path,manifest_path))
        assets={}
        for path in paths:
            data=json.loads(path.read_text(encoding='utf-8'))
            if data.get('schema_version')!='1.0':raise ValueError('不支持的角色图库清单版本')
            seen=set()
            for asset in data['assets']:
                if asset['status']!='downloaded':continue
                ident=asset['content_id']
                if ident in seen:raise ValueError('图库中的学生条目 ID 重复')
                seen.add(ident)
                previous=assets.get(ident)
                if previous:
                    if previous[0]['sha256']!=asset['sha256']:
                        raise ValueError('同一学生在不同图库清单中的图片不一致，请核对图库')
                    asset=dict(asset,local_name=asset.get('local_name') or previous[0].get('local_name'))
                assets[ident]=(asset,path.resolve().parent)
        if content_ids is not None:
            wanted=set(content_ids)
            missing=wanted-set(assets)
            if missing:raise ValueError('本地图库缺少所选学生图片，条目 ID：'+','.join(map(str,sorted(missing))))
            assets={key:value for key,value in assets.items() if key in wanted}
        elif names:
            wanted=set(names)
            subset={key:value for key,value in assets.items() if (value[0].get('local_name') or value[0]['name']) in wanted}
            if len(subset)>=2:assets=subset
        self.sift = cv2.SIFT_create(nfeatures=1000, contrastThreshold=.012, edgeThreshold=15)
        self.matcher = cv2.BFMatcher(cv2.NORM_L2)
        self.references = []
        for number,(asset,root) in enumerate(assets.values(),1):
            raw = safe_media(root, asset['image']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != asset['sha256']:
                raise ValueError('图库图片校验失败，请重新下载或核对清单')
            image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise ValueError('无法解码角色图库图片')
            h, w = image.shape
            mask = np.full((h,w),255,np.uint8)
            # 网站头像两角为职能、星级，不能用通用角标判断角色。
            mask[:round(h*.22), :round(w*.24)] = 0
            mask[:round(h*.22), -round(w*.24):] = 0
            points, descriptors = self.sift.detectAndCompute(image, mask)
            if descriptors is None or len(points)<12:
                raise ValueError('图库图片的有效局部特征不足')
            self.references.append((asset, image.shape, points, descriptors))
            if progress and (number%25==0 or number==len(assets)):
                progress(f'图库特征索引：{number}/{len(assets)}',flush=True)
        if len(self.references)<2:
            raise ValueError('至少需要两名学生的参考图片，才能比较候选差距')
        self.index=None
        if len(self.references)>16:
            self.index=cv2.FlannBasedMatcher(dict(algorithm=1,trees=4),dict(checks=96))
            self.index.add([reference[3] for reference in self.references])
            self.index.train()

    def recognize(self, crop):
        if crop is None or not crop.size:
            return dict(valid=False, content_id=None, reason='empty_crop', candidates=[])
        gray = cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY) if crop.ndim==3 else crop
        # 小卡面先放大，减轻低分辨率对特征点检测的影响。
        gray = cv2.resize(gray,None,fx=3,fy=3,interpolation=cv2.INTER_CUBIC)
        mask = np.full(gray.shape,255,np.uint8)
        mask[:round(gray.shape[0]*.27), :round(gray.shape[1]*.3)] = 0
        points, descriptors = self.sift.detectAndCompute(gray,mask)
        candidates = []
        if descriptors is not None and len(points)>=12:
            references=self.references
            if self.index is not None:
                # 全库索引仅缩小候选；每个候选仍执行原来的局部匹配和几何验证。
                votes=Counter(match.imgIdx for pair in self.index.knnMatch(descriptors,k=2) for match in pair)
                references=[self.references[i] for i,_ in votes.most_common(12)]
            for asset, shape, ref_points, ref_descriptors in references:
                pairs = self.matcher.knnMatch(descriptors,ref_descriptors,k=2)
                good = [pair[0] for pair in pairs if len(pair)==2 and pair[0].distance < .75*pair[1].distance]
                result = dict(content_id=asset['content_id'], name=asset.get('local_name') or asset['name'],
                              matches=len(good), inliers=0, unique_points=0, inlier_ratio=0., coverage=0., geometry_valid=False)
                if len(good)>=4:
                    source = np.float32([points[m.queryIdx].pt for m in good])
                    target = np.float32([ref_points[m.trainIdx].pt for m in good])
                    homography, inliers = cv2.findHomography(source,target,cv2.RANSAC,5.)
                    if homography is not None and inliers is not None:
                        selected = source[inliers.ravel().astype(bool)]
                        result['inliers'] = len(selected)
                        result['inlier_ratio'] = len(selected)/len(good)
                        result['unique_points'] = len({tuple(np.round(p,1)) for p in selected})
                        if len(selected)>=3:
                            result['coverage'] = float(cv2.contourArea(cv2.convexHull(selected)) / (gray.shape[0]*gray.shape[1]))
                        corners = np.float32([[0,0],[gray.shape[1],0],[gray.shape[1],gray.shape[0]],[0,gray.shape[0]]]).reshape(-1,1,2)
                        projected = cv2.perspectiveTransform(corners,homography)
                        if np.all(np.isfinite(projected)):
                            area = abs(cv2.contourArea(projected)) / (shape[0]*shape[1])
                            center = projected.mean(axis=0)[0]
                            result['geometry_valid'] = bool(cv2.isContourConvex(projected) and .02<=area<=3 and
                                0<=center[0]<shape[1] and 0<=center[1]<shape[0])
                candidates.append(result)
        candidates.sort(key=lambda c:(c['inliers'],c['unique_points']),reverse=True)
        best = candidates[0] if candidates else None
        gap = best['inliers']-candidates[1]['inliers'] if len(candidates)>1 else 0
        valid = bool(best and best['geometry_valid'] and best['inliers']>=12 and best['unique_points']>=8 and
                     best['inlier_ratio']>=.4 and best['coverage']>=.04 and gap>=6)
        return dict(valid=valid, content_id=best['content_id'] if valid else None,
                    name=best['name'] if valid else None, reason='matched' if valid else 'insufficient_visual_evidence',
                    inlier_gap=gap, candidates=candidates)
