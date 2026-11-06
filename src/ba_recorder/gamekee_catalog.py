# SPDX-License-Identifier: GPL-3.0-or-later
"""读取实装学生栏目并保存独立本地素材；不将网站头像直接认定为技能阶段。"""
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import cv2
import numpy as np
from .student_names import image_filename,resolve_name
from .catalog_files import image_owners


INDEX_URL = 'https://www.gamekee.com/ba/second/23941'
STUDENT_PID = 49443
CATALOG_URL = f'https://www.gamekee.com/v1/entry/treesByPid?pid={STUDENT_PID}'
MAX_BYTES = 12 * 1024 * 1024
CDN_HOSTS = {'cdnimg.gamekee.com', 'cdnimg-v2.gamekee.com'}


def image_url(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('学生条目缺少图片地址')
    parts = urlsplit('https:'+value if value.startswith('//') else value)
    if parts.scheme not in ('http','https') or parts.hostname not in CDN_HOSTS or parts.username or parts.password or parts.port:
        raise ValueError('学生图片地址不属于已知 GameKee 图片站点')
    return urlunsplit(('https', parts.netloc, parts.path, parts.query, ''))


def parse_catalog(payload):
    """严格读取该栏目直接子条目，不递归遍历 NPC 或其他分类。"""
    if not isinstance(payload, dict) or type(payload.get('code')) is not int or payload['code'] != 0:
        raise ValueError('GameKee 图鉴接口返回异常，未取得学生目录')
    if not isinstance(payload.get('data'), list):
        raise ValueError('学生目录结构已变化：data 应为数组，请停止下载并核对接口')
    rows, seen = [], set()
    for item in payload['data']:
        if not isinstance(item, dict):
            raise ValueError('学生目录条目结构无效')
        if 'pid' in item and str(item['pid']) != str(STUDENT_PID):
            continue
        content_id = item.get('content_id')
        if not str(content_id).isdigit() or int(content_id) <= 0:
            continue
        name = str(item.get('name') or item.get('title') or '').strip()
        if not name:
            continue
        ident = int(content_id)
        if ident in seen:
            continue
        seen.add(ident)
        rows.append(dict(entry_id=item.get('id'), content_id=ident, category_pid=STUDENT_PID,
                         name=name, image_url=image_url(item.get('icon')),
                         page_url=f'https://www.gamekee.com/ba/{ident}'))
    if not rows:
        raise ValueError('没有得到实装学生图片条目，未继续下载')
    return rows


def select_team(rows, team):
    if not isinstance(team, dict) or team.get('schema_version') != '1.0' or not isinstance(team.get('students'),list) or not team['students']:
        raise ValueError('学生名称映射配置无效')
    selected, seen = [], set()
    by_id = {row['content_id']:row for row in rows}
    for entry in team['students']:
        ident = entry.get('gamekee_content_id') if isinstance(entry,dict) else None
        if type(ident) is not int or ident <= 0 or ident in seen:
            raise ValueError('学生名称映射中的网站条目 ID 无效或重复')
        seen.add(ident)
        name = entry.get('local_name')
        if not isinstance(name,str) or not name.strip():
            raise ValueError('学生名称映射缺少本地名称')
        if ident not in by_id:
            raise ValueError(f'实装学生目录中没有条目 {ident}：{name}，未用同名学生替代')
        row = by_id[ident]
        if row['name'] != entry.get('gamekee_name'):
            raise ValueError(f'网站条目 {ident} 名称已变化：{row["name"]}；请核对映射后再下载')
        selected.append(dict(row,local_name=name,aliases=entry.get('aliases',[])))
    return selected


def fetch_bytes(url, *, catalog=False):
    headers = {'User-Agent':'BA-Battle-Recorder/0.1 (local asset research)', 'Referer':INDEX_URL}
    if catalog:
        headers.update({'game-alias':'ba', 'device-num':'1', 'Accept':'application/json'})
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        final = urlsplit(response.geturl())
        allowed = {'www.gamekee.com'} if catalog else CDN_HOSTS
        if final.scheme != 'https' or final.hostname not in allowed:
            raise ValueError('资源跳转到了未知站点，已停止读取')
        data = response.read(MAX_BYTES+1)
    if len(data) > MAX_BYTES:
        raise ValueError('单个资源超过大小限制，未保存')
    return data


def decode_image(data):
    if not data:
        raise ValueError('下载内容为空')
    image = cv2.imdecode(np.frombuffer(data,dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('下载内容不是可解码图片，可能为错误页或验证页面')
    if min(image.shape[:2]) < 40:
        raise ValueError('图片尺寸过小，不适合作为卡牌识别参考')
    return image


def download(output, *, names=(), limit=6, all_students=False, catalog_json=None, team=None,
             fetcher=fetch_bytes, progress=print, batch_size=5, batch_delay=30., request_delay=1.):
    if type(batch_size) is not int or batch_size<1:
        raise ValueError('每组下载数量必须是正整数')
    for value in (batch_delay,request_delay):
        if type(value) not in (int,float) or not math.isfinite(value) or value<0:
            raise ValueError('下载等待秒数必须是非负有限数值')
    output = Path(output).resolve()
    payload = (json.loads(Path(catalog_json).read_text(encoding='utf-8')) if catalog_json else
               json.loads(fetcher(CATALOG_URL,catalog=True).decode('utf-8')))
    rows = parse_catalog(payload)
    if team is not None:
        selected = select_team(rows,team)
    elif names:
        wanted = set(names)
        selected = [row for row in rows if row['name'] in wanted]
        missing = wanted - {row['name'] for row in selected}
        if missing:
            raise ValueError('目录中没有找到以下名称：'+'、'.join(sorted(missing)))
    elif all_students:
        selected = rows
    else:
        if type(limit) is not int or limit < 1:
            raise ValueError('下载数量必须为正整数')
        selected = rows[:limit]
    output.mkdir(parents=True, exist_ok=True)
    owners=image_owners(output)
    # 每次独立清单；不覆盖上次来源和校验记录。
    from uuid import uuid4
    manifest_path = output / ('manifest-'+uuid4().hex[:12]+'.json')
    manifest = dict(schema_version='1.0', source=INDEX_URL, catalog_url=CATALOG_URL,
                    category='实装学生', category_pid=STUDENT_PID, catalog_count=len(rows),
                    downloaded_at_ns=time.time_ns(), assets=[], requested_count=len(selected),
                    batch_size=batch_size,batch_delay_seconds=batch_delay,request_delay_seconds=request_delay,
                    usage_note='保存本地学生图库供比对；不凭卡面推断技能阶段，不扩展为素材再分发授权。')
    manifest['image_naming']='english-v1'
    request_count=0
    stop_requested=False
    for index, row in enumerate(selected):
        asset = dict(row, status='failed')
        display_name = (row['local_name']+' → '+row['name']) if row.get('local_name') else row['name']
        try:
            suffix = Path(urlsplit(row['image_url']).path).suffix.lower()
            if suffix not in ('.png','.jpg','.jpeg','.webp'):
                suffix = '.img'
            relative = 'images/'+image_filename(row['name'],suffix)
            owner=owners.get(relative.casefold())
            if owner is not None and owner[0]!=row['content_id']:
                raise ValueError('学生英文名称与另一条目重名，请补全名称区别，未覆盖已有图片')
            path = output / relative
            if path.exists():
                data = path.read_bytes()
                if owner is None or hashlib.sha256(data).hexdigest()!=owner[1]:
                    raise ValueError('已有同名图片缺少归属或校验不符，未覆盖')
                if owner[2] and owner[2]!=row['image_url']:
                    raise ValueError('同名学生的图片来源已更新，请先核对图库版本；未用旧图冒充新来源，也未覆盖旧图')
            else:
                if request_count:
                    wait=batch_delay if request_count%batch_size==0 else request_delay
                    if wait:
                        progress(f'已请求 {request_count} 张图片，等待 {wait:g} 秒后继续……')
                        time.sleep(wait)
                request_count+=1
                data = fetcher(row['image_url'])
            image = decode_image(data)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                partial=path.with_suffix(path.suffix+'.partial')
                with partial.open('wb') as handle:
                    handle.write(data)
                partial.replace(path)
            asset.update(status='downloaded', image=relative, width=image.shape[1], height=image.shape[0],
                         sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),english_name=resolve_name(row['name'])[0])
            owners[relative.casefold()]=(row['content_id'],asset['sha256'],row['image_url'])
            progress(f'[{index+1}/{len(selected)}] 已保存：{display_name}（{image.shape[1]}×{image.shape[0]}）')
        except HTTPError as error:
            asset.update(error=str(error),http_status=error.code)
            stop_requested=error.code in (403,429)
            progress(f'[{index+1}/{len(selected)}] 网站返回 HTTP {error.code}：{display_name}')
            if stop_requested:
                manifest['termination_reason']='server_refused_or_rate_limited'
                if error.headers and error.headers.get('Retry-After'):
                    manifest['retry_after']=error.headers['Retry-After']
                progress('网站拒绝访问或要求限流，已停止后续请求；请稍后再试。')
        except (OSError, ValueError, cv2.error) as error:
            asset['error'] = str(error)
            progress(f'[{index+1}/{len(selected)}] 下载失败：{display_name}：{error}')
        manifest['assets'].append(asset)
        temporary = manifest_path.with_suffix('.json.partial')
        temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(manifest_path)
        if stop_requested:
            break
    return manifest_path, manifest
