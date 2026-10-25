"""Local OCR for archived MOT image tables; no network or paid model calls."""
from io import BytesIO
import json
from pathlib import Path
import subprocess
import tempfile
import re
import sys
import hashlib
from functools import lru_cache
from threading import Lock
from PIL import Image, ImageDraw, ImageOps


def grid_lines(image):
    gray=ImageOps.grayscale(image)
    width,height=gray.size
    def groups(values):
        result=[]
        for value in values:
            if result and value-result[-1][-1]<=2:result[-1].append(value)
            else:result.append([value])
        return [sum(g)/len(g) for g in result]
    pixels=gray.load()
    xs=groups([x for x in range(width) if sum(pixels[x,y]<110 for y in range(height))/height>.65])
    ys=groups([y for y in range(height) if sum(pixels[x,y]<110 for x in range(width))/width>.65])
    if len(xs)!=6 or not 8<=len(ys)<=30:
        raise ValueError('图片未识别到完整五列表格网格，留待核验')
    return xs,ys


def recognize_native(raw,language='zh-Hans-CN',scale=3):
    with Image.open(BytesIO(raw)) as original:
        original.load()
        if original.width*original.height>12_000_000:raise ValueError('OCR图片过大')
        image=original.convert('RGB')
    xs,ys=grid_lines(image)
    # Remove only table rules; preserve the source image and original cell positions.
    draw=ImageDraw.Draw(image)
    for x in xs:draw.line((int(x),0,int(x),image.height),fill='white',width=3)
    for y in ys:draw.line((0,int(y),image.width,int(y)),fill='white',width=3)
    image=image.resize((image.width*scale,image.height*scale),Image.Resampling.LANCZOS)
    script=Path(__file__).with_name('resources')/'shipping_ocr.ps1'
    with tempfile.TemporaryDirectory(prefix='shipping-ocr-') as folder:
        file=Path(folder)/'table.png';image.save(file)
        try:
            result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',str(script),
                '-ImagePath',str(file),'-Language',language],capture_output=True,timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
        except (OSError,subprocess.SubprocessError) as exc:
            raise ValueError('本地Windows OCR不可用，保留其他识别结果交叉核验') from exc
    if result.returncode:
        raise ValueError('本地Windows OCR不可用或识别失败，图片保留待核验')
    output=result.stdout.decode('utf-8-sig')
    # Ignore host startup diagnostics before the single JSON payload.
    payload=json.loads(output[output.index('{'):])
    cells=[]
    for top,bottom in zip(ys,ys[1:]):
        row=[]
        for left,right in zip(xs,xs[1:]):
            words=[w for w in payload['words'] if left<(w['x']+w['width']/2)/scale<right
                   and top<(w['y']+w['height']/2)/scale<bottom]
            words.sort(key=lambda w:w['x'])
            row.append(''.join(w['text'] for w in words))
        cells.append(row)
    return dict(language=language,scale=scale,cells=cells)


_LOCK=Lock()
ROUTES={'综合','欧洲','地中海','美西','美东','波红','澳新','西非','南非','南美','日本','东南亚','韩国','香港','台湾','东西非'}


@lru_cache(maxsize=1)
def engine():
    runtime=Path(__file__).resolve().parents[1]/'data/runtime/shipping-ocr'
    if runtime.is_dir() and str(runtime) not in sys.path:
        sys.path.insert(0,str(runtime))
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise ValueError('本地OCR依赖未安装，参见requirements-shipping-ocr.txt；图片保留待核验') from exc
    return RapidOCR(intra_op_num_threads=1,inter_op_num_threads=1)


def recognize_rapid(image,xs,ys,scale):
    import numpy as np
    resized=image.resize((image.width*scale,image.height*scale),Image.Resampling.BICUBIC)
    result,_=engine()(np.asarray(resized))
    cells=[[[] for _ in range(5)] for _ in range(len(ys)-1)]
    for box,text,score in result or []:
        x=sum(p[0] for p in box)/len(box)/scale
        y=sum(p[1] for p in box)/len(box)/scale
        col=next((i for i,(a,b) in enumerate(zip(xs,xs[1:])) if a<x<b),None)
        row=next((i for i,(a,b) in enumerate(zip(ys,ys[1:])) if a<y<b),None)
        if row is not None and col is not None:
            cells[row][col].append(dict(text=text,score=float(score)))
    return cells


def route_name(text):
    name=re.sub(r'\s+','',text).replace('航线','')
    return '综合' if name=='综合指数' else name


@lru_cache(maxsize=1)
def model_hashes():
    import rapidocr_onnxruntime
    models=Path(rapidocr_onnxruntime.__file__).parent/'models'
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in models.glob('*.onnx')}


def extract_month_table(raw,month):
    with Image.open(BytesIO(raw)) as opened:
        opened.load()
        if opened.width*opened.height>12_000_000:raise ValueError('OCR图片过大')
        image=opened.convert('RGB')
    xs,ys=grid_lines(image)
    with _LOCK:
        gray=ImageOps.grayscale(image).convert('RGB')
        scans=[recognize_rapid(image,xs,ys,3),recognize_rapid(gray,xs,ys,2),
               recognize_rapid(gray,xs,ys,3),recognize_rapid(gray,xs,ys,4)]
    try:
        native=recognize_native(raw)['cells']
    except ValueError:
        native=None  # RapidOCR can still pass if its route labels are unambiguous.
    year,m=map(int,month.split('-'))
    headers=[''.join(token['text'] for cell in scan[0] for token in cell) for scan in scans]
    if not any(str(year-1) in h and str(year) in h and re.search(rf'(?<!\d){m}月',h) for h in headers):
        raise ValueError('OCR表头年份/月度与目标不一致或不清晰，留待核验')
    rows=[]
    for r in range(1,len(ys)-1):
        names=set()
        for scan in scans:
            cell=scan[r][0]
            if len(cell)==1 and cell[0]['score']>=.9:
                name=route_name(cell[0]['text'])
                if name in ROUTES:names.add(name)
        if native and len(native)==len(ys)-1:
            name=route_name(native[r][0])
            if name in ROUTES:names.add(name)
        if len(names)!=1:
            raise ValueError(f'OCR第{r}行航线不确定或有冲突，留待核验')
        values=[]
        for col in range(1,5):
            candidates=[]
            pattern=r'\d{1,5}\.\d{2}' if col<4 else r'[+-]?\d{1,3}\.\d{1,2}'
            for scan in scans:
                cell=scan[r][col]
                if len(cell)==1 and cell[0]['score']>=.9:
                    text=re.sub(r'\s+','',cell[0]['text']).replace('−','-').replace('－','-')
                    if re.fullmatch(pattern,text):candidates.append(float(text))
            if len(candidates)<2 or len(set(candidates))!=1:
                raise ValueError(f'OCR第{r}行第{col+1}列多次识别不一致或缺值，留待核验')
            if col<4 and candidates[0]<=0:raise ValueError('OCR指数非正，留待核验')
            values.append(candidates[0])
        rows.append(dict(zip(('route','annual','ytd','monthly','change'),[next(iter(names)),*values])))
    if len({r['route'] for r in rows})!=len(rows) or not any(r['route']=='综合' for r in rows):
        raise ValueError('OCR航线重复或缺综合指数，留待核验')
    evidence=dict(extractor='mot-local-ocr-v1',variants=['color3','gray2','gray3','gray4'],numeric_min_score=.9,
        native_labels=native is not None,rows=rows,
        model_hashes=model_hashes())
    return rows,evidence
