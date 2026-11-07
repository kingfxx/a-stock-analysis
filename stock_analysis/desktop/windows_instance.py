"""Current-session desktop identity and activation, independent of the DB lock."""
import ctypes
from ctypes import wintypes
import getpass
import hashlib
import os
from pathlib import Path


class DesktopInstance:
    def __init__(self, database_path):
        if os.name != 'nt':
            raise OSError('桌面托盘入口仅支持 Windows。')
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        for name, args, result in (
            ('CreateMutexW', [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            ('CreateEventW', [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            ('SetEvent', [wintypes.HANDLE], wintypes.BOOL),
            ('WaitForSingleObject', [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            ('CloseHandle', [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        identity = getpass.getuser() + ':' + os.path.normcase(str(Path(database_path).resolve()))
        key = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]
        name = 'Local\\AStockAnalysis-' + key
        self.event = self.mutex = None
        try:
            # Create event before publishing mutex: a racing second launch can signal it.
            self.event = self.api.CreateEventW(None, False, False, name + '-open')
            if not self.event:
                raise ctypes.WinError(ctypes.get_last_error())
            self.mutex = self.api.CreateMutexW(None, False, name)
            last_error = ctypes.get_last_error()
            if not self.mutex:
                raise ctypes.WinError(last_error)
            self.primary = last_error != 183  # ERROR_ALREADY_EXISTS
        except BaseException:
            self.close()
            raise

    def activate(self):
        if not self.api.SetEvent(self.event):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, milliseconds=500):
        result = self.api.WaitForSingleObject(self.event, milliseconds)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0

    def close(self):
        for name in ('mutex', 'event'):
            handle = getattr(self, name, None)
            if handle:
                self.api.CloseHandle(handle)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def show_error(message):
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    api.MessageBoxW(None, str(message), 'A 股分析', 0x10)
