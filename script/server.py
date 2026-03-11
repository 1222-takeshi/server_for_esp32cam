import socket
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

socket.setdefaulttimeout(60)


def _create_app():
    from script.app import create_app

    return create_app()

app = _create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=app.config["PORT"])
