"""Bounded command execution without inherited PIPE handles blocking shutdown."""
from __future__ import annotations

import subprocess
import tempfile
from contextlib import suppress

import psutil


def _kill_tree(process: subprocess.Popen) -> None:
    victims = []
    try:
        parent = psutil.Process(process.pid)
        parent.suspend()
        victims = parent.children(recursive=True)
        for child in victims:
            with suppress(psutil.Error):
                child.suspend()
        victims.append(parent)
    except psutil.Error:
        pass
    for child in victims:
        with suppress(psutil.Error):
            child.kill()
    with suppress(OSError):
        process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=2)


def run_bounded(args, *, timeout: float = 30, capture_output: bool = True,
                text: bool = False, encoding=None, errors=None, check: bool = False, **kwargs):
    if not capture_output:
        raise ValueError('run_bounded requires captured output')
    # Files avoid communicate() waiting forever for a grandchild to close PIPE.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(args, stdout=out, stderr=err, **kwargs)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            raise subprocess.TimeoutExpired(args, timeout) from None
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read(2 * 1024 * 1024), err.read(2 * 1024 * 1024)
    if text or encoding:
        stdout = stdout.decode(encoding or 'utf-8', errors or 'replace')
        stderr = stderr.decode(encoding or 'utf-8', errors or 'replace')
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result
