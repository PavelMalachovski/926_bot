import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def _no_auto_plan(monkeypatch):
    """The auto-plan slot gate reads wall-clock time: without this, any
    stub-Watcher test calling run_cycle() after the day's first slot
    (08:05 Prague) would walk into a real snapshot fetch — a live network
    call from the test suite (CLAUDE.md: tests stay network-free). Tests
    that exercise the gate re-enable it explicitly."""
    monkeypatch.setattr(settings.smc, "auto_plan", False)
