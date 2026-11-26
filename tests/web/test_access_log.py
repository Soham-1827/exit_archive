"""uvicorn's access-log formatter works with the redaction hook installed (W1)."""

import logging
from pathlib import Path

import pytest
from uvicorn.logging import AccessFormatter

from tests.web.helpers import TOKEN, build, settings_for

ARGS = ("127.0.0.1:1", "GET", "/healthz", "1.1", 200)


def _format(record: logging.LogRecord) -> str:
    return AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(record)


def test_access_formatter_formats_record_made_under_hook(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)):
        record = logging.getLogger("uvicorn.access").makeRecord(
            "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', ARGS, None
        )
        assert record.args == ARGS
        assert "GET /healthz" in _format(record)


def test_access_line_with_bearer_in_path_is_scrubbed_and_still_formats(tmp_path: Path) -> None:
    with build(settings_for(tmp_path)):
        args = (ARGS[0], "GET", "/x?a=Bearer " + TOKEN, "1.1", 200)
        record = logging.getLogger("uvicorn.access").makeRecord(
            "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None
        )
        text = _format(record)
        assert TOKEN not in text
        assert "GET" in text


def test_real_access_logger_reports_no_logging_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
    logger = logging.getLogger("uvicorn.access")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with build(settings_for(tmp_path)):
            logger.info('%s - "%s %s HTTP/%s" %d', ARGS[0], "GET", "/SILENCED-MARKER", "1.1", 200)
        logger.info('%s - "%s %s HTTP/%s" %d', *ARGS)
    finally:
        logger.removeHandler(handler)
    err = capsys.readouterr().err
    assert "Logging error" not in err
    assert "SILENCED-MARKER" not in err
    assert 'GET /healthz HTTP/1.1" 200' in err
