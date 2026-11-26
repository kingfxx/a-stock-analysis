"""Identity for the shared Shanghai, Shenzhen and Hong Kong equity entry points."""

import re


def stock_identity(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError('请输入股票代码')
    match = re.fullmatch(r'(sh|sz|hk)?([0-9]{5,6})(?:\.(sh|sz|hk))?', value.strip().lower())
    if match:
        prefix, code, suffix = match.groups()
        if len(code) == 5:
            exchange = 'hk'
        elif code[0] in '036':
            exchange = 'sh' if code[0] == '6' else 'sz'
        else:
            exchange = None
        if exchange and prefix in (None, exchange) and suffix in (None, exchange):
            return exchange, code
    raise ValueError('请输入沪深 A 股六位代码或港股五位代码，例如 601919、01919')
