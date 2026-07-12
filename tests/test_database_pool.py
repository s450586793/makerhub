from unittest.mock import patch

import pytest

from app.core import database


class FakeConnection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeCheckout:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        return False


class FakePool:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.connection_value = FakeConnection()
        self.checkouts = 0
        self.puts = 0
        self.open_calls = 0
        self.close_calls = 0
        self.__class__.instances.append(self)

    def open(self, *, wait=False):
        self.open_calls += 1

    def getconn(self):
        self.checkouts += 1
        return self.connection_value

    def putconn(self, connection):
        assert connection is self.connection_value
        self.puts += 1

    def close(self):
        self.close_calls += 1


@pytest.fixture(autouse=True)
def reset_pool():
    FakePool.instances = []
    database._reset_database_pool_for_tests()
    yield
    database._reset_database_pool_for_tests()


def test_database_connection_reuses_one_pid_scoped_pool():
    with (
        patch.object(database, "ConnectionPool", FakePool),
        patch.object(database, "database_url", return_value="postgresql://example/db"),
        patch.object(database, "dict_row", object()),
    ):
        with database.database_connection():
            pass
        with database.database_connection():
            pass

    assert len(FakePool.instances) == 1
    assert FakePool.instances[0].open_calls == 1
    assert FakePool.instances[0].checkouts == 2
    assert FakePool.instances[0].puts == 2
    assert FakePool.instances[0].connection_value.commits == 2


def test_database_connection_rolls_back_before_returning_connection():
    with (
        patch.object(database, "ConnectionPool", FakePool),
        patch.object(database, "database_url", return_value="postgresql://example/db"),
        patch.object(database, "dict_row", object()),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            with database.database_connection():
                raise RuntimeError("boom")

    connection = FakePool.instances[0].connection_value
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_database_connection_returns_connection_when_commit_fails():
    class CommitFailureConnection(FakeConnection):
        def commit(self):
            raise RuntimeError("commit failed")

    class CommitFailurePool(FakePool):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.connection_value = CommitFailureConnection()

    with (
        patch.object(database, "ConnectionPool", CommitFailurePool),
        patch.object(database, "database_url", return_value="postgresql://example/db"),
        patch.object(database, "dict_row", object()),
    ):
        with pytest.raises(RuntimeError, match="commit failed"):
            with database.database_connection():
                pass

    pool = CommitFailurePool.instances[0]
    assert pool.connection_value.rollbacks == 1
    assert pool.puts == 1


def test_database_pool_rebuilds_after_pid_or_url_change():
    pid = 100
    url = "postgresql://example/one"

    def current_pid():
        return pid

    def current_url():
        return url

    with (
        patch.object(database, "ConnectionPool", FakePool),
        patch.object(database, "database_url", side_effect=current_url),
        patch.object(database.os, "getpid", side_effect=current_pid),
        patch.object(database, "dict_row", object()),
    ):
        with database.database_connection():
            pass
        pid = 101
        with database.database_connection():
            pass
        url = "postgresql://example/two"
        with database.database_connection():
            pass

    assert len(FakePool.instances) == 3
    assert [item.close_calls for item in FakePool.instances[:2]] == [1, 1]


def test_close_database_pool_is_idempotent():
    with (
        patch.object(database, "ConnectionPool", FakePool),
        patch.object(database, "database_url", return_value="postgresql://example/db"),
        patch.object(database, "dict_row", object()),
    ):
        with database.database_connection():
            pass
        database.close_database_pool()
        database.close_database_pool()

    assert FakePool.instances[0].close_calls == 1


def test_pool_checkout_failure_does_not_expose_database_url():
    class BrokenPool(FakePool):
        def getconn(self):
            raise RuntimeError("password=secret host=database.internal")

    with (
        patch.object(database, "ConnectionPool", BrokenPool),
        patch.object(database, "database_url", return_value="postgresql://user:secret@database.internal/db"),
        patch.object(database, "dict_row", object()),
    ):
        with pytest.raises(database.DatabaseUnavailable) as exc_info:
            with database.database_connection():
                pass

    message = str(exc_info.value)
    assert "secret" not in message
    assert "database.internal" not in message
