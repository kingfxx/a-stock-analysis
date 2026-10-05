import copy
import json
from pathlib import Path
import subprocess

from quarterly_dashboard import server
from quarterly_dashboard.valuation_transport import compact_valuation


def decode_in_browser(payload):
    page = (Path(__file__).resolve().parents[1]/'web'/'index.html').read_text(encoding='utf-8')
    decoder = page[page.index('  function expandValuationPayload('):page.index('  const state =')]
    script = decoder + "\nconst data=JSON.parse(require('fs').readFileSync(0,'utf8')); const decoded=expandValuationPayload(data); console.log(JSON.stringify(expandValuationPayload(decoded)));"
    result = subprocess.run(['node', '-e', script], input=json.dumps(payload),
                            capture_output=True, text=True, encoding='utf-8', check=True)
    return json.loads(result.stdout)


def test_chart_transport_round_trip_preserves_all_metrics_frequencies_and_metadata():
    dates = ['2018-09-30','2022-09-30','2024-09-30','2026-09-28','2026-09-29','2026-09-30']
    data = {'observation_rows': [dict(date=day, pe=10+i, pb=2+i, ps=3+i,
             pe_date=day, pb_date=day, ps_date=day) for i,day in enumerate(dates)]}
    raw = [{'date':day,'close':20+i} for i,day in enumerate(dates)]
    qfq = [{'date':day,'close':10+i} for i,day in enumerate(dates[:-1])]
    payload = server._p4_valuation_payload(data, raw=raw, qfq=qfq,
                                           events=[{'date':'2026-07-15','per_share':1}])
    original = copy.deepcopy(payload)
    compact = compact_valuation(payload)
    assert payload == original
    assert decode_in_browser(compact) == original
    assert len(json.dumps(compact)) < len(json.dumps(original))*.6
    assert compact['views']['3']['pe']['row_indices'] == compact['views']['3']['pb']['row_indices']


def test_legacy_and_empty_payloads_round_trip():
    for payload in ({'views':{}}, {'views':{'10':{'pe':{'rows':[{'date':'2026-09-30','pe':0}], 'percentile':None}}}}):
        assert decode_in_browser(compact_valuation(payload)) == payload
        assert decode_in_browser(payload) == payload
