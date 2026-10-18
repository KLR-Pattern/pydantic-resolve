"""Keep the demo database pool within each test's event-loop lifetime."""

import pytest

from demo.use_case.database import engine, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Seed each test, then dispose the pool before its event loop closes."""
    try:
        await init_db()
        yield
    finally:
        await engine.dispose()
