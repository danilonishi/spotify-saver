"""Initial administrator setup and session login endpoints."""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ...config import Config
from ..services.security_store import SESSION_LIFETIME, SecurityStore

router = APIRouter()
SESSION_COOKIE = "spotifysaver_session"


class InitialSetup(BaseModel):
    setup_token: str = Field(min_length=20, max_length=100)
    username: str = Field(default="admin", min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=256)


class LoginCredentials(BaseModel):
    username: str = Field(default="admin", min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


def _store(request: Request) -> SecurityStore:
    store: SecurityStore | None = getattr(request.app.state, "security_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Authentication is unavailable")
    return store


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0]
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        secure=request.url.scheme == "https" or forwarded_proto.strip() == "https",
        samesite="strict",
        path="/",
    )


@router.get("/auth/status")
async def get_authentication_status(request: Request):
    """Report whether first-run setup is complete and this browser is signed in."""
    store = _store(request)
    return {
        "configured": store.is_configured(),
        "authenticated": store.is_valid_session(
            request.cookies.get(SESSION_COOKIE)
        ),
        "auth_mode": "sso" if Config.SSO_ENABLED else "local",
    }


@router.post("/auth/setup")
async def complete_initial_setup(
    settings: InitialSetup, request: Request, response: Response
):
    """Set the one-time administrator password using the startup token."""
    store = _store(request)
    if store.is_configured():
        raise HTTPException(status_code=409, detail="Initial setup is already complete")
    if not store.complete_setup(
        settings.setup_token, settings.password, username=settings.username
    ):
        raise HTTPException(status_code=403, detail="Setup token is invalid or expired")
    token, expires_at = store.create_session()
    _set_session_cookie(response, request, token)
    return {"configured": True, "authenticated": True, "expires_at": expires_at}


@router.post("/auth/login")
async def login(
    settings: LoginCredentials, request: Request, response: Response
):
    """Authenticate the administrator and issue a persistent HttpOnly session."""
    store = _store(request)
    if not store.is_configured():
        raise HTTPException(status_code=409, detail="Initial setup is required")
    client_key = _client_key(request)
    if store.login_is_blocked(client_key):
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts; try again in 15 minutes",
        )
    if not store.verify_password(settings.password, username=settings.username):
        store.record_login_failure(client_key)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    store.clear_login_failures(client_key)
    token, expires_at = store.create_session()
    _set_session_cookie(response, request, token)
    return {"authenticated": True, "expires_at": expires_at}


@router.post("/auth/password")
async def change_password(settings: PasswordChange, request: Request):
    """Change the administrator password and sign out all other sessions."""
    store = _store(request)
    client_key = _client_key(request)
    if store.login_is_blocked(client_key):
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts; try again in 15 minutes",
        )
    if not store.change_password(
        settings.current_password,
        settings.new_password,
        request.cookies.get(SESSION_COOKIE),
    ):
        store.record_login_failure(client_key)
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    store.clear_login_failures(client_key)
    return {"changed": True}


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    """Revoke the current server-side session and clear its browser cookie."""
    store = _store(request)
    store.revoke_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=(
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
            == "https"
        ),
        samesite="strict",
        path="/",
    )
    return {"authenticated": False}
