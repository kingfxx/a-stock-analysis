"""Compare browser and Python engines with the same units and assumptions."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from stock_analysis.domains.valuation.dcf import calculate_dcf, calculate_growth_dcf


@pytest.mark.parametrize('base,stages,rate,terminal', [
    (1e8, [15, 8, 4], 9, 2),
    (123.456, [-2.5, .75, -1.2], -.5, -2),
    (0, [15, 8, 4], 9, 2),
    (-1e8, [15, 8, 4], 9, 2),
])
def test_python_browser_growth_results_match(base, stages, rate, terminal):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the browser calculation comparison')
    args = [base, *stages, rate, terminal]
    script = "const d=require('./web/features/valuation/dcf.js');console.log(JSON.stringify(d.calculateGrowth(...JSON.parse(process.argv[1]),1e8,10)));"
    run = subprocess.run([node, '-e', script, json.dumps(args)], cwd=Path(__file__).resolve().parents[1],
                         capture_output=True, text=True, check=True)
    browser = json.loads(run.stdout)
    python = calculate_growth_dcf(*args, shares=1e8, price=10)
    assert 'error' not in browser
    for py_key, js_key in [('total_value', 'total'), ('explicit_value', 'explicit'),
                           ('terminal_value', 'terminalValue'), ('terminal_present_value', 'terminalPV'),
                           ('per_share', 'perShare')]:
        assert python[py_key] == pytest.approx(browser[js_key], rel=1e-12)
    assert [r['fcf'] for r in python['rows']] == pytest.approx([r['fcf'] for r in browser['rows']], rel=1e-12)
    if python['reference_difference_pct'] is None:
        assert browser['priceGap'] is None
    else:
        assert python['reference_difference_pct'] / 100 == pytest.approx(browser['priceGap'], rel=1e-12)


def test_equal_stages_agree_with_standard_dcf():
    standard = calculate_dcf(1e8, 3, 9, 2, shares=1e8, price=10)
    growth = calculate_growth_dcf(1e8, 3, 3, 3, 9, 2, shares=1e8, price=10)
    assert growth['total_value'] == pytest.approx(standard['total_value'], rel=1e-12)
