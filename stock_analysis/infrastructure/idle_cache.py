"""Access timestamps for bounded caches, guarded by their existing owner locks."""
from collections import OrderedDict
from time import monotonic

IDLE_SECONDS = 3600


class IdleCache(OrderedDict):
    def __init__(self, *, clock=None):
        super().__init__()
        self.clock = clock or monotonic
        self._used = {}

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self._used[key] = self.clock()
        return value

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._used[key] = self.clock()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._used.pop(key, None)

    def popitem(self, last=True):
        key, value = super().popitem(last=last)
        self._used.pop(key, None)
        return key, value

    def clear(self):
        super().clear()
        self._used.clear()

    def expire(self):
        now = self.clock()
        removed = []
        for key, used in list(self._used.items()):
            if now - used >= IDLE_SECONDS:
                removed.append((key, super().__getitem__(key)))
                del self[key]
        return removed
