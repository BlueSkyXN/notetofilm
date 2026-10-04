import os
import signal
import subprocess
import time


def capture(command, timeout, cwd=None, input_text=None, stopping=None):
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if stopping is not None and stopping.is_set():
                raise InterruptedError('Service is stopping')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                output, error = process.communicate(input_text, timeout=min(remaining, .2))
                return process.returncode, output, error
            except subprocess.TimeoutExpired:
                input_text = None
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
