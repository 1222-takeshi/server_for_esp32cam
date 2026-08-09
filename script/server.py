import socket
import sys
import os
from pathlib import Path

from script.config import gunicorn_preload_requested

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

socket.setdefaulttimeout(60)


def _create_app():
    from script.app import create_app

    # Gunicorn may preload the WSGI app in its master process. Defer the
    # scheduler until the worker process when preload is requested. With the
    # default non-preload mode, this import already runs in the worker.
    return create_app({"RETENTION_SCHEDULER_AUTO_START": not gunicorn_preload_requested()})

app = _create_app()


def _start_retention_scheduler_after_fork() -> None:
    from script.app import start_retention_scheduler

    start_retention_scheduler(app)


os.register_at_fork(after_in_child=_start_retention_scheduler_after_fork)


if __name__ == "__main__":
    from script.app import start_retention_scheduler

    start_retention_scheduler(app)
    app.run(host="0.0.0.0", port=app.config["PORT"])
