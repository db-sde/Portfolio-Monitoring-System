"""Supervise API and durable worker in a single container.

Exit if either child fails so the platform restarts the service. Each child
has a process group, allowing shutdown to include the parser subprocess.
"""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).parent / ".env")
    children = []
    stopping = False

    def stop(*args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        cwd = str(Path(__file__).parent)
        children.append(
            subprocess.Popen(
                [sys.executable, "worker.py"], cwd=cwd, start_new_session=True
            )
        )
        children.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "main:app",
                    "--host",
                    os.environ.get("HOST", "0.0.0.0"),
                    "--port",
                    os.environ.get("PORT", "8000"),
                ],
                cwd=cwd,
                start_new_session=True,
            )
        )
        while not stopping:
            if any(child.poll() is not None for child in children):
                return 1
            time.sleep(0.25)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


if __name__ == "__main__":
    sys.exit(main())
