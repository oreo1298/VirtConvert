"""Runs conversions on a worker thread (shared with the EZP2019Linux suite)."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

Job = Callable[[Callable[[str, int, int], None], Callable[[], bool]], Any]


class _Operation(QObject):
    progress = Signal(str, object, object)   # byte counts: a C++ int ends at 2 GB
    succeeded = Signal(object)
    failed = Signal(object)
    done = Signal()

    def __init__(self, job: Job):
        super().__init__()
        self._job = job
        self._cancel = threading.Event()
        self._last_emit = 0.0
        self._last_stage = ""

    def cancel(self) -> None:
        self._cancel.set()

    def _progress(self, stage: str, done: int, total: int) -> None:
        now = time.monotonic()
        if stage != self._last_stage or (total and done >= total) or now - self._last_emit > 0.05:
            self._last_emit = now
            self._last_stage = stage
            self.progress.emit(stage, done, total)

    @Slot()
    def run(self) -> None:
        try:
            result = self._job(self._progress, self._cancel.is_set)
        except BaseException as exc:  # reported to the UI, never swallowed
            self.failed.emit(exc)
        else:
            self.succeeded.emit(result)
        finally:
            self.done.emit()
            # Leave the thread's event loop now so waiting on it never blocks.
            QThread.currentThread().quit()


class OperationRunner(QObject):
    """Executes one job at a time and reports back on the GUI thread."""

    started = Signal(str)
    progress = Signal(str, object, object)   # byte counts: a C++ int ends at 2 GB
    finished = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._op: _Operation | None = None
        self._name = ""
        self._on_success: Callable[[Any], None] | None = None
        self._on_error: Callable[[BaseException], None] | None = None
        self.started_at = 0.0

    @property
    def busy(self) -> bool:
        return self._thread is not None

    @property
    def name(self) -> str:
        return self._name

    def start(self, name: str, job: Job, on_success: Callable[[Any], None],
              on_error: Callable[[BaseException], None]) -> bool:
        if self.busy:
            return False
        self._name = name
        self._on_success = on_success
        self._on_error = on_error
        thread = QThread(self)
        op = _Operation(job)
        op.moveToThread(thread)
        thread.started.connect(op.run)
        op.progress.connect(self._relay_progress, Qt.QueuedConnection)
        op.succeeded.connect(self._handle_success, Qt.QueuedConnection)
        op.failed.connect(self._handle_error, Qt.QueuedConnection)
        op.done.connect(self._handle_done, Qt.QueuedConnection)
        self._thread, self._op = thread, op
        self.started_at = time.monotonic()
        self.started.emit(name)
        thread.start()
        return True

    def cancel(self) -> None:
        if self._op is not None:
            self._op.cancel()

    def wait(self, ms: int = 10000) -> None:
        if self._thread is not None:
            self._thread.wait(ms)

    @Slot(str, object, object)
    def _relay_progress(self, stage: str, done: int, total: int) -> None:
        self.progress.emit(stage, done, total)

    @Slot(object)
    def _handle_success(self, result: Any) -> None:
        if self._on_success is not None:
            self._on_success(result)

    @Slot(object)
    def _handle_error(self, exc: BaseException) -> None:
        if self._on_error is not None:
            self._on_error(exc)

    @Slot()
    def _handle_done(self) -> None:
        thread, name = self._thread, self._name
        self._thread = self._op = None
        self._on_success = self._on_error = None
        if thread is not None:
            thread.quit()
            thread.wait()
            thread.deleteLater()
        self.finished.emit(name)
