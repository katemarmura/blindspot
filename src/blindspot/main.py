from pathlib import Path

from fastapi import FastAPI

from blindspot.batch_api import BatchStore, router
from blindspot.runs import RUNS_DIR


def create_app(runs_dir: Path = RUNS_DIR) -> FastAPI:
    """The HTTP app. `runs_dir` is where saved batches are found (tests pass a temp dir)."""
    app = FastAPI(title="Blindspot")
    app.state.batch_store = BatchStore(runs_dir)
    app.include_router(router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
