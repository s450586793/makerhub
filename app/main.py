from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.config import router as config_router
from app.api.web import router as web_router
from app.core.settings import ARCHIVE_DIR, ROOT_DIR, ensure_app_dirs


ensure_app_dirs()

app = FastAPI(title="makerhub", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "app" / "static")), name="static")
app.mount("/archive", StaticFiles(directory=str(ARCHIVE_DIR)), name="archive")
app.include_router(web_router)
app.include_router(config_router)
