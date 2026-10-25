from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from .config import Settings
from .database import database_engine
from .migrate import schema_head
from .auth_routes import install_auth
from .admin_routes import install_admin
from .registry_routes import install_registry
from .signer import install_signer
from .operation_routes import install_operations
from importlib.resources import files
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


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

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path not in ("/api/signer/claim", "/api/signer/result"):
            if request.headers.get("origin") != settings.public_url or request.headers.get("X-Requested-With") != "marking-erp":
                return JSONResponse({"detail": "Недопустимый источник запроса"}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"})
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"detail": [{"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]} for item in error.errors()]}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": "Проверьте введённые данные"}, status_code=422)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        return JSONResponse({"detail": "База данных временно недоступна"}, status_code=503)

    app.state.auth_dependency = install_auth(app, engine, settings)
    install_admin(app, app.state.auth_dependency)
    install_registry(app, app.state.auth_dependency, settings)
    install_signer(app,engine)
    install_operations(app,app.state.auth_dependency)
    static = files("marking_erp").joinpath("static")
    app.mount("/assets", StaticFiles(directory=str(static)), name="assets")

    @app.get("/")
    @app.get("/login")
    @app.get("/profile")
    @app.get("/users")
    @app.get("/groups")
    @app.get("/rules")
    @app.get("/codes")
    @app.get("/operations")
    def shell():
        # Public shell contains no account data. All data/actions require the API session.
        return FileResponse(str(static.joinpath("index.html")))

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

