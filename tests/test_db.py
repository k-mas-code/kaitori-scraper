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
