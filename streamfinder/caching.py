"""Small thread-safe helpers for the in-memory caches shared by request threads."""

import contextlib
import threading
from collections import OrderedDict
from collections.abc import Iterator


class LRU(OrderedDict):
    """A dict that forgets its least recently used entries beyond `size`."""

    def __init__(self, size: int):
        super().__init__()
        self.size = size
        self._lock = threading.Lock()

    def __setitem__(self, key, value) -> None:
        with self._lock:
            super().__setitem__(key, value)
            self.move_to_end(key)
            while len(self) > self.size:
                self.popitem(last=False)

    def get(self, key, default=None):
        with self._lock:
            if key not in self:
                return default
            self.move_to_end(key)
            return super().__getitem__(key)


class KeyedLocks:
    """One lock per key, dropped again when nobody holds or waits for it.

    Lets the first request for something do the work while identical requests
    wait for its result, without a lock per key piling up forever.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._locks: dict = {}  # key -> [lock, users]

    @contextlib.contextmanager
    def hold(self, key) -> Iterator[None]:
        with self._lock:
            entry = self._locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._lock:
                entry[1] -= 1
                if not entry[1]:
                    del self._locks[key]
