"""Validate HK F10 rows and map verified line items to shared view fields."""

from datetime import date

from stock_analysis.domains.stocks.source_parsers import _number

SOURCE = 'eastmoney:hk-f10-statements'
VERSION = 'hk_f10_statements_v1'
CATALOG = 'RPT_CUSTOM_HKSK_APPFN_CASHFLOW_SUMMARY'
REPORTS = {'lrb': 'RPT_HKF10_FN_INCOME_PC', 'fzb': 'RPT_HKF10_FN_BALANCE_PC',
           'llb': 'RPT_HKF10_FN_CASHFLOW_PC'}
CURRENCIES = {'人民币': 'CNY', '港元': 'HKD', '港币': 'HKD', '美元': 'USD',
              'CNY': 'CNY', 'HKD': 'HKD', 'USD': 'USD'}

FIELDS = {
    'fzb': {'004009999': 'TOTASSET', '004025999': 'TOTLIAB', '004036999': 'RIGHAGGR',
            '004002010': 'CURFDS', '004002003': 'ACCORECE', '004002001': 'INVE',
            '004001005': 'GOODWILL', '004001001': 'FIXEDASSECLEATOT',
            '004001004': 'INTAASSET', '004011010': 'SHORTTERMBORR',
            '004020001': 'LONGBORR', '004020005': 'LEASELIAB',
            '004001999': 'NONCASSTOTA', '004002999': 'TOTALCURRASSETS'},
    'lrb': {'004001999': 'BIZINCO', '004010999': 'PERPROFIT', '004011999': 'TOTPROFIT',
            '004012001': 'INCOTAXEXPE', '004012999': 'NETPROFIT', '004025001': 'MINYSHARRIGH',
            '004025002': 'PARENETP', '004027002': 'BASICEPS', '004027003': 'DILUTEDEPS',
            '004010003': 'SALESEXPE'},
    'llb': {'003999': 'MANANETR', '005999': 'INVNETCASHFLOW', '007999': 'FINNETCFLOW',
            '010999': 'CASHNETR', '011001': 'INICASHBALA', '011999': 'FINALCASHBALA',
            '005005': 'ACQUASSETCASH'},
}


def _day(value):
    return date.fromisoformat(str(value).split(' ', 1)[0]).isoformat()


def report_catalog(payload, code):
    result = payload.get('result') or {}
    entries = result.get('data')
    if payload.get('success') is not True or not isinstance(entries, list) or len(entries) != 1:
        raise ValueError('东财港股报告目录无有效数据')
    records = entries[0].get('REPORT_LIST')
    if not isinstance(records, list) or not records:
        raise ValueError('东财港股报告目录缺少报告期')
    catalog = {}
    for record in records:
        if record['SECUCODE'] != code + '.HK':
            raise ValueError('东财港股报告目录证券不一致')
        period = _day(record['REPORT_DATE'])
        start = _day(record['START_DATE'])
        if start > period:
            raise ValueError('东财港股报告期间无效')
        entry = {'start': start, 'currency': CURRENCIES.get(record['CURRENCY']),
                 'reported_currency': record['CURRENCY'], 'standard': record['ACCOUNT_STANDARD'],
                 'report_type': record['REPORT_TYPE'], 'name': record['SECURITY_NAME_ABBR'],
                 'fiscal_year_end': record['FISCAL_YEAR']}
        if period in catalog and catalog[period] != entry:
            raise ValueError('东财港股报告目录含重复口径')
        catalog[period] = entry
    return catalog


def statement_page(payload, code, periods, page, size):
    result = payload.get('result') or {}
    if payload.get('success') is not True or not isinstance(result.get('data'), list):
        raise ValueError('东财港股报表无有效分页')
    records = result['data']
    count, pages = int(result['count']), int(result['pages'])
    if (count <= 0 or pages != (count + size - 1) // size or page > pages
            or len(records) != min(size, count - (page - 1) * size)):
        raise ValueError('东财港股报表分页长度不符')
    for record in records:
        if record['SECUCODE'] != code + '.HK' or _day(record['REPORT_DATE']) not in periods:
            raise ValueError('东财港股报表证券或报告期不符')
    return count, pages, records


def normalize_statements(records, kind, catalog):
    reports, seen = {}, set()
    for record in records:
        period, field = _day(record['REPORT_DATE']), record['STD_ITEM_CODE']
        if not isinstance(field, str) or not field.isascii() or not field.isdigit() or (period, field) in seen:
            raise ValueError('东财港股报表字段缺失或重复')
        seen.add((period, field))
        meta = catalog[period]
        if kind != 'fzb' and _day(record['START_DATE']) != meta['start']:
            raise ValueError('东财港股报表起始日期不符')
        value = record['AMOUNT']
        if value is not None and _number(value) is None:
            raise ValueError('东财港股报表金额无效')
        if not record['STD_ITEM_NAME']:
            raise ValueError('东财港股报表字段名称为空')
        data = reports.setdefault(period, {'rCurrency': meta['currency'], 'rType': '合并期末',
            'statement_source': SOURCE, 'period_start': meta['start'], 'fiscal_year_end': meta['fiscal_year_end'],
            'accounting_standard': meta['standard'], 'report_type': meta['report_type'],
            'reported_currency': meta['reported_currency'], 'data': []})
        data['data'].append({'item_field': FIELDS[kind].get(field, field), 'source_field': field,
            'item_title': record['STD_ITEM_NAME'], 'item_source': kind, 'item_value': value,
            'source_date_type': record['DATE_TYPE_CODE']})
    return reports
