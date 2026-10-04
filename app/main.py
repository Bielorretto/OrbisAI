"""Point d'entrée : `uvicorn app.main:app --reload`"""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect

from app import config
from app.db import Base, engine
from app.routes import billing, matters, pages, tasks
from app.routes.common import Forbidden, LoginRequired, redirect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not _schema_is_current():
        # Prototype sans migrations : base vide ou schéma modifié -> on recrée les données de démo
        from app.seed import reset_database
        reset_database()
        logging.getLogger("loickaton").info("Base absente ou schéma modifié : données de démo recréées.")
    loop_task = asyncio.create_task(_scheduler()) if config.SCHEDULER_ENABLED else None
    yield
    if loop_task:
        loop_task.cancel()
        with suppress(asyncio.CancelledError):
            await loop_task


def _schema_is_current() -> bool:
    """Toutes les tables et colonnes du modèle existent-elles dans la base ?"""
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            return False
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        if not {c.name for c in table.columns} <= existing:
            return False
    return True


async def _scheduler():
    from app.services.scheduler import loop
    await loop()


app = FastAPI(title="Loickaton", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(pages.router)
app.include_router(matters.router)
app.include_router(tasks.router)
app.include_router(billing.router)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, _: LoginRequired):
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(_: Request, __: Forbidden):
    return redirect("/", "Accès non autorisé pour votre rôle.", error=True)
