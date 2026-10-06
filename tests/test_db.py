import pytest

from arbitrage import db
from arbitrage.config import ConfigError, parse_settings


class FakeQuery:
    def __init__(self, client, table):
        self.client, self.table_name = client, table

    def select(self, columns):
        self.client.calls.append((self.table_name, columns))
        return self

    def limit(self, n):
        return self

    def execute(self):
        if self.client.error:
            raise self.client.error
        return type("Response", (), {"data": self.client.rows})()


class FakeClient:
    """arbitrage_settings の行を返すだけの偽の Supabase クライアント"""

    def __init__(self, rows, error=None):
        self.rows, self.error, self.calls = rows, error, []

    def table(self, name):
        return FakeQuery(self, name)


def test_fetch_settings_returns_config_of_the_single_row():
    config = {"version": 1, "purchase_date": "2026-10-11", "common": [{"name": "a", "rate": 0.01}]}
    client = FakeClient([{"config": config}])
    got = db.fetch_settings(client)
    assert got == config and client.calls == [("arbitrage_settings", "config")]
    assert parse_settings(got).common[0].name == "a"


def test_fetch_settings_without_row_tells_to_save_on_settings_page():
    with pytest.raises(ConfigError, match="設定画面で保存してください"):
        db.fetch_settings(FakeClient([]))


def test_fetch_settings_reports_broken_row_and_read_errors():
    with pytest.raises(ConfigError, match="壊れている"):
        db.fetch_settings(FakeClient([{"config": None}]))
    with pytest.raises(ConfigError, match="読めない.*relation does not exist"):
        db.fetch_settings(FakeClient([], error=RuntimeError("relation does not exist")))


# ---- リサーチ依頼・待ち受けの状態 ----

class RecordingQuery:
    """呼ばれたメソッドを順に記録するだけの偽のクエリ"""

    def __init__(self, client, table):
        self.client = client
        self.steps = [("table", table)]

    def __getattr__(self, name):
        def step(*args, **kwargs):
            self.steps.append((name, *args, *sorted(kwargs.items())))
            return self
        return step

    def execute(self):
        self.client.queries.append(self.steps)
        return type("Response", (), {"data": self.client.rows})()


class RecordingClient:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def table(self, name):
        return RecordingQuery(self, name)


def test_update_request_returns_updated_row_and_can_require_status():
    client = RecordingClient([{"id": 7, "cancel_requested": True}])
    assert db.update_request(client, 7, {"progress": {"stage": "api"}}) == {"id": 7, "cancel_requested": True}
    assert client.queries[0] == [("table", "arbitrage_research_requests"),
                                 ("update", {"progress": {"stage": "api"}}), ("eq", "id", 7)]
    db.update_request(client, 7, {"status": "running"}, only_status="queued")
    assert client.queries[1][-1] == ("eq", "status", "queued")
    assert db.update_request(RecordingClient([]), 7, {"status": "running"}, only_status="queued") is None


def test_fetch_requests_and_worker_state():
    client = RecordingClient([{"id": 3}, {"id": 5}])
    assert db.fetch_requests_by_status(client, "queued") == [{"id": 3}, {"id": 5}]
    assert ("eq", "status", "queued") in client.queries[0] and ("order", "id") in client.queries[0]
    assert db.fetch_request(client, 3) == {"id": 3} and db.fetch_request(RecordingClient([]), 3) is None
    assert db.fetch_worker_state(RecordingClient([])) is None
    db.upsert_worker_state(client, {"heartbeat_at": "2026-10-05T03:00:00+00:00"})
    assert client.queries[-1] == [("table", "arbitrage_worker_state"),
                                  ("upsert", {"heartbeat_at": "2026-10-05T03:00:00+00:00", "singleton": True},
                                   ("on_conflict", "singleton"))]
