"""Explicit image transcription review; archived image remains the fact's source."""
from calendar import monthrange
from datetime import date
from pathlib import Path
from urllib.parse import urljoin
import json
import re
import hashlib
from .collection import read_source,archive_source,parsed_source,finish_batch,encoded
from .shipping_forecast import publish_rates
from .shipping_sources import mot_body


def import_review(db,file):
    file=Path(file).resolve()
    if file.suffix.lower()!='.json' or file.stat().st_size>100000:
        raise ValueError('核验文件须为小于100KB的JSON')
    raw=file.read_bytes();review=json.loads(raw)
    if not isinstance(review,dict) or set(review)!={'image_source_id','html_source_id','month','published_on','rows','verified'} or review['verified'] is not True:
        raise ValueError('核验格式无效，须显式verified=true')
    image,image_source=read_source(db,int(review['image_source_id']))
    html,html_source=read_source(db,int(review['html_source_id']))
    if image_source['dataset']!='shipping.mot_image' or html_source['dataset']!='shipping.mot_rates' or image_source['batch_id']!=html_source['batch_id']:
        raise ValueError('图片和HTML来源身份或批次不匹配')
    if not image.startswith((b'\x89PNG',b'\xff\xd8',b'GIF8',b'RIFF')):
        raise ValueError('原件不是可识别的数据图片')
    soup,body=mot_body(html.decode('utf8'))
    html_url=json.loads(html_source['request_json']).get('url','')
    image_url=json.loads(image_source['request_json']).get('url','')
    if image_url not in [urljoin(html_url,i['src']) for i in body.find_all('img',src=True)]:
        raise ValueError('图片未被该HTML正文引用')
    month=review['month'];y,m=map(int,month.split('-'));date(y,m,1)
    title=soup.title.get_text() if soup.title else ''
    if not re.search(f'{y}年\\s*{m}月.*中国出口集装箱运价指数',title):
        raise ValueError('核验月份与HTML标题不匹配')
    if review['published_on'] not in soup.get_text():raise ValueError('核验披露日与HTML不匹配')
    date.fromisoformat(review['published_on'])
    rows=review['rows']
    if not isinstance(rows,list) or not rows or len(rows)>30 or not any(r.get('route')=='综合' for r in rows):
        raise ValueError('核验行缺综合指数或行数无效')
    points=[]
    for r in rows:
        if set(r)!={'route','annual','ytd','monthly','change'} or not isinstance(r['route'],str) or not r['route'].strip():
            raise ValueError('核验行字段无效')
        for stat,start,end,value in [('annual_mean',f'{y-1}-01-01',f'{y-1}-12-31',r['annual']),
            ('ytd_mean',f'{y}-01-01',f'{month}-{monthrange(y,m)[1]}',r['ytd']),
            ('monthly_mean',month+'-01',f'{month}-{monthrange(y,m)[1]}',r['monthly'])]:
            if isinstance(value,bool) or not isinstance(value,(int,float)):raise ValueError('核验值必须是数字')
            if r['change'] is not None and (isinstance(r['change'],bool) or not isinstance(r['change'],(int,float))):raise ValueError('变动值须为数字或空')
            points.append(dict(index_name='CCFI',frequency='monthly',route=r['route'],statistic=stat,period_start=start,period_end=end,
                value=value,unit='点',change_value=r['change'] if stat=='monthly_mean' else None,
                change_kind='percent' if stat=='monthly_mean' else None,published_on=review['published_on'],quality='official',channel='mot_sse'))
    # The review document is evidence, not a replacement for the original image.
    sha=hashlib.sha256(raw).hexdigest()
    with db.connection() as conn:
        existing=conn.execute("SELECT id FROM collection_sources WHERE batch_id=? AND dataset='shipping.image_review' AND raw_sha256=? AND parse_status='complete' LIMIT 1",(image_source['batch_id'],sha)).fetchone()
    audit=existing['id'] if existing else archive_source(db,image_source['batch_id'],'mot','shipping.image_review',
        dict(transport='explicit_verified_transcription',image_source_id=image_source['id'],review_sha256=sha),1,raw=raw)
    count=publish_rates(db,points,image_source['batch_id'],image_source['id'])
    parsed_source(db,audit,fields=['verified_rows'])
    parsed_source(db,image_source['id'],fields=['verified_rows','review_source_id:'+str(audit)])
    parsed_source(db,html_source['id'],fields=['image_reference'])
    finish_batch(db,image_source['batch_id'],'complete','partial',result=dict(month=month,published=count,review_source_id=audit,note='仅人工核验行；未提供完整行清单时不宣称全航线覆盖'))
    return dict(published=count,review_source_id=audit)
