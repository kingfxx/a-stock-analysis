"""Strict parsers for the free shipping releases; no network or model calls."""
from calendar import monthrange
from datetime import date
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup

SSE = 'https://www.sse.net.cn/index/singleIndex?indexType='
MOT = 'https://www.mot.gov.cn/shuju/yunjiazhishu/chukoujizhuangxiangyjzs/'


def number(text):
    text = re.sub(r'[\s,，％%]', '', text).replace('−', '-').replace('－', '-')
    return float(text) if re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text) else None


def cells(row):
    return [c.get_text(' ', strip=True) for c in row.find_all(['td', 'th'], recursive=False)]


def parse_sse(html, index):
    soup = BeautifulSoup(html, 'html.parser')
    for table in soup.find_all('table'):
        text = table.get_text(' ', strip=True)
        dates = re.findall(r'\d{4}-\d{2}-\d{2}', text)
        if '本期' not in text or len(dates) < 2:
            continue
        previous, current = dates[:2]
        if date.fromisoformat(previous) >= date.fromisoformat(current):
            raise ValueError('周度上期/本期日期无效')
        points = []
        for tr in table.find_all('tr'):
            row = cells(tr)
            if len(row) < 4 or not row[0] or '分航线' in row[0]:
                continue
            # Free CCFI has name/previous/current/change; SCFI adds unit/weight.
            a, b, change = [number(v) for v in row[-3:]]
            if a is None and b is None:
                continue
            route = '综合' if '综合指数' in row[0] else re.sub(r'[A-Za-z].*', '', row[0]).strip()
            if not route or a is None or b is None or a <= 0 or b <= 0:
                raise ValueError('周度航线不完整或指数非正')
            kind = 'percent' if index == 'CCFI' else 'points'
            expected = (b / a - 1) * 100 if kind == 'percent' else b - a
            if change is not None and abs(expected-change) > (0.12 if kind == 'percent' else .025):
                raise ValueError('公布变动与两期原值不符')
            for day, value in [(previous, a), (current, b)]:
                points.append(dict(index_name=index, frequency='weekly', route=route, statistic='release',
                    period_start=day, period_end=day, value=value, unit='点' if index == 'CCFI' or route == '综合' else row[1],
                    change_value=change if day == current else None, change_kind=kind if day == current else None,
                    published_on=day, quality='official', channel='sse'))
        if not any(p['route'] == '综合' for p in points):
            raise ValueError('周度缺综合指数')
        return points
    raise ValueError('未找到带真实日期的周度指数表')


def discover_mot(html, base=MOT):
    soup = BeautifulSoup(html, 'html.parser')
    found = {}
    for a in soup.find_all('a', href=True):
        title = a.get_text('', strip=True)
        match = re.search(r'(20\d{2})年\s*(\d{1,2})月.*(中国出口集装箱运价指数|集装箱运输市场分析)', title)
        if match:
            month = f'{match[1]}-{int(match[2]):02d}'
            kind = 'analysis' if '分析' in title else 'rates'
            found[month, kind] = urljoin(base, a['href'])
    return found


def mot_body(html):
    soup = BeautifulSoup(html, 'html.parser')
    return soup, soup.select_one('.TRS_Editor, .view TRS_Editor, #zoom, .article-content') or soup


class ImageTableRequired(ValueError):
    pass


def mot_month_metadata(html,month):
    soup,body=mot_body(html)
    title=soup.title.get_text() if soup.title else soup.get_text(' ',strip=True)[:200]
    year,m=map(int,month.split('-'))
    if not re.search(f'{year}年\\s*{m}月.*中国出口集装箱运价指数',title):
        raise ValueError('月度标题与目标月份不一致')
    pub=re.search(r'(20\d{2}-\d{2}-\d{2})',soup.get_text(' ',strip=True))
    if not pub:raise ValueError('月报缺真实发布时间')
    date.fromisoformat(pub[1])
    return body,pub[1]


def monthly_points(rows,month,published):
    import math
    year,m=map(int,month.split('-'));end=f'{month}-{monthrange(year,m)[1]}'
    points=[];names=set()
    for r in rows:
        route=r['route']
        if not route or route in names:raise ValueError('月表航线名缺失或重复')
        names.add(route)
        for key in ('annual','ytd','monthly'):
            if r[key] is None or isinstance(r[key],bool) or not math.isfinite(r[key]) or r[key]<=0:
                raise ValueError('月度值或年度参考值缺失/无效')
        if r['change'] is not None and not math.isfinite(r['change']):raise ValueError('公布环比无效')
        for stat,start,finish,value in [('annual_mean',f'{year-1}-01-01',f'{year-1}-12-31',r['annual']),
            ('ytd_mean',f'{year}-01-01',end,r['ytd']),('monthly_mean',month+'-01',end,r['monthly'])]:
            points.append(dict(index_name='CCFI',frequency='monthly',route=route,statistic=stat,
                period_start=start,period_end=finish,value=value,unit='点',
                change_value=r['change'] if stat=='monthly_mean' else None,
                change_kind='percent' if stat=='monthly_mean' else None,published_on=published,
                quality='official',channel='mot_sse'))
    if '综合' not in names:raise ValueError('月表缺综合指数')
    return points


def parse_mot(html,month):
    body,published=mot_month_metadata(html,month)
    for table in body.find_all('table'):
        text=table.get_text(' ',strip=True)
        if '综合指数' not in text or '至今' not in text:continue
        rows=[]
        for tr in table.find_all('tr'):
            cells_row=cells(tr)
            if len(cells_row)!=5:continue
            values=[number(c) for c in cells_row[1:]]
            if values[2] is None:continue
            route='综合' if '综合指数' in cells_row[0] else cells_row[0].replace('航线','').strip()
            rows.append(dict(zip(('route','annual','ytd','monthly','change'),[route,*values])))
        if rows:return monthly_points(rows,month,published)
    if body.find('img',src=True):
        raise ImageTableRequired('月表为图片：进入本地OCR并校验')
    raise ValueError('月表结构未知，保留来源待核验')


class NoThroughputDisclosure(ValueError):
    pass


def parse_throughput(html, report_month):
    soup, body = mot_body(html)
    text = re.sub(r'\s+', '', body.get_text()).replace('％','%')
    pub = re.search(r'(20\d{2}-\d{2}-\d{2})', soup.get_text(' ', strip=True))
    year,m=map(int,report_month.split('-'))
    title=soup.title.get_text() if soup.title else ''
    if not pub or not re.search(f'{year}年\\s*{m}月份?.*集装箱.*分析',title):
        raise ValueError('市场分析标题、正文或真实发布时间无效')
    date.fromisoformat(pub[1])
    volume=r'(全国|沿海|内河)(规模以上|主要)?港口(?:完成)?集装箱吞吐量(?:共计)?([\d,.]+)万(?:标准箱|TEU)'
    nationals=list(re.finditer(volume,text,re.I))
    nationals=[match for match in nationals if match[1]=='全国']
    if not nationals:
        if len(text)<300:raise ValueError('市场分析正文过短，不能判定未披露')
        if re.search(r'集装箱吞吐量[^。；]{0,40}\d[\d,.]*万',text):
            raise ValueError('存在吞吐量数值但统计范围或结构未识别，保留待核验')
        raise NoThroughputDisclosure('该市场分析未披露可提取的集装箱吞吐量数值，不填补缺值')
    # Resolve the closest explicit period before each national observation. A short month
    # inherits only a preceding explicit year/month context, never the URL or publication year.
    period_pattern=r'(?:(20\d{2})年)?(?:1[至\-~～—](\d{1,2})月|前(\d{1,2})个?月|(全年)|(\d{1,2})月份?|(第[一1]季度))'
    results = []
    for index,national in enumerate(nationals):
        prefix=text[:national.start()]
        dates=list(re.finditer(period_pattern,prefix))
        if not dates or national.start()-dates[-1].end()>350:
            raise ValueError('未找到吞吐量数值对应的明确统计期')
        latest=dates[-1];explicit=[d for d in dates if d[1]]
        if not explicit:raise ValueError('吞吐量所属年缺少正文依据')
        y=int(latest[1] or explicit[-1][1]);metric_month=int(latest[2] or latest[3] or (12 if latest[4] else 3 if latest[6] else latest[5]))
        date(y,metric_month,1)
        period=f'{y}-{metric_month:02d}'
        if period>report_month:raise ValueError('吞吐量统计期晚于报告月')
        basis='annual' if latest[4] else 'ytd' if latest[2] or latest[3] or latest[6] else 'monthly'
        end=nationals[index+1].start() if index+1<len(nationals) else len(text)
        segment=text[national.start():min(end,national.start()+500)]
        matches=list(re.finditer(volume,segment,re.I))
        for pos,match in enumerate(matches):
            tail=segment[match.end():matches[pos+1].start() if pos+1<len(matches) else match.end()+65]
            tail=re.split('[。；;②③]',tail)[0]
            change=re.search(r'(?:同比|较(?:去年|上年|20\d{2}年)(?:同期)?)(?:大幅|小幅|微幅)?(增长|上升|下降|减少|下跌|微跌|微增|微涨|上涨)([\d.]+)%',tail)
            yoy=None
            if change:yoy=(-1 if change[1] in ('下降','减少','下跌','微跌') else 1)*float(change[2])
            elif re.search(r'同比(?:基本)?持平',tail):yoy=0.0
            elif '同比' in tail or re.search('较.*同期',tail):raise ValueError('吞吐量同比表达未识别')
            results.append(dict(metric='container_throughput',scope=match[1]+(match[2] or ''),period=period,
                report_month=report_month,basis=basis,value=number(match[3]),unit='万TEU',
                yoy=yoy,published_on=pub[1]))
    keys=[(r['scope'],r['period'],r['basis']) for r in results]
    if len(keys)!=len(set(keys)):raise ValueError('吞吐量统计期或范围重复，保留待核验')
    return results
