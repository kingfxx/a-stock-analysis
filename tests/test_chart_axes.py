import shutil
import subprocess
from pathlib import Path

import pytest


def test_multiple_chart_axes_share_zero_and_keep_all_values_visible():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is needed to exercise the chart renderer')
    result = subprocess.run([node, str(Path(__file__).with_name('chart_axes.cjs'))],
                            input='{}', capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
