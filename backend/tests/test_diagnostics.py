from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.diagnostics.engine import critical_path_ms


def test_concurrent_spans_are_not_summed() -> None:
    start = datetime.now(UTC)
    spans = [
        SimpleNamespace(started_at=start, ended_at=start + timedelta(seconds=2)),
        SimpleNamespace(
            started_at=start + timedelta(seconds=1), ended_at=start + timedelta(seconds=3)
        ),
    ]
    assert critical_path_ms(spans) == 3000
