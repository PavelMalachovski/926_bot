import logging

from app.core.logging import configure_logging


def test_httpx_request_logging_is_silenced():
    configure_logging()
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING
