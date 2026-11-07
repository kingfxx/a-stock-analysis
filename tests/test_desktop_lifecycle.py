"""Offline lifecycle checks; no real sources, desktop or daily database writes."""
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time
from types import SimpleNamespace
import urllib.request

import pytest

from stock_analysis import runtime
from stock_analysis.runtime_control import RuntimeControl
from stock_analysis.maintenance.database_restore import RequestGate


def ready_control(activity=lambda: False):
    control = RuntimeControl()
    server = SimpleNamespace(service_actions=lambda: None, shutdown=Event().set,
                             request_gate=RequestGate())
    control.bind(server, activity)
    assert control.snapshot()[0] == 'starting'
    server.service_actions()
    assert control.snapshot()[0] == 'running'
    return control, server


def test_stop_is_rejected_before_loop_and_while_requests_or_restore_are_active():
    control = RuntimeControl()
    assert control.request_stop()
    control, server = ready_control()
    assert server.request_gate.enter()
    assert control.request_stop()
    assert not server.request_gate.blocked
    server.request_gate.leave()
    server.request_gate.blocked = True
    assert control.request_stop()
    assert control.snapshot()[0] == 'running'


def test_stop_freezes_admissions_before_check_and_rolls_back_if_busy_or_error():
    control, server = ready_control()
    def busy():
        assert not server.request_gate.enter()
        return True
    control.activity_check = busy
    with pytest.raises(ValueError, match='尚未完成'):
        control.request_stop()
    assert not server.request_gate.blocked
    control.activity_check = lambda: (_ for _ in ()).throw(OSError('unavailable'))
    with pytest.raises(OSError):
        control.request_stop()
    assert not server.request_gate.blocked


def test_stop_once_and_restore_rebind_uses_new_listener():
    control, first = ready_control()
    control.detach(restoring=True)
    assert control.request_stop()
    control, second = rebind(control)
    shutdown = Event()
    second.shutdown = shutdown.set
    assert control.request_stop() == ''
    assert control.request_stop() == ''
    assert second.request_gate.blocked and not first.request_gate.blocked
    second.service_actions()
    assert shutdown.wait(2)
    control.finish()
    assert control.snapshot() == ('stopped', '')


def rebind(control):
    server = SimpleNamespace(service_actions=lambda: None, shutdown=lambda: None, request_gate=RequestGate())
    control.bind(server, lambda: False)
    server.service_actions()
    return control, server


def test_real_listener_stops_without_shutdown_deadlock(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'instance_lock', lambda path: nullcontext())
    monkeypatch.setattr(runtime, 'SourceArchiveService', lambda *args: None)
    counts = []
    class Scheduler:
        def __init__(self, *args, **kwargs): counts.append('created')
        def start(self): counts.append('started')
        def close(self): counts.append('closed')
    monkeypatch.setattr(runtime, 'Scheduler', Scheduler)
    control = RuntimeControl()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
        def log_message(self, *args): pass
    # Check and reserve a free explicit port before the integration worker starts.
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    def run():
        runtime.run_server(port, False, database_path=tmp_path/'unused.sqlite3',
            handler=Handler, server_factory=ThreadingHTTPServer, initialize=lambda *a, **k: None,
            activity_check=lambda: False, management_factory=lambda db: None,
            clear_state=lambda: None, open_url=lambda url: pytest.fail('not ready'), control=control)
        control.finish()
    worker = Thread(target=run)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while control.snapshot()[0] != 'running' and time.monotonic() < deadline:
            time.sleep(.02)
        assert control.snapshot()[0] == 'running'
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=2) as response:
            assert response.read() == b'ok'
        assert not control.request_stop()
        worker.join(5)
        assert not worker.is_alive()
        assert counts == ['created', 'started', 'closed']
    finally:
        if worker.is_alive():
            control.server.shutdown()
            worker.join(5)


def test_cleanup_still_closes_listener_and_reader_after_scheduler_error(tmp_path, monkeypatch):
    events = []
    server = SimpleNamespace(serve_forever=lambda: None,
        server_close=lambda: events.append('server'),
        index_reader=SimpleNamespace(close=lambda: events.append('reader')))
    class Scheduler:
        def __init__(self, *a, **k): pass
        def start(self): pass
        def close(self): raise RuntimeError('scheduler failure')
    monkeypatch.setattr(runtime, 'Scheduler', Scheduler)
    monkeypatch.setattr(runtime, 'SourceArchiveService', lambda *a: None)
    monkeypatch.setattr(runtime, 'instance_lock', lambda path: nullcontext())
    with pytest.raises(RuntimeError, match='scheduler failure'):
        runtime.run_server(8768, False, database_path=tmp_path/'unused.sqlite3',
            handler=None, server_factory=lambda *a: server, initialize=lambda *a, **k: None,
            activity_check=lambda: False, management_factory=lambda db: None,
            clear_state=lambda: None, open_url=lambda url: None)
    assert events == ['server', 'reader']


@pytest.mark.skipif(os.name != 'nt', reason='Windows kernel objects')
def test_windows_secondary_activation_and_crash_release(tmp_path):
    from stock_analysis.desktop.windows_instance import DesktopInstance
    identity = tmp_path/'desktop.sqlite3'
    code = ('from stock_analysis.desktop.windows_instance import DesktopInstance; import sys; '
            'i=DesktopInstance(sys.argv[1]); print(i.primary); i.activate(); i.close()')
    with DesktopInstance(identity) as primary:
        assert primary.primary
        result = subprocess.run([sys.executable, '-c', code, str(identity)],
                                capture_output=True, text=True, check=True, timeout=10)
        assert result.stdout.strip() == 'False'
        assert primary.wait(1000)
        assert not primary.wait(10)
    crash = ('from stock_analysis.desktop.windows_instance import DesktopInstance; import sys,os; '
             'i=DesktopInstance(sys.argv[1]); print(i.primary,flush=True); os._exit(0)')
    result = subprocess.run([sys.executable, '-c', crash, str(identity)],
                            capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == 'True'
    with DesktopInstance(identity) as next_instance:
        assert next_instance.primary


def test_log_streams_rotation_and_exception_hooks(tmp_path, monkeypatch):
    from stock_analysis.desktop.logging_setup import configure_logs
    from stock_analysis.desktop import windows_instance
    import threading
    log_errors = []
    monkeypatch.setattr(windows_instance, 'show_error',
                        lambda message: log_errors.append(f'{message}: {sys.exc_info()[1]}'))
    # configure_logs intentionally replaces process streams/hooks; restore after test.
    for module, attr in [(sys,'stdout'), (sys,'stderr'), (sys,'excepthook'), (threading,'excepthook')]:
        monkeypatch.setattr(module, attr, getattr(module, attr))
    logger = configure_logs(tmp_path)
    handler = logger.handlers[-1]
    handler.maxBytes = 500
    try:
        for _ in range(30):
            print('中文日志' * 20, flush=True)
        print('partial', end=''); sys.stdout.flush()
        try:
            raise ValueError('error evidence')
        except ValueError:
            sys.excepthook(*sys.exc_info())
        sys.stderr.write('thread evidence\n')
        assert not log_errors, '\n'.join(log_errors)
        assert (tmp_path/'desktop.log.1').exists()
        contents = ''.join(p.read_text(encoding='utf-8') for p in tmp_path.glob('desktop.log*'))
        assert 'error evidence' in contents and 'thread evidence' in contents
        assert len(list(tmp_path.glob('desktop.log*'))) <= 4
    finally:
        logger.removeHandler(handler); handler.close()


def test_log_write_failure_reports_once_without_recursive_logging(tmp_path, monkeypatch):
    from stock_analysis.desktop.logging_setup import DesktopLogHandler
    from stock_analysis.desktop import windows_instance
    errors = []
    monkeypatch.setattr(windows_instance, 'show_error', errors.append)
    handler = DesktopLogHandler(tmp_path/'failure.log', maxBytes=50, backupCount=1, encoding='utf-8')
    try:
        def fail_to_rotate():
            raise PermissionError('rotation denied')
        monkeypatch.setattr(handler, 'doRollover', fail_to_rotate)
        record = logging.LogRecord('test', logging.ERROR, __file__, 1, 'x' * 100, (), None)
        handler.emit(record)
        handler.emit(record)
        assert len(errors) == 1 and '日志写入失败' in errors[0]
    finally:
        handler.close()


@pytest.mark.skipif(os.name != 'nt', reason='pythonw only exists on Windows')
def test_pythonw_search_worker_json_pipe_with_chinese_names(tmp_path):
    interpreter = Path(sys.executable).with_name('pythonw.exe')
    script = tmp_path/'check_search.py'
    output = tmp_path/'output.json'
    script.write_text('import sys,json\n'
        f'sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n'
        'from stock_analysis.domains.stocks.stock_search import generate_keys\n'
        f'from pathlib import Path\nPath({str(output)!r}).write_text('
        'json.dumps(generate_keys(["宁德时代","中国银行"])),encoding="utf-8")\n', encoding='utf-8')
    subprocess.run([str(interpreter), str(script)], check=True, timeout=40)
    rows = json.loads(output.read_text(encoding='utf-8'))
    assert rows == [['ningdeshidai', 'ndsd'], ['zhongguoyinhang', 'zgyh']]


@pytest.mark.parametrize('fail,restart,busy_first,compete', [
    (False,False,False,False), (True,False,False,False),
    (False,True,False,False), (False,True,True,False),
    (False,True,False,True), (False,False,False,True)])
def test_tray_opens_only_when_running_and_exits_after_backend(tmp_path, monkeypatch, fail,restart,busy_first,compete):
    from stock_analysis.desktop import tray
    from stock_analysis.infrastructure import app_settings
    from stock_analysis.http import server
    opened, errors, stopped = [], [], []
    settings = SimpleNamespace(DATABASE_PATH=tmp_path/'unused.sqlite3', ROOT=tmp_path)
    monkeypatch.setattr(app_settings, 'AppSettings', lambda: settings)
    monkeypatch.setattr(tray.webbrowser, 'open', lambda url: opened.append(url) or True)
    monkeypatch.setattr(tray, 'show_error', errors.append)
    holder = {}
    class Instance:
        primary = True
        exit_sent = False
        busy_sent = False
        def __init__(self, path): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def wait(self):
            time.sleep(.01)
            if (opened or errors) and not self.exit_sent:
                if busy_first and not self.busy_sent:
                    self.busy_sent = True
                    holder['control'].activity_check = lambda: True
                    holder['icon'].menu[-2].action(holder['icon'], None)
                    return False
                if busy_first and not errors:
                    return False
                holder['control'].activity_check = lambda: False
                self.exit_sent = True
                holder['icon'].menu[-2 if restart else -1].action(holder['icon'], None)
                if compete:
                    deadline = time.monotonic() + 2
                    while holder['control'].snapshot()[0] == 'running' and time.monotonic() < deadline:
                        time.sleep(.01)
                    assert holder['control'].snapshot()[0] in ('stopping','stopped')
                    # A stale second menu callback must not reverse the accepted action.
                    holder['icon'].menu[-1 if restart else -2].action(holder['icon'], None)
            return False
    class Menu(list):
        SEPARATOR = None
        def __init__(self, *items): super().__init__(items)
    class Item:
        def __init__(self, title, action, **kwargs): self.action = action; self.title = title
    class Icon:
        def __init__(self, *args, menu): self.menu = menu; holder['icon'] = self
        def run(self, setup): setup(self)
        def update_menu(self): pass
        def notify(self, *args): errors.append(str(args))
        def stop(self): stopped.append(True)
    monkeypatch.setattr(tray, 'DesktopInstance', Instance)
    monkeypatch.setitem(sys.modules, 'pystray', SimpleNamespace(Icon=Icon, Menu=Menu, MenuItem=Item))
    def serve(port, *, control):
        assert not opened
        holder['control'] = control
        if fail: raise OSError('startup unavailable')
        control, listener = rebind(control)
        until = time.monotonic() + 5
        while control.snapshot()[0] != 'stopping' and time.monotonic() < until:
            time.sleep(.01)
        assert control.snapshot()[0] == 'stopping'
    monkeypatch.setattr(server, 'serve', serve)
    result = tray.run(8768)
    assert stopped
    if fail:
        assert not opened and any('startup unavailable' in error for error in errors)
    else:
        assert opened == ['http://127.0.0.1:8768/']
        assert bool(result) is restart
        if busy_first:
            assert len(errors) == 1 and '暂时无法重启' in errors[0]
        else:
            assert not errors


def test_unwritable_log_directory_does_not_replace_streams(tmp_path):
    from stock_analysis.desktop.logging_setup import configure_logs
    blocked = tmp_path/'file'
    blocked.write_text('not a directory', encoding='utf-8')
    original = sys.stdout
    with pytest.raises(OSError):
        configure_logs(blocked/'logs')
    assert sys.stdout is original


def test_scheduler_recovery_failure_preserves_original_error_and_closes_port(tmp_path, monkeypatch):
    events = []
    server = SimpleNamespace(server_close=lambda: events.append('closed'))
    def recover():
        raise OSError('recovery unavailable')
    monkeypatch.setattr(runtime, 'SourceArchiveService', lambda *a: None)
    monkeypatch.setattr(runtime, 'instance_lock', lambda path: nullcontext())
    with pytest.raises(OSError, match='recovery unavailable'):
        runtime.run_server(8768, False, database_path=tmp_path/'unused.sqlite3',
            handler=None, server_factory=lambda *a: server, initialize=lambda *a, **k: None,
            activity_check=lambda: False, management_factory=lambda db: SimpleNamespace(recover=recover),
            clear_state=lambda: None, open_url=lambda url: None)
    assert events == ['closed']
