from stock_analysis import application as app
from stock_analysis.http import rendering
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from stock_analysis.domains.notes.investment_notes import InvestmentNotes, NoteConflict, digest


def save(notes, text, *, base=None):
    base = base or notes.read('300750', '宁德时代')
    return notes.save({**base, 'content': text, 'name': '宁德时代'})


def test_portable_unicode_note_read_has_no_side_effect_and_empty_is_valid(tmp_path):
    notes = InvestmentNotes(tmp_path)
    first = notes.read('300750', '宁德时代')
    assert not first['exists'] and not notes.root().exists()
    result = save(notes, '# 宁德时代\n\n## 2026-10-23\n\n观察 PE。\n')
    assert result['exists'] and result['file'] == 'sz300750_宁德时代.md'
    assert (notes.root() / result['file']).read_text(encoding='utf-8') == result['content']
    assert save(notes, '')['content'] == ''
    assert not (notes.root() / '.history').exists()


def test_external_change_with_same_mtime_and_size_is_not_overwritten(tmp_path):
    notes = InvestmentNotes(tmp_path)
    base = save(notes, 'AAAA')
    path = notes.root() / base['file']
    before = path.stat()
    path.write_text('BBBB', encoding='utf-8')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(NoteConflict) as conflict:
        save(notes, 'CCCC', base=base)
    assert conflict.value.current['content'] == 'BBBB'
    assert path.read_text() == 'BBBB'
    assert not (notes.root() / '.history').exists()


def test_existing_history_is_preserved_but_never_updated(tmp_path):
    notes = InvestmentNotes(tmp_path)
    folder = notes.root() / '.history' / 'sz300750'; folder.mkdir(parents=True)
    legacy = folder / 'latest.md'; legacy.write_bytes(b'legacy history')
    base = save(notes, 'one')
    save(notes, 'two', base=base)
    with pytest.raises(NoteConflict):
        save(notes, 'stale', base=base)
    assert legacy.read_bytes() == b'legacy history'
    assert list(folder.iterdir()) == [legacy]


def test_stale_tab_and_deleted_file_require_explicit_resolution(tmp_path):
    notes = InvestmentNotes(tmp_path)
    base = save(notes, 'first')
    current = save(notes, 'second', base=base)
    with pytest.raises(NoteConflict):
        save(notes, 'stale', base=base)
    (notes.root() / current['file']).unlink()
    with pytest.raises(NoteConflict):
        save(notes, 'restore', base=current)
    assert not (notes.root() / current['file']).exists()
    assert save(notes, 'restore', base=notes.read('300750'))['content'] == 'restore'


def test_atomic_replace_failure_keeps_original(tmp_path, monkeypatch):
    from stock_analysis.domains.notes import investment_notes as module
    notes = InvestmentNotes(tmp_path)
    base = save(notes, 'old')
    replace = module.os.replace
    def fail(source, target):
        if Path(target).name == base['file']:
            raise PermissionError('file busy')
        return replace(source, target)
    monkeypatch.setattr(module.os, 'replace', fail)
    with pytest.raises(PermissionError):
        save(notes, 'new', base=base)
    assert notes.read('300750')['content'] == 'old'
    assert not list(notes.root().glob('.note-*'))


def test_directory_copy_verifies_attachments_and_invalidates_old_requests(tmp_path):
    notes = InvestmentNotes(tmp_path / 'data')
    old = save(notes, '# original')
    attachment = notes.root() / 'attachments' / '图.png'
    attachment.parent.mkdir(); attachment.write_bytes(b'png')
    source = notes.root()
    preview = notes.preview_directory({'directory': str(tmp_path / 'vault'), 'mode': 'copy'})
    assert preview['files'] >= 2 and not preview['conflicts']
    notes.apply_directory({'token': preview['token']})
    assert notes.read('300750')['content'] == '# original'
    assert (notes.root() / 'attachments' / '图.png').read_bytes() == b'png'
    assert (source / old['file']).exists()
    with pytest.raises(NoteConflict):
        save(notes, 'stale path', base=old)


def test_directory_preview_detects_source_and_target_changes_and_nested_paths(tmp_path):
    notes = InvestmentNotes(tmp_path / 'data')
    base = save(notes, 'first')
    target = tmp_path / 'vault'
    for nested in (notes.root() / 'child', notes.root().parent):
        with pytest.raises(ValueError):
            notes.preview_directory({'directory': str(nested), 'mode': 'copy'})
    preview = notes.preview_directory({'directory': str(target), 'mode': 'copy'})
    save(notes, 'changed', base=base)
    with pytest.raises(NoteConflict):
        notes.apply_directory({'token': preview['token']})
    preview = notes.preview_directory({'directory': str(target), 'mode': 'use'})
    (target / 'external.md').write_text('changed')
    with pytest.raises(NoteConflict):
        notes.apply_directory({'token': preview['token']})
    assert notes.root() == tmp_path / 'data' / 'investment_notes'


def test_conflicting_names_and_duplicate_stock_identity_prevent_migration(tmp_path):
    notes = InvestmentNotes(tmp_path / 'data')
    current = save(notes, 'one')
    target = tmp_path / 'vault'; target.mkdir()
    (target / current['file']).write_text('different')
    preview = notes.preview_directory({'directory': str(target), 'mode': 'copy'})
    assert preview['conflicts']
    with pytest.raises(ValueError):
        notes.apply_directory({'token': preview['token']})
    (target / current['file']).unlink()
    (target / '300750_other.md').write_text('same stock different file')
    assert notes.preview_directory({'directory': str(target), 'mode': 'copy'})['conflicts']


def test_external_markdown_association_and_resource_boundary(tmp_path):
    notes = InvestmentNotes(tmp_path)
    notes.root().mkdir()
    path = notes.root() / '我的观察.md'; path.write_text('# 我的观察\n\n正文', encoding='utf-8')
    row = notes.listing()[0]
    assert row['code'] is None
    result = notes.associate({'file': row['file'], 'code': '300750', 'version': row['version']})
    assert '# 我的观察\n\n正文' in result['content'].replace('\r\n', '\n')
    assert result['file'] == row['file']
    for unsafe in ('../outside.png', '/absolute.png', '.history/x.md', 'C:relative.png', 'file:stream.png'):
        with pytest.raises(ValueError):
            notes.resource(unsafe)
    with pytest.raises(ValueError):
        save(notes, '---\nstock_code: "600887"\n---\nwrong stock', base=result)


def test_http_notes_session_auth_conflict_and_no_database_writes(tmp_path, monkeypatch):
    from stock_analysis.http import server
    monkeypatch.setattr(app.settings, 'DATABASE_PATH', tmp_path / 'unused.sqlite3')
    httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{httpd.server_port}'
    def request(path, data=None, token=None, method=None):
        headers = {'Origin': base, 'Content-Type': 'application/json'}
        if token: headers['X-Local-Session'] = token
        req = Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                      headers=headers, method=method)
        with urlopen(req, timeout=5) as response: return json.load(response)
    try:
        settings = request('/api/notes/settings')
        doc = request('/api/notes/document?code=300750')
        command = {**doc, 'content': '第一条笔记'}
        with pytest.raises(HTTPError) as error:
            request('/api/notes/document', command, method='PUT')
        assert error.value.code == 400
        request('/api/notes/document', command, settings['session_token'], 'PUT')
        with pytest.raises(HTTPError) as error:
            request('/api/notes/document', command, settings['session_token'], 'PUT')
        assert error.value.code == 409
        assert not app.settings.DATABASE_PATH.exists()
        assert request('/api/notes')['items'][0]['code'] == '300750'
        for path, body in [('/api/notes/history?code=300750', None), ('/api/notes/recovery', {'code': '300750', 'content': 'draft'})]:
            with pytest.raises(HTTPError) as removed:
                request(path, body, settings['session_token'])
            assert removed.value.code == 404
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=5)


def test_notes_in_real_browser(tmp_path, monkeypatch):
    from stock_analysis.http import server
    from stock_analysis.infrastructure.storage import Database
    from stock_analysis.domains.notes import note_snapshots
    edge = shutil.which('msedge') or r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
    node = shutil.which('node')
    if not Path(edge).is_file() or not node:
        pytest.skip('Edge and Node are needed')
    db = Database(tmp_path / 'isolated.sqlite3'); db.initialize(); db.ensure_instrument('300750', '测试企业')
    monkeypatch.setattr(app.settings, 'DATABASE_PATH', db.path)
    reports = [{'period': p, 'profit_ytd': v * 1e8, 'revenue_ytd': v * 10e8}
               for p, v in [('2025-03-31', 10), ('2025-06-30', 30), ('2026-03-31', 20), ('2026-06-30', 40)]]
    monkeypatch.setattr(note_snapshots.FundamentalService, 'read', lambda *a: {'reports': reports, 'name': '测试企业'})
    monkeypatch.setattr(note_snapshots.ValuationService, 'read', lambda *a: {'observation_rows': [
        {'date': '2026-09-01', 'pe': 20, 'pb': 2}, {'date': '2026-10-19', 'pe': 18, 'pb': 1.8},
        {'date': '2026-10-23', 'pe': 16, 'pb': 1.6}]})
    monkeypatch.setattr(note_snapshots.PriceService, 'read', lambda *a: [
        {'date': '2026-09-01', 'close': 100}, {'date': '2026-10-19', 'close': 50},
        {'date': '2026-10-23', 'close': 200}])
    def page(code, refresh):
        valuation_rows = [{'date': day, 'pe': pe, 'pb': pe / 10, 'ps': pe / 5, 'dividend_yield': 3,
                           'qfq_close': 100 + pe, 'qfq_close_date': day}
                          for day, pe in [('2026-09-01', 20), ('2026-10-19', 18), ('2026-10-23', 16)]]
        valuation_views = {str(year): {metric: {'rows_by_frequency': {frequency: valuation_rows
                           for frequency in ('day', 'week', 'month')}, 'count': 3, 'current': 16,
                           'current_date': '2026-10-23', 'percentile': 20}
                           for metric in ('pe', 'pb', 'ps', 'dividend_yield')} for year in (1, 3, 5, 10)}
        payload = {'code': code, 'name': '测试企业', 'views': {}, 'warnings': [], 'valuation': {'views': {}},
                   'loading': {}, 'price_version': 0, 'price_needs_update': False, 'cached_stocks': []}
        payload['valuation']['views'] = valuation_views
        return (rendering.TEMPLATE.replace('__RESEARCH_SIDEBAR__', rendering.research_sidebar('stock'))
                .replace('__PAYLOAD__', json.dumps(payload, ensure_ascii=False)).replace('__CODE__', code).replace('__ERROR__', ''))
    monkeypatch.setattr(rendering, 'render_page', page)
    httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True); thread.start()
    browser = None; profile = tmp_path / 'browser'
    try:
        with (tmp_path / 'edge.log').open('w') as log:
            browser = subprocess.Popen([edge, '--headless=new', '--disable-gpu', '--no-first-run',
                '--no-default-browser-check', '--remote-debugging-port=0', f'--user-data-dir={profile}', 'about:blank'],
                stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            endpoint = profile / 'DevToolsActivePort'; deadline = time.monotonic() + 15
            while not endpoint.exists():
                if browser.poll() is not None or time.monotonic() > deadline:
                    pytest.fail('Headless browser failed to start')
                time.sleep(.05)
            result = subprocess.run([node, str(Path(__file__).with_name('notes_browser.cjs')),
                endpoint.read_text().splitlines()[0], f'http://127.0.0.1:{httpd.server_port}',
                str(tmp_path / 'investment_notes'), str(tmp_path / 'vault'),
                str(tmp_path / 'notes-desktop.png'), str(tmp_path / 'notes-mobile.png')],
                capture_output=True, text=True, encoding='utf-8', timeout=90)
            assert result.returncode == 0, result.stdout + '\n' + result.stderr
    finally:
        if browser and browser.poll() is None:
            browser.terminate()
            try: browser.wait(timeout=5)
            except subprocess.TimeoutExpired: browser.kill(); browser.wait(timeout=5)
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=5)
