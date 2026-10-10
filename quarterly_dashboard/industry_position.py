"""Read public industry facts only during explicit business generation."""
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from zoneinfo import ZoneInfo

import requests

from .storage import utc_now

PAGE = 'https://alphaliner.axsmarine.com/PublicTop100/'
ENDPOINT = PAGE + 'rooter.php'
QUERY = [{'action': 'top100', 'method': name, 'data': [], 'type': 'rpc', 'tid': i}
         for i, name in enumerate(('getTop100Table', 'getTextCalculations'), 1)]


def parse(payload, *, captured_at):
    results = {item['method']: item['result'] for item in payload
               if item.get('action') == 'top100' and item.get('type') == 'rpc'}
    rows = sorted((row for row in results['getTop100Table'] if str(row.get('rank', '')).isdigit()), key=lambda row: int(row['rank']))
    top = []
    for rank, row in enumerate(rows[:5], 1):
        if int(row['rank']) != rank or not 0 < float(row['percent']) <= 100 or int(row['totalTeu']) <= 0:
            raise ValueError('行业排行数据无效')
        top.append({'rank': rank, 'name': row['operator'], 'teu': int(row['totalTeu']),
                    'share_pct': float(row['percent'])})
    if len(top) != 5:
        raise ValueError('行业排行缺少前五名')
    company = next(row for row in top if row['name'] == 'COSCO Group')
    total = int(results['getTextCalculations']['active']['sum_teu'])
    if total <= company['teu']:
        raise ValueError('行业总运力无效')
    return {'market': '全球集装箱班轮运营运力', 'basis': '运营船舶TEU份额；集团合并口径，订单运力不计入现有份额',
            'company': company, 'top': top, 'global_teu': total,
            'cr3_pct': float(sum(Decimal(str(row['share_pct'])) for row in top[:3])),
            'cr5_pct': float(sum(Decimal(str(row['share_pct'])) for row in top)),
            'captured_at': captured_at, 'statistics_date': None,
            'date_note': '当前公开排行接口快照；接口未提供独立统计日期，页面标题由浏览器当天日期生成。',
            'scope_note': 'COSCO Group按来源定义合并，不直接等同中远海控上市公司合并范围；非营收或货量份额。',
            'source': PAGE, 'endpoint': ENDPOINT,
            'group_components': next(row.get('tooltip', '') for row in rows if row['operator'] == 'COSCO Group')}


def collect(input_data, cancelled):
    # First explicit adapter; do not apply liner TEU to other shipping businesses.
    if input_data['instrument']['code'] != '601919' or cancelled.is_set():
        return None
    with requests.Session() as session:
        response = session.post(ENDPOINT, json=QUERY, timeout=(5, 15))
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError('行业来源响应过大')
        value = parse(response.json(), captured_at=utc_now())
        value['response_sha256'] = sha256(response.content).hexdigest()
    if cancelled.is_set():
        return None
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    return {'id': 'industry.position', 'metric': 'industry_position', 'observed_on': today,
            'source': PAGE, 'value': value,
            'methodology': '直接读取公开网页的数据接口；CR3/CR5为已四舍五入份额相加的约数。'}
