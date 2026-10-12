from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from blindspot.batch_api import BatchStore, router
from blindspot.runs import RUNS_DIR

# The answering screen: plain HTML, CSS, and JavaScript modules, no build step.
_WEB_DIR = Path(__file__).parent / "web"
# Only this app's own files may load; no inline scripts or styles, no framing.
_UI_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


def create_app(runs_dir: Path = RUNS_DIR) -> FastAPI:
    """The HTTP app. `runs_dir` is where saved batches are found (tests pass a temp dir)."""
    app = FastAPI(title="Blindspot")
    app.state.batch_store = BatchStore(runs_dir)
    app.include_router(router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui")

    @app.get("/ui", include_in_schema=False)
    def ui() -> FileResponse:
        return FileResponse(_WEB_DIR / "index.html", headers=_UI_HEADERS)

    app.mount("/ui/assets", StaticFiles(directory=_WEB_DIR / "assets"), name="ui-assets")
    return app


app = create_app()
