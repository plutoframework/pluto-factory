"""A step pool whose threads can never block interpreter exit.

``concurrent.futures`` joins its (non-daemon) worker threads from an atexit
hook, so a step blocked on a long worker session keeps the process alive
after SIGTERM/SIGINT and the coordinator lock is long gone. These threads are
daemons: worker sessions are designed to outlive the coordinator and are
reconciled on the next start, so abandoning a step is safe.
"""

import queue
import threading
from concurrent.futures import Future


class StepPool:
    def __init__(self, max_workers, thread_name_prefix="factory-step"):
        self.max_workers = max_workers
        self._prefix = thread_name_prefix
        self._queue = queue.SimpleQueue()
        self._threads = []
        self._idle = 0
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, fn, *args, **kwargs):
        future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("step pool is shut down")
            self._queue.put((future, fn, args, kwargs))
            # A thread counts itself idle before it takes its first item,
            # so compare queued work with idle threads, not idle with zero.
            if (
                self._queue.qsize() > self._idle
                and len(self._threads) < self.max_workers
            ):
                thread = threading.Thread(
                    target=self._work,
                    name=f"{self._prefix}_{len(self._threads)}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()
        return future

    def _work(self):
        while True:
            with self._lock:
                self._idle += 1
            item = self._queue.get()
            with self._lock:
                self._idle -= 1
            if item is None:
                return
            future, fn, args, kwargs = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as error:  # noqa: BLE001 - delivered
                future.set_exception(error)

    def shutdown(self, wait=True, cancel_futures=False):
        with self._lock:
            self._closed = True
            threads = list(self._threads)
        if cancel_futures:
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    item[0].cancel()
        for _ in threads:
            self._queue.put(None)
        if wait:
            for thread in threads:
                thread.join()
