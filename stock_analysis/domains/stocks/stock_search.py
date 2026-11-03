"""Bounded offline conversion and startup preparation of stock search keys."""
import json
import os
from pathlib import Path
import subprocess
import sys


def generate_keys(names):
    if not names:
        return []
    if len(names) > 2000 or any(not isinstance(name, str) or len(name) > 1000 for name in names):
        raise ValueError('股票检索名称超出转换范围')
    if not any(names):
        return [('', '') for _ in names]
    payload = json.dumps(names, ensure_ascii=False).encode('utf-8')
    if len(payload) > 1048576:
        raise ValueError('股票检索名称批次过大')
    # Execute the file directly so the worker loads no application package graph.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('stock_search_worker.py'))],
            input=payload, capture_output=True, timeout=30, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if len(result.stdout) > 8388608:
            raise ValueError('Oversized worker result')
        rows = json.loads(result.stdout.decode('utf-8'))
        if (not isinstance(rows, list) or len(rows) != len(names)
                or any(not isinstance(row, list) or len(row) != 2
                       or any(not isinstance(value, str) or len(value) > 16000 for value in row)
                       for row in rows)):
            raise ValueError('Invalid worker result')
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError) as exc:
        raise OSError('股票拼音检索生成失败，请重试；名称和检索字段未发布') from exc
    return rows


def prepare_search(db):
    # All queries/atomic writes are owned by Database; no SQLite or storage import here.
    while True:
        rows = db.pending_search_keys(2000)
        if not rows:
            return
        names = list(dict.fromkeys(row['name'] or '' for row in rows))
        keys = dict(zip(names, generate_keys(names)))
        db.publish_search_keys([(row['id'], row['name'], *keys[row['name'] or '']) for row in rows])
