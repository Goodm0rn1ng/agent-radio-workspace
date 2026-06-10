"""Writer-priority read/write lock.

Many concurrent QA reads can hold the read lock at the same time; a writer
(ingest step / KB apply) waits for in-flight readers to drain, then takes an
exclusive lock that blocks both new readers and new writers. Writer-priority
prevents reader starvation of the rarer ingest path.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager


class ReadWriteLock:
    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    def acquire_read(self) -> None:
        with self._cond:
            while self._writer or self._waiting_writers > 0:
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers > 0:
                    self._cond.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1

    def release_write(self) -> None:
        with self._cond:
            self._writer = False
            self._cond.notify_all()

    @contextmanager
    def reader(self):
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextmanager
    def writer(self):
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()
