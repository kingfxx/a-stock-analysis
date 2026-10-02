import shutil
import subprocess
from pathlib import Path
import pytest


def test_product_table_formats_without_stock_specific_logic():
    node=shutil.which('node')
    if not node:pytest.skip('Node.js unavailable')
    result=subprocess.run([node,str(Path(__file__).with_name('checklist_product_tables.cjs'))],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stdout+result.stderr
