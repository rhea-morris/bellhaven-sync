import sys
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_server import TOKEN, create_app  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    app = create_app()
    srv = make_server("127.0.0.1", 0, app)
    port = srv.server_port
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    from bellhaven_sync import config
    monkeypatch.setattr(config, "SITE_BASE", base)
    monkeypatch.setattr(config, "CRM_BASE", base + "/api/v1")
    monkeypatch.setattr(config, "CRM_TOKEN", TOKEN)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "state.db")
    yield {"state": app.config["STATE"], "base": base, "tmp": tmp_path}
    srv.shutdown()
