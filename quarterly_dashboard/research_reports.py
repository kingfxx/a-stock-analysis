"""Read HTML reports produced by the project's research skill."""
from datetime import datetime, timezone
import html
from pathlib import Path
import re
from urllib.parse import quote

from .sources import normalize_code

REPORT_ROOT = Path(__file__).resolve().parent.parent / 'data' / 'research_reports'


def resolve_report(relative, root=REPORT_ROOT):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('研报路径无效')
    parts = path.relative_to(root).parts
    if (len(parts) < 3 or not re.fullmatch(r'(?:sh|sz|bj)\d{6}', parts[0], re.I)
            or parts[-1] != 'report.html' or not path.is_file()):
        raise ValueError('研报不存在')
    return path


def list_reports(code, root=REPORT_ROOT):
    code = normalize_code(code)
    root = Path(root).resolve()
    reports = []
    if root.is_dir():
        for directory in root.iterdir():
            if not re.fullmatch(r'(?:sh|sz|bj)'+code, directory.name, re.I) or not directory.is_dir():
                continue
            for file in directory.rglob('report.html'):
                relative = file.relative_to(root).as_posix()
                try:
                    path = resolve_report(relative, root)
                except ValueError:
                    continue
                parts = Path(relative).parts
                with path.open(encoding='utf-8-sig') as stream:
                    heading = stream.read(16384)
                title = re.search(r'<title\b[^>]*>(.*?)</title>', heading, re.I | re.S)
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                reports.append({
                    'title': html.unescape(re.sub(r'<[^>]+>', '', title[1])).strip() if title else code+' AI 研报',
                    'date': parts[1],
                    'version': '/'.join(parts[2:-1]) or '首版',
                    'modified_at': modified,
                    'url': '/research-reports/'+quote(relative, safe='/'),
                })
    reports.sort(key=lambda item: (item['date'], item['modified_at'], item['version']), reverse=True)
    return {'code': code, 'reports': reports}
