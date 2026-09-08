"""受宿主管理的 POSIX Shell 进程组。

stdout/stderr 先落盘，防止长命令吃满内存。进程组清理能清理普通子进程，
但不能约束主动 setsid 的进程；它不是容器或安全沙箱。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path
from uuid import uuid4


class ShellRunner:
    def __init__(self, output_dir: Path, timeout: float = 120):
        self.output_dir = output_dir
        self.timeout = timeout
        self._processes: dict[subprocess.Popen, Path] = {}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    def run(self, command: str, cwd: Path) -> tuple[str, int]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.output_dir / f"shell-{uuid4().hex}.txt"
        timed_out = False
        with output_path.open("wb") as output:
            with self._lock:
                if self._closed:
                    raise RuntimeError("ShellRunner is closed")
                process = subprocess.Popen(
                    command, shell=True, executable="/bin/bash", cwd=cwd,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self._processes[process] = cwd.resolve()
            try:
                process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                self._stop(process)
                with self._lock:
                    self._processes.pop(process, None)
        with output_path.open(encoding="utf-8", errors="replace") as handle:
            text = handle.read(40001)
        if len(text) > 40000:
            text = text[:40000] + f"\n[Full output saved to {output_path}]"
        if timed_out:
            text += f"\n[Timed out after {self.timeout:g}s]"
        return text or "(no output)", 124 if timed_out else int(process.returncode or 0)

    def is_busy(self, cwd: Path) -> bool:
        with self._lock:
            return cwd.resolve() in self._processes.values()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            processes = list(self._processes)
        for process in processes:
            self._stop(process)
