from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from deerflow.config.database_config import DatabaseConfig


@pytest.mark.anyio
async def test_async_store_uses_database_config_when_checkpointer_absent(tmp_path):
    from deerflow.runtime.store.async_provider import make_store

    app_config = SimpleNamespace(
        checkpointer=None,
        database=DatabaseConfig(backend="sqlite", sqlite_dir=str(tmp_path)),
    )

    async with make_store(app_config) as store:
        assert store.__class__.__name__ == "AsyncSqliteStore"
        await store.aput(("threads",), "thread-1", {"title": "Persistent"})
        item = await store.aget(("threads",), "thread-1")

    assert item is not None
    assert item.value == {"title": "Persistent"}


def test_sync_store_context_uses_database_config_when_checkpointer_absent(tmp_path):
    from deerflow.runtime.store.provider import store_context

    app_config = SimpleNamespace(
        checkpointer=None,
        database=DatabaseConfig(backend="sqlite", sqlite_dir=str(tmp_path)),
    )

    with patch("deerflow.runtime.store.provider.get_app_config", return_value=app_config):
        with store_context() as store:
            assert store.__class__.__name__ == "SqliteStore"
            store.put(("threads",), "thread-1", {"title": "Persistent"})
            item = store.get(("threads",), "thread-1")

    assert item is not None
    assert item.value == {"title": "Persistent"}


def test_sync_checkpointer_context_uses_database_config_when_checkpointer_absent(tmp_path):
    from deerflow.runtime.checkpointer.provider import checkpointer_context

    app_config = SimpleNamespace(
        checkpointer=None,
        database=DatabaseConfig(backend="sqlite", sqlite_dir=str(tmp_path)),
    )

    with patch("deerflow.runtime.checkpointer.provider.get_app_config", return_value=app_config):
        with checkpointer_context() as checkpointer:
            assert checkpointer.__class__.__name__ == "SqliteSaver"
