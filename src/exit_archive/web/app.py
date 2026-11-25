"""FastAPI app factory. Importing this module never reads or validates the environment."""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.exceptions import HTTPException as StarletteHTTPException

from exit_archive.archive.writer import resolve_inside
from exit_archive.errors import ExportError, SessionInUseError
from exit_archive.exporter import export_account
from exit_archive.jobs.cleanup import run_cleanup_loop, sweep
from exit_archive.jobs.runner import ARCHIVES_DIR, ExportFn, JobRunner
from exit_archive.jobs.store import Job, JobStore, to_iso, utcnow
from exit_archive.secrets import install_log_redaction
from exit_archive.web.forms import JOB_ID, BadInputError, parse_job_form
from exit_archive.web.payments import AllowAllVerifier, PaymentVerifier
from exit_archive.web.settings import Settings

__all__ = ["PaymentVerifier", "app", "create_app"]

_HERE = Path(__file__).parent
_log = logging.getLogger(__name__)
_MESSAGES = {
    400: "That request could not be processed.",
    402: "Payment could not be confirmed for this request.",
    404: "We could not find that page.",
    405: "That action is not allowed here.",
    409: "This payment has already been used for an export.",
    410: "This archive has expired and was deleted. Archives are kept for 7 days.",
    422: "The form was incomplete. Go back and fill in the token field.",
    503: "A service we depend on is not available. Please try again in a minute.",
}
_GENERIC = "Something went wrong on our side. Please try again."


def _templates() -> Jinja2Templates:
    env = Environment(
        loader=FileSystemLoader(_HERE / "templates"),
        autoescape=select_autoescape(["html"], default=True),
    )
    return Jinja2Templates(env=env)


async def _lookup_job(request: Request, job_id: str) -> Job | None:
    """Fetch a job; ids that are not the capability-URL shape 404 before any DB lookup."""
    if not JOB_ID.fullmatch(job_id):
        raise HTTPException(404)
    job: Job | None = await asyncio.to_thread(request.app.state.store.get, job_id)
    return job


def _archive_file(data_dir: Path, job: Job) -> Path:
    """Resolve the stored relative path inside ``archives/``; refuse links and escapes."""
    root = data_dir / ARCHIVES_DIR
    if root.is_symlink() or job.archive_path is None:
        raise HTTPException(404)
    relative = Path(job.archive_path)
    if relative.is_absolute() or relative.parts[:1] != (ARCHIVES_DIR,):
        raise HTTPException(404)
    try:
        resolved = resolve_inside(root, *relative.parts[1:])
    except ExportError:
        raise HTTPException(404) from None
    walked = data_dir
    for part in relative.parts:
        walked = walked / part
        if walked.is_symlink():
            raise HTTPException(404)
    if not resolved.is_file():
        raise HTTPException(404)
    return resolved


def create_app(
    settings: Settings | None = None,
    export: ExportFn = export_account,
    payments: PaymentVerifier | None = None,
) -> FastAPI:
    templates = _templates()

    def page(request: Request, name: str, status: int = 200, **context: object) -> Response:
        return templates.TemplateResponse(request, name, context, status_code=status)

    def error_page(request: Request, status: int, message: str | None = None) -> Response:
        text = message or _MESSAGES.get(status, _GENERIC)
        return page(request, "error.html", status, code=status, message=text)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or Settings.from_env(os.environ)
        uninstall = install_log_redaction(
            None
        )  # Bearer-only; per-job tokens are added by the runner
        try:
            for name in ("httpx", "httpcore"):
                logging.getLogger(name).setLevel(logging.WARNING)
            (cfg.data_dir / ARCHIVES_DIR).mkdir(parents=True, exist_ok=True)
            store = JobStore(cfg.data_dir / "jobs.sqlite3")
            runner = JobRunner(store, cfg.data_dir, cfg.max_concurrent_jobs, export=export)
            runner.recover_on_startup()
            await asyncio.to_thread(sweep, store, cfg.data_dir, utcnow())
            cleanup = asyncio.create_task(run_cleanup_loop(store, cfg.data_dir, initial_delay=3600))
            app.state.settings = cfg
            app.state.store = store
            app.state.runner = runner
            app.state.verifier = AllowAllVerifier() if not cfg.payment_required else payments
            try:
                yield
            finally:
                await runner.aclose()
                cleanup.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cleanup
        finally:
            uninstall()

    app = FastAPI(
        title="Exit Archive", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        return error_page(request, 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        response = error_page(request, exc.status_code)
        response.headers.update(exc.headers or {})
        return response

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        _log.warning("unhandled error (%s)", type(exc).__name__)
        return error_page(request, 500)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/")
    async def landing(request: Request) -> Response:
        cfg: Settings = request.app.state.settings
        buy = "/start" if not cfg.payment_required else cfg.stripe_payment_link_url
        return page(request, "index.html", buy_url=buy)

    @app.get("/privacy")
    async def privacy(request: Request) -> Response:
        return page(request, "privacy.html")

    @app.get("/start")
    async def start(request: Request, session_id: str = "") -> Response:
        return page(request, "start.html", session_id=session_id[:255])

    @app.post("/jobs")
    async def create_job(
        request: Request,
        token: Annotated[str, Form()],
        account_id: Annotated[str, Form()] = "",
        session_id: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            form = parse_job_form(token, account_id, session_id)
        except BadInputError as exc:
            return error_page(request, 400, str(exc))
        verifier: PaymentVerifier | None = request.app.state.verifier
        if verifier is None or (
            request.app.state.settings.payment_required and not form.session_id
        ):
            return error_page(request, 402)
        try:
            paid = await verifier.verify(form.session_id or "")
        except Exception as exc:  # noqa: BLE001 - log the type only
            _log.warning("payment check failed (%s)", type(exc).__name__)
            return error_page(request, 503)
        if paid is not True:  # strictly True: a truthy non-bool never creates a job
            return error_page(request, 402)
        try:
            job_id = await request.app.state.runner.submit(
                form.token, form.account_id, form.session_id
            )
        except SessionInUseError:
            return error_page(request, 409)
        except Exception as exc:  # noqa: BLE001 - keep token-bearing frames from escaping
            _log.warning("job submit failed (%s)", type(exc).__name__)
            return error_page(request, 500)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}")
    async def job_page(request: Request, job_id: str) -> Response:
        job = await _lookup_job(request, job_id)
        if job is None:
            raise HTTPException(404)
        expires = job.expires_at[:10] if job.expires_at else None
        return page(request, "progress.html", job=job, expires=expires)

    @app.get("/jobs/{job_id}/download")
    async def download(request: Request, job_id: str) -> Response:
        job = await _lookup_job(request, job_id)
        if job is None:
            raise HTTPException(404)
        if job.status == "expired" or (
            job.status == "done" and (job.expires_at or "") < to_iso(utcnow())
        ):
            raise HTTPException(410)
        if job.status != "done":
            raise HTTPException(404)
        path = _archive_file(request.app.state.settings.data_dir, job)
        return FileResponse(path, media_type="application/zip", filename=path.name)

    return app


app = create_app()
