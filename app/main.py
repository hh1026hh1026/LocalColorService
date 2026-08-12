"""
Main Entry Point for Local Color Service FastAPI Application.
"""

from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.config import settings
from app.api.endpoints import router as api_router
from workers.job_worker import JobWorker

STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = settings.DATA_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"Starting {settings.PROJECT_NAME} v{settings.VERSION}...")
    # Two lanes: renders and model inference on one, short interactive jobs on
    # the other. With a single queue a one-second reference preflight could sit
    # behind a fifteen-minute render and look like a failure from the UI.
    workers = [JobWorker(lane="heavy"), JobWorker(lane="light")]
    app.state.job_worker = workers[0]
    app.state.job_workers = workers
    for worker in workers:
        worker.start()
    try:
        yield
    finally:
        print("Shutting down Local Color Service background workers...")
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(timeout=15.0)
            if worker.is_alive():
                # ``stop`` has already released the lease and requested
                # cancellation, so a future process can recover safely even
                # if an external library delays this daemon thread.
                print(f"Worker {worker.name} did not stop before timeout; job lease was released.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Local Color Grading Engine & REST API Service for Video and Images",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# Enable CORS for local clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API Routers
app.include_router(api_router)

# Mount Static Files & Media View Routes
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/data/view", StaticFiles(directory=str(DATA_DIR)), name="data_view")


@app.get("/", response_class=FileResponse, include_in_schema=False)
def serve_home_ui():
    """Serves the interactive Color Grading Workbench Web UI."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/tasks", response_class=FileResponse, include_in_schema=False)
def serve_task_manager():
    """Dedicated operational task manager, independent of the grading UI."""
    return FileResponse(STATIC_DIR / "tasks.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)
