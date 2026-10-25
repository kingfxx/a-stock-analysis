"""Read-only note fragments for one explicitly selected index and source series."""
from datetime import date, datetime
from zoneinfo import ZoneInfo
import json
import math

from stock_analysis.domains.notes.investment_notes import note_code
from stock_analysis.domains.indices.index_definitions import METRICS
from stock_analysis.domains.indices.index_read_service import IndexReadService
from stock_analysis.domains.valuation.valuation import valuation_summary_observations
from stock_analysis.domains.notes.note_snapshots import number, valid, percentile_position

LABELS = dict(pe_source='来源披露 PE', pe_static='静态 PE', pe_dynamic='预测动态 PE',
              pe_ttm='TTM PE', pe_rolling='滚动 PE', pb='PB', ps='PS', ps_ttm='TTM PS', dividend_yield='股息率')


def catalog(db, command):
    code = note_code(command.get('code', ''))
    identity = code.removeprefix('index:')
    group = command.get('group', 'index_valuation')
    if group not in ('index_valuation', 'index_crowding', 'index_spread'):
        raise ValueError('指数笔记数据分组无效')
    years = int(command.get('years', 10))
    if years not in (3, 5, 10):
        raise ValueError('历史区间无效')
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    requested = command.get('as_of')
    if requested is not None:
        try:
            if not isinstance(requested, str) or date.fromisoformat(requested).isoformat() != requested or requested > today:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError('数据日期须为有效 YYYY-MM-DD 且不能晚于今天') from None
    with db.connection() as conn:
        entity = conn.execute('SELECT name FROM index_catalog WHERE index_code=? ORDER BY id DESC LIMIT 1', (identity,)).fetchone()
        if not entity:
            raise ValueError('本地目录中没有该指数')
        rows = []
        if group == 'index_valuation':
            provider, series, definition = (command.get(key) for key in ('provider', 'series', 'definition'))
            if not provider or not series or not definition:
                raise ValueError('请选择指数估值来源及口径')
            definition_row = conn.execute('SELECT definition_json FROM index_metric_definitions WHERE provider=? AND series_key=? AND definition_version=?', (provider, series, definition)).fetchone()
            if not definition_row:
                raise ValueError('指数估值来源或口径无效')
            fields = set(json.loads(definition_row[0]).get('fields', {}).values())
            metrics = [key for key in METRICS if key in fields]
            rows = [dict(row) for row in conn.execute(
                'SELECT o.*,s.frequency FROM index_valuation_observations o JOIN index_valuation_series s ON s.id=o.series_id '
                'WHERE s.index_code=? AND s.provider=? AND s.series_key=? AND s.definition_version=? AND o.is_latest=1 ORDER BY o.observation_date',
                (identity, provider, series, definition))]
            source = f'{provider} / {series} / {definition}'
    reader = IndexReadService(db)
    try:
        overlay = json.loads(reader.price_overlay(identity, field='amount_ratio' if group == 'index_crowding' else 'close'))
        if group == 'index_spread':
            daily = json.loads(reader.equity_bond_spread(identity))
            rows = daily['rows']
            source = 'csindex / history.rolling / v1；同日中国十年期国债'
        elif group == 'index_crowding':
            rows = overlay['rows']
            source = f"{overlay.get('provider', '')} / 指数日行情 amount_ratio"
    finally:
        reader.close()
    rows = [row for row in rows if row['observation_date'] <= today]
    price_rows = [row for row in overlay.get('rows', []) if row['observation_date'] <= today]
    relevant = metrics if group == 'index_valuation' else ['amount_ratio'] if group == 'index_crowding' else ['spread']
    dates = {row['observation_date'] for row in rows if any(valid(row.get(key)) for key in relevant)}
    if group == 'index_valuation':
        dates.update(row['observation_date'] for row in price_rows if valid(row.get('close')))
    day = requested or (max(dates) if dates else today)
    result = dict(code=code, name=entity[0], group=group, items=[], periods=[], as_of=day,
                  available_dates=sorted(dates), has_date_data=day in dates,
                  captured_at=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S'))
    selected = next((row for row in rows if row['observation_date'] == day), {})

    def add(key, label, value, unit, basis='', position=None):
        item = dict(id=key, label=label, value=number(value, unit), date=day, available=valid(value),
                    basis=f'{source}；{basis}；当前保存版本，缺失不沿用其他日期', source=source)
        if position:
            item.update(position)
        result['items'].append(item)

    def percentile(key, label, value, observations, *, target=None, reverse=False, crowding=False):
        if target:
            prepared = [{'date': row['observation_date'], target: row.get(key), 'frequency': row.get('frequency')} for row in observations if row['observation_date'] <= day]
            if prepared and all(row['frequency'] == 'monthly' for row in prepared):
                monthly = {row['date'][:7]: row for row in prepared}
                prepared = list(monthly.values())
            summary = valuation_summary_observations(prepared, target, years, {}, as_of=day)
            rank = summary['percentile'] if valid(value) and (value >= 0 if target == 'dividend_yield' else value > 0) else None
            count = summary['count']
        else:
            end = date.fromisoformat(day)
            try:
                start = end.replace(year=end.year-years).isoformat()
            except ValueError:
                start = end.replace(year=end.year-years, day=28).isoformat()
            values = [row[key] for row in observations if start <= row['observation_date'] <= day and valid(row.get(key))]
            count = len(values)
            rank = math.floor(1000*sum(item < value for item in values)/count+.5)/10 if count and valid(value) else None
        position = percentile_position(rank, 'dividend_yield' if reverse else 'pe')
        if group == 'index_spread' and position:
            position['position']['label'] = {'低息': '低位', '高息': '高位', '中值': '适中'}.get(position['position']['label'], position['position']['label'])
        if crowding and position:
            position['position']['label'] = {'低位': '低拥挤', '中值': '适中', '高位': '拥挤', '极高': '极拥挤'}.get(position['position']['label'], position['position']['label'])
        add(key+'_percentile', f'{label}近{years}年历史分位', rank, '%', f'截至{day}，{count} 个有效样本', position)

    if group == 'index_valuation':
        for metric in metrics:
            target = 'pe' if metric.startswith('pe_') else 'ps' if metric.startswith('ps') else metric
            value = selected.get(metric)
            if not valid(value) or (value < 0 if target == 'dividend_yield' else value <= 0):
                value = None
            add(metric, LABELS[metric], value, '%' if target == 'dividend_yield' else '倍')
            percentile(metric, LABELS[metric], value, rows, target=target, reverse=target == 'dividend_yield')
        point = next((row for row in price_rows if row['observation_date'] == day), {})
        add('index_close', '指数收盘点位', point.get('close'), '点', f"点位来源 {overlay.get('provider', '未记录')} / {overlay.get('return_type', '')}")
    elif group == 'index_crowding':
        add('amount_ratio', '指数拥挤度', selected.get('amount_ratio'), '%', '指数成交额占全市场成交额比例，非大盘前5%个股集中度')
        percentile('amount_ratio', '指数拥挤度', selected.get('amount_ratio'), rows, crowding=True)
        add('index_close', '指数收盘点位', selected.get('close'), '点')
    else:
        add('spread', '股债利差', selected.get('spread'), '%', '100÷滚动PE－同日中国十年期国债收益率，零及负值保留')
        percentile('spread', '股债利差', selected.get('spread'), rows, reverse=True)
        add('pe_rolling', '滚动 PE', selected.get('pe_rolling'), '倍')
        add('china_10y', '中国十年期国债收益率', selected.get('china_10y'), '%')
    return result
