"""Infrequent idle-process reclamation, without trimming live working-set pages."""
import gc
import os
from time import monotonic, perf_counter

from stock_analysis.infrastructure.idle_cache import IDLE_SECONDS


def memory_state():
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
            (key, ctypes.c_size_t) for key in ('peak', 'working', 'qpp', 'qp', 'qnp',
                                             'qn', 'page', 'peakpage', 'private')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    api = ctypes.WinDLL('psapi', use_last_error=True)
    api.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    value = Counters()
    value.cb = ctypes.sizeof(value)
    if not api.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return dict(working_bytes=value.working, private_bytes=value.private)


def _optimize_windows_heap():
    if os.name != 'nt':
        return False
    import ctypes
    from ctypes import wintypes

    class HeapInfo(ctypes.Structure):
        _fields_ = [('Version', wintypes.DWORD), ('Flags', wintypes.DWORD)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.HeapSetInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    kernel.HeapSetInformation.restype = wintypes.BOOL
    info = HeapInfo(1, 0)
    # HeapOptimizeResources=3: decommit unused LFH pages across the process heaps.
    if not kernel.HeapSetInformation(None, 3, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return True


def release_unused_memory():
    started = perf_counter()
    result = {}
    try:
        result['before'] = memory_state()
    except OSError as exc:
        result['measurement_error'] = str(exc)
    result['collected'] = gc.collect()
    try:
        result['heap_optimized'] = _optimize_windows_heap()
    except (OSError, AttributeError) as exc:
        result['heap_optimized'] = False
        result['heap_error'] = str(exc)
    try:
        result['after'] = memory_state()
    except OSError as exc:
        result['measurement_error'] = str(exc)
    result['elapsed_ms'] = round((perf_counter() - started) * 1000, 2)
    return result


class IdleMemoryReclaimer:
    def __init__(self, *, clock=None, release=None):
        self.clock = clock or monotonic
        self.release = release or release_unused_memory
        self.last_reclaimed_at = self.clock()
        self.last_result = None

    def due(self, last_request_at):
        now = self.clock()
        return now - last_request_at >= IDLE_SECONDS and now - self.last_reclaimed_at >= IDLE_SECONDS

    def reclaim(self):
        # Even an unsupported OS operation must not retry every scheduler tick.
        self.last_reclaimed_at = self.clock()
        self.last_result = self.release()
        result = self.last_result
        before, after = result.get('before'), result.get('after')
        detail = (f"工作集 {before['working_bytes']/1048576:.1f}→{after['working_bytes']/1048576:.1f} MiB；"
                  f"私有提交 {before['private_bytes']/1048576:.1f}→{after['private_bytes']/1048576:.1f} MiB"
                  if before and after else '本平台未提供内存测量')
        error = f"；堆优化原因：{result['heap_error']}" if result.get('heap_error') else ''
        print(f"空闲进程回收：{detail}；GC {result['collected']} 项；堆优化 {result['heap_optimized']}；"
              f"耗时 {result['elapsed_ms']}ms{error}", flush=True)
        return self.last_result
