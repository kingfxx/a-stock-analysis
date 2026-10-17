from pathlib import Path
import shutil
import subprocess

import pytest


def test_beijing_time_and_cooldown_presentation():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for time presentation checks')
    run = subprocess.run([node, str(Path(__file__).with_name('beijing_time.cjs'))],
                         capture_output=True, text=True, encoding='utf-8')
    assert run.returncode == 0, run.stdout + run.stderr
