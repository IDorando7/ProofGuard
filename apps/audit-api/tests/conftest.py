import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import shutil
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))


class _Python314ASGIClient:
    """Synchronous facade for a Python 3.14 AnyIO worker-thread regression."""

    def request(self, method: str, url: str, **kwargs):
        async def send_request():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=True,
            ) as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(send_request())

    def __init__(self, app):
        self.app = app

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs):
        return self.request("PATCH", url, **kwargs)

    def close(self):
        return None


def _disable_broken_python314_threadpool() -> None:
    if sys.version_info < (3, 14):
        return

    async def run_directly(function, *args, **kwargs):
        return function(*args, **kwargs)

    @asynccontextmanager
    async def enter_directly(context_manager):
        with context_manager as value:
            yield value

    import fastapi.concurrency
    import fastapi.dependencies.utils
    import fastapi.routing
    import starlette.background
    import starlette.concurrency
    import starlette.datastructures
    import starlette.routing

    starlette.concurrency.run_in_threadpool = run_directly
    starlette.routing.run_in_threadpool = run_directly
    starlette.background.run_in_threadpool = run_directly
    starlette.datastructures.run_in_threadpool = run_directly
    fastapi.concurrency.run_in_threadpool = run_directly
    fastapi.concurrency.contextmanager_in_threadpool = enter_directly
    fastapi.dependencies.utils.run_in_threadpool = run_directly
    fastapi.dependencies.utils.contextmanager_in_threadpool = enter_directly
    fastapi.routing.run_in_threadpool = run_directly


@pytest.fixture()
def client(tmp_path):
    data_dir = APP_ROOT / "data"
    shutil.rmtree(data_dir / "audits", ignore_errors=True)
    (data_dir / "audits").mkdir(parents=True, exist_ok=True)

    _disable_broken_python314_threadpool()

    from app.core.database import Base, engine
    from app.core.paths import protocol_data_root
    from app.main import app

    engine.dispose()
    (data_dir / "audit_api.sqlite3").unlink(missing_ok=True)
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[protocol_data_root] = lambda: tmp_path / "protocol"
    test_client = _Python314ASGIClient(app) if sys.version_info >= (3, 14) else TestClient(app)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()
