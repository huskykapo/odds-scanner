from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from requests.structures import CaseInsensitiveDict

from odds_scanner.models import Event
from odds_scanner.providers.parsing import parse_events

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "sample_odds.json"

# The sample file's "fresh" timestamps are 30s before this instant.
NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def sample_path() -> Path:
    return SAMPLE


@pytest.fixture
def sample_events() -> dict[str, Event]:
    import json

    return {e.id: e for e in parse_events(json.loads(SAMPLE.read_text()))}


class FakeResponse:
    def __init__(self, status=200, json_body=None, headers=None, text_json=True):
        self.status_code = status
        self._body = json_body
        self.headers = CaseInsensitiveDict(headers or {})
        self._valid = text_json

    def json(self):
        if not self._valid:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Stands in for requests.Session: returns/raises queued items, records calls."""

    def __init__(self, *queue):
        self.queue = list(queue)
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, dict(json or {})))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True
