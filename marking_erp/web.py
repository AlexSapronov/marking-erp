from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from .config import Settings
from .database import database_engine
from .migrate import schema_head


def create_app(settings=None, engine=None):
    settings = settings or Settings()
    owned_engine = engine is None
    engine = engine or database_engine(settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owned_engine:
            engine.dispose()

    app = FastAPI(title="Marking ERP infrastructure", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        try:
            with engine.connect() as connection:
                versions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            if versions != [schema_head()]:
                return JSONResponse({"status": "not_ready", "reason": "schema_revision_mismatch"}, status_code=503)
        except SQLAlchemyError:
            return JSONResponse({"status": "not_ready", "reason": "database_unavailable_or_unmigrated"}, status_code=503)
        return {"status": "ready", "schema_revision": versions[0], "build_sha": settings.build_sha}

    return app

