"""Fresh-process desktop restart; no backend imports or database ownership."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys


def wait_for_parent(pid, timeout_ms=60000):
    """Do not open logs or the desktop mutex until the old process is gone."""
    if pid <= 0 or pid == os.getpid():
        raise ValueError('重启等待的原进程无效。')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: process already exited
            return
        raise ctypes.WinError(error)
    try:
        result = api.WaitForSingleObject(handle, timeout_ms)
        if result == 258:  # WAIT_TIMEOUT
            raise TimeoutError('原后台尚未退出，已取消此次重启；请检查日志。')
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.CloseHandle(handle)


def launch_replacement(entry_path, port):
    """Launch a waiting GUI process, without inheriting old locks or handles."""
    entry = Path(entry_path).resolve(strict=True)
    interpreter = Path(sys.executable).with_name('pythonw.exe')
    if not interpreter.is_file():
        raise FileNotFoundError(f'找不到同环境的 pythonw.exe：{interpreter}')
    subprocess.Popen(
        [str(interpreter), str(entry), '--port', str(port),
         '--restart-parent', str(os.getpid()), '--no-open-browser'],
        cwd=str(entry.parent), close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
