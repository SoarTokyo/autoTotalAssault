# SPDX-License-Identifier: GPL-3.0-or-later
"""共享学生英文名称索引；供搜索与素材文件命名使用。"""
from functools import lru_cache
import json
import re
import unicodedata
from pathlib import Path
from .paths import PROJECT_ROOT


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKC',text).casefold() if c.isalnum())


def base_name(text):return re.split(r'[（(]',text)[0].strip()


@lru_cache(maxsize=8)
def name_index(root):
    root=Path(root);full={};bases={}
    for filename in ('student_names.json','student_names_supplement.json'):
        path=root/filename
        if not path.exists():continue
        for row in json.loads(path.read_text(encoding='utf-8'))['students']:
            if 'name' in row:
                en=row['name'].get('en');names=[row['name'].get('cn')]
            else:en=row.get('en');names=[row.get('cn'),row.get('tw')]
            for cn in names:
                if cn and en:
                    full[normalize(cn)]=en;bases[normalize(base_name(cn))]=base_name(en)
    path=root/'student_name_aliases.json'
    aliases=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    return full,bases,{normalize(k):v for k,v in aliases.get('base_names',{}).items()},aliases.get('forms',{})


def resolve_name(cn,names_root=None):
    full,bases,extra,forms=name_index(str(Path(names_root or PROJECT_ROOT/'examples').resolve()))
    base=base_name(cn);english=full.get(normalize(cn)) or ''
    words=extra.get(normalize(base),[]) or ([bases[normalize(base)]] if normalize(base) in bases else [])
    form=cn[len(base):].strip('()（）')
    if (not english or re.search(r'[\u4e00-\u9fff]',english)) and words:
        translated=forms.get(form,[])
        english=words[0]+(f' ({translated[0] if translated else form})' if form else '')
    if not english and cn.isascii() and re.search(r'[A-Za-z]',cn):english=cn
    previous_english=unicodedata.normalize('NFKC',english)
    english=canonical_english_name(previous_english)
    search=[previous_english,*[word for key,values in forms.items() if key in cn for word in values]]
    return english,words,search


def canonical_english_name(name):
    """规范显示名称；不改学生 ID、已有素材路径或历史轴。旧拼写仍可检索。"""
    replacements={'Walnut':'Kurumi','Cocoro':'Kokoro','Cheongsam':'Qipao',
                  'Battle':'Armed','Part-Time':'Part-Timer','Part Timer':'Part-Timer',
                  'Pajama':'Pajamas'}
    for old,new in replacements.items():
        name=re.sub(r'(?<![\w-])'+re.escape(old)+r'(?![\w-])',new,name,flags=re.I)
    return re.sub(r'(?<!Pop )\bIdol\b','Pop Idol',name,flags=re.I)


def image_filename(name,suffix='.png'):
    english=resolve_name(name)[0]
    if not english or re.search(r'[\u4e00-\u9fff]',english):
        raise ValueError(f'学生“{name}”缺少完整英文名称，请先补充名称索引')
    stem=re.sub(r'[<>:"/\\|?*\x00-\x1f]','-',english)
    stem=re.sub(r'\s+',' ',stem).strip(' .')
    if not stem or stem.upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        raise ValueError('英文名称不能作为 Windows 文件名')
    return stem+suffix.lower()
