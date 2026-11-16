import hashlib
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.bootstrap import PASSWORD_HASH, bootstrap
from app.config import Settings
from app.database import create_database_engine, session_factory
from app.models import LoginSession, User

COOKIE_NAME = "myl_session"


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_database_engine(settings.database_url)
    factory = session_factory(engine)
    attempts: dict[str, deque] = defaultdict(deque)
    attempt_lock = Lock()
    dummy_hash = PASSWORD_HASH.hash(secrets.token_urlsafe(24))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bootstrap(settings)
        yield
        engine.dispose()

    app = FastAPI(
        title="MYL Inc. Compliance Monitor",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.engine = engine

    def database():
        with factory() as db:
            yield db

    def browser_write(request: Request):
        if request.headers.get("x-requested-with") != "XMLHttpRequest":
            raise HTTPException(403, "Same-origin request header required")
        origin = request.headers.get("origin")
        if (origin and origin != settings.app_origin) or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Request origin not allowed")

    def current_session(request: Request, db: Session = Depends(database)):
        token = request.cookies.get(COOKIE_NAME)
        login = db.get(LoginSession, hash_token(token)) if token else None
        if login is None or login.expires_at <= int(time.time()):
            raise HTTPException(401, "Please sign in")
        user = db.get(User, login.user_id)
        if user is None:
            raise HTTPException(401, "Please sign in")
        return user, login

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/ready")
    def ready(db: Session = Depends(database)):
        try:
            db.execute(text("SELECT id FROM users LIMIT 1"))
        except SQLAlchemyError:
            raise HTTPException(503, "Database is not ready") from None
        return {"status": "ready", "database": "connected"}

    @app.post("/api/auth/login", dependencies=[Depends(browser_write)])
    def login(payload: LoginInput, request: Request, response: Response, db: Session = Depends(database)):
        now = int(time.time())
        # Single-process, bounded login throttle for this local demo.
        key = request.client.host if request.client else "local"
        with attempt_lock:
            for old_key in list(attempts):
                while attempts[old_key] and attempts[old_key][0] <= now - 60:
                    attempts[old_key].popleft()
                if not attempts[old_key]:
                    del attempts[old_key]
            if len(attempts[key]) >= 10:
                raise HTTPException(429, "Too many login attempts; try again in one minute", headers={"Retry-After": "60"})
            attempts[key].append(now)
        user = db.scalar(select(User).where(User.username == payload.username))
        valid = PASSWORD_HASH.verify(payload.password, user.password_hash if user else dummy_hash)
        if not valid or user is None:
            raise HTTPException(401, "Invalid username or password")
        old_token = request.cookies.get(COOKIE_NAME)
        if old_token:
            db.execute(delete(LoginSession).where(LoginSession.token_hash == hash_token(old_token)))
        db.execute(delete(LoginSession).where(LoginSession.expires_at <= now))
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        db.add(LoginSession(token_hash=hash_token(token), user_id=user.id, csrf_token=csrf, expires_at=now + settings.session_hours * 3600))
        db.commit()
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict", secure=settings.cookie_secure, max_age=settings.session_hours * 3600, path="/api")
        return {"username": user.username, "csrf_token": csrf}

    @app.get("/api/auth/me")
    def me(identity=Depends(current_session)):
        user, login = identity
        return {"username": user.username, "csrf_token": login.csrf_token}

    @app.post("/api/auth/logout", status_code=204, dependencies=[Depends(browser_write)])
    def logout(request: Request, response: Response, identity=Depends(current_session), db: Session = Depends(database)):
        _, login = identity
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), login.csrf_token):
            raise HTTPException(403, "Invalid CSRF token")
        db.execute(delete(LoginSession).where(LoginSession.token_hash == login.token_hash))
        db.commit()
        response.delete_cookie(COOKIE_NAME, path="/api", httponly=True, samesite="strict", secure=settings.cookie_secure)

    @app.get("/api/system", dependencies=[Depends(current_session)])
    def system():
        return {"organization": "MYL Inc.", "phase": 1, "frameworks": ["GDPR", "PCI DSS"], "inventory_seeded": False}

    return app
