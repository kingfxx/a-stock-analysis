"""Decode the two Sina index responses without executing response scripts."""

from datetime import date, time
from pathlib import Path
import re


def parse_quote(raw, symbol):
    match = re.fullmatch(r'\s*var hq_str_' + re.escape(symbol) + r'="([^"]*)";\s*', raw.decode('gb18030'))
    values = match[1].split(',') if match else []
    if len(values) < 32 or values[0] not in ('创业板50', '创业板50指数'):
        raise ValueError('新浪实时行情代码或字段无效')
    observed = date.fromisoformat(values[30])
    if time.fromisoformat(values[31]) < time(15):
        raise ValueError('新浪实时行情尚未收盘')
    return dict(_code=symbol[2:], date=observed.isoformat(),
                open=values[1], close=values[3], high=values[4], low=values[5])


def parse_history(raw, symbol):
    from py_mini_racer import MiniRacer

    match = re.match(r'\s*var KLC_KL_' + re.escape(symbol) + r'\s*=\s*"([A-Za-z0-9+/=]+)";',
                     raw.decode('utf-8-sig'))
    if not match:
        raise ValueError('新浪历史行情代码或数据格式无效')
    decoder = MiniRacer()
    decoder.eval((Path(__file__).parent / 'resources/sina_index_history.js').read_text(encoding='utf-8'))
    rows = decoder.call('d', match[1])
    if not isinstance(rows, list) or not rows:
        raise ValueError('新浪历史行情为空')
    return [{**row, '_code': symbol[2:]} for row in rows]
