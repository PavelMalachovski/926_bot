import pytest

from app.services.smc import i18n


@pytest.fixture(autouse=True)
def _english_messages():
    """The bot renders Russian by default (owner request 2026-09-10); the
    suite asserts on the English text, which is also the catalog key, so
    every test runs in English unless it switches languages itself.
    `test_i18n.py` covers the Russian side."""
    previous = i18n.get_language()
    i18n.set_language("en")
    yield
    i18n.set_language(previous)
