"""Narrow, evidence-backed corrections to identified published snapshots.

The underlying observations remain untouched. A correction applies only to the
exact snapshot whose inputs were checked, not to later revisions or other dates.
"""

_BYD_2025_Q2 = {
    'security_code': '002594',
    'exchange': 'sz',
    'trade_date': '2025-06-30',
    'definition_version': 'em-valueanalysis-v1',
    'total_shares': 5_494_665_855,
    'close_price': 331.91,
    'total_market_cap': 1_823_734_543_933.05,
}
_REPORT_URL = 'https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=11424525&stockid=002594'


def corrected_market_cap(row):
    """Return the verified period-end estimate and provenance, or None."""
    if any(row[key] != value for key, value in _BYD_2025_Q2.items()):
        return None
    shares = 3_039_065_855
    return {
        'value': row['close_price'] * shares,
        'shares': shares,
        'price': row['close_price'],
        'original_value': row['total_market_cap'],
        'original_shares': row['total_shares'],
        'report_url': _REPORT_URL,
        'reason': '2025年半年报披露期末总股本30.39065855亿股，期后送转不应计入该期股本。',
    }
