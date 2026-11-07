"""Restart process handoff without touching the daily backend or desktop."""
import os
from pathlib import Path
import subprocess
import sys
import time
import runpy

import pytest

from stock_analysis.desktop import restart


def wait_for_file(path, timeout=5):
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert path.exists()


def test_replacement_keeps_environment_port_and_does_not_open_browser(tmp_path, monkeypatch):
    entry = tmp_path/'项目 with spaces'/'desktop_app.pyw'
    entry.parent.mkdir(); entry.write_text('# test', encoding='utf-8')
    interpreter = tmp_path/'Python'/'python.exe'
    interpreter.parent.mkdir(); interpreter.with_name('pythonw.exe').touch()
    monkeypatch.setattr(restart.sys, 'executable', str(interpreter))
    calls = []
    monkeypatch.setattr(restart.subprocess, 'Popen', lambda *args, **kwargs: calls.append((args, kwargs)))
    restart.launch_replacement(entry, 8768)
    (args, kwargs), = calls
    assert args[0] == [str(interpreter.with_name('pythonw.exe')), str(entry.resolve()),
                       '--port','8768','--restart-parent',str(os.getpid()),'--no-open-browser']
    assert kwargs == dict(cwd=str(entry.parent.resolve()), close_fds=True,
                         creationflags=subprocess.CREATE_NO_WINDOW)


def test_replacement_spawn_failure_is_not_hidden(tmp_path, monkeypatch):
    entry = tmp_path/'desktop_app.pyw'; entry.touch()
    monkeypatch.setattr(restart.sys, 'executable', str(tmp_path/'python.exe'))
    (tmp_path/'pythonw.exe').touch()
    def failed(*args, **kwargs):
        raise OSError('cannot create process')
    monkeypatch.setattr(restart.subprocess, 'Popen', failed)
    with pytest.raises(OSError, match='cannot create process'):
        restart.launch_replacement(entry, 8768)


@pytest.mark.skipif(os.name != 'nt', reason='Windows process handles')
def test_wait_parent_timeout_then_reads_new_code_only_after_parent_exits(tmp_path):
    release = tmp_path/'release'
    source = tmp_path/'new_code.py'
    source.write_text('VERSION = 1\n', encoding='utf-8')
    parent_code = ('from pathlib import Path; import sys,time\n'
                   'p=Path(sys.argv[1]); deadline=time.monotonic()+15\n'
                   'while not p.exists() and time.monotonic()<deadline: time.sleep(.02)\n')
    parent = subprocess.Popen([sys.executable, '-c', parent_code, str(release)])
    child = None
    try:
        with pytest.raises(TimeoutError, match='尚未退出'):
            restart.wait_for_parent(parent.pid, timeout_ms=10)
        waiting, output = tmp_path/'waiting', tmp_path/'result'
        script = tmp_path/'replacement.py'
        script.write_text('from pathlib import Path\nimport sys\n'
            f'sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n'
            'from stock_analysis.desktop.restart import wait_for_parent\n'
            f'Path({str(waiting)!r}).touch()\nwait_for_parent({parent.pid},10000)\n'
            'import new_code\n'
            f'Path({str(output)!r}).write_text(str(new_code.VERSION),encoding="utf-8")\n', encoding='utf-8')
        child = subprocess.Popen([str(Path(sys.executable).with_name('pythonw.exe')), str(script)])
        wait_for_file(waiting)
        assert not output.exists() and parent.poll() is None
        source.write_text('VERSION = 2\n', encoding='utf-8')
        release.touch()
        assert parent.wait(timeout=5) == 0
        assert child.wait(timeout=5) == 0
        assert output.read_text(encoding='utf-8') == '2'
        restart.wait_for_parent(parent.pid, timeout_ms=10)  # Already exited is OK.
    finally:
        release.touch()
        parent.wait(timeout=5)
        if child is not None:
            child.wait(timeout=5)


def test_wait_for_self_or_invalid_pid_is_rejected():
    for pid in (-1, 0, os.getpid()):
        with pytest.raises(ValueError):
            restart.wait_for_parent(pid)


@pytest.mark.parametrize('outcome', ['exit', 'restart', 'spawn_failure', 'cleanup_failure'])
def test_entry_waits_before_logging_and_only_restarts_after_successful_cleanup(monkeypatch, outcome):
    from stock_analysis.desktop import tray, logging_setup, windows_instance
    calls, errors = [], []
    monkeypatch.setattr(sys, 'argv', ['desktop_app.pyw','--port','8768',
                                     '--restart-parent','12345','--no-open-browser'])
    monkeypatch.setattr(restart, 'wait_for_parent', lambda pid: calls.append(('wait',pid)))
    monkeypatch.setattr(logging_setup, 'configure_logs', lambda directory: calls.append('logs'))
    def run(port, *, open_browser):
        calls.append(('run', port, open_browser))
        if outcome == 'cleanup_failure':
            raise OSError('cleanup failed')
        return outcome != 'exit'
    def launch(entry, port):
        calls.append(('launch', port))
        if outcome == 'spawn_failure':
            raise OSError('spawn failed')
    monkeypatch.setattr(tray, 'run', run)
    monkeypatch.setattr(restart, 'launch_replacement', launch)
    monkeypatch.setattr(windows_instance, 'show_error', errors.append)
    runpy.run_path(str(Path(__file__).resolve().parents[1]/'desktop_app.pyw'), run_name='__main__')
    assert calls[:3] == [('wait',12345), 'logs', ('run',8768,False)]
    assert (('launch',8768) in calls) == (outcome in ('restart', 'spawn_failure'))
    if outcome == 'spawn_failure':
        assert len(errors) == 1 and '原后台已安全停止' in errors[0]
    elif outcome == 'cleanup_failure':
        assert len(errors) == 1 and 'cleanup failed' in errors[0]
    else:
        assert not errors
