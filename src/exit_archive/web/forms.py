"""Validation of POST /jobs fields. Messages are fixed text: submitted input is never echoed."""

import re
from dataclasses import dataclass

from exit_archive.secrets import HarvestToken

__all__ = ["BadInputError", "JobForm", "JOB_ID", "parse_job_form"]

MAX_TOKEN_LEN = 512
MAX_ACCOUNT_DIGITS = 18
JOB_ID = re.compile(r"[A-Za-z0-9_-]{20,128}")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,255}")


class BadInputError(Exception):
    """Invalid form input; ``str()`` is a safe, fixed sentence."""


@dataclass(frozen=True)
class JobForm:
    token: HarvestToken
    account_id: int | None
    session_id: str | None


def parse_job_form(token: str, account_id: str, session_id: str) -> JobForm:
    """Validate the three fields; the token is wrapped before anything else sees it."""
    raw = token.strip()
    if not raw:
        raise BadInputError("Please paste your Harvest personal access token.")
    if len(raw) > MAX_TOKEN_LEN:
        raise BadInputError("That token is too long to be a Harvest token.")
    wrapped = HarvestToken(raw)
    account = account_id.strip()
    if account and not (
        account.isascii() and account.isdigit() and len(account) <= MAX_ACCOUNT_DIGITS
    ):
        raise BadInputError("The account ID must contain digits only.")
    session = session_id.strip()
    if session and not _SESSION_ID.fullmatch(session):
        raise BadInputError("The payment session reference is not valid.")
    return JobForm(wrapped, int(account) if account else None, session or None)
