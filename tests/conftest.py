"""Используем настоящий aiosqlite; старый fallback-адаптер тестов не подменяет его."""
import asyncio
import sys
from pathlib import Path

import aiosqlite
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def fresh_test_lock():
    # Каждый старый синхронный тест создаёт отдельный asyncio.run/event loop.
    # В production один event loop; в тестах lock не переносим между loops.
    from database.db import db
    assert db.db is None
    db._lock = asyncio.Lock()
