import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import (
    delete_rule,
    finish_extract,
    get_db,
    get_pipeline,
    get_rule,
    init_db,
    list_pipelines,
    list_rules,
    mask_detail,
    overview,
    run_extract,
    run_load,
    save_rule,
    set_policy,
    start_extract,
    submit_mask,
    update_rule,
)
from app.masking import DomainError, STRENGTHS, MASK_TYPES

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-etl-session")

app = FastAPI(title="cc-etl-lab")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("etl_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _domain_error(exc: DomainError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": exc.message}, status_code=exc.status_code)


@app.exception_handler(DomainError)
async def _handle_domain_error(request: Request, exc: DomainError):
    return _domain_error(exc)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/")
def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return RedirectResponse("/app/", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/app/")
def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC / "index.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return JSONResponse({"ok": False, "error": "账号或密码错误"}, status_code=400)
    resp = JSONResponse({"ok": True, "user": user})
    resp.set_cookie(
        "etl_session",
        serializer.dumps({"user": user}),
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("etl_session")
    return resp


@app.get("/api/me")
def api_me(user: str = Depends(require_user)):
    return {"user": user, "lab": Config.LAB_NAME, "domain": "数据工程"}


@app.get("/api/pipelines")
def api_pipelines(user: str = Depends(require_user)):
    with get_db() as conn:
        return {"items": list_pipelines(conn)}


@app.get("/api/pipelines/{pipeline_id}")
def api_pipeline_detail(pipeline_id: int, user: str = Depends(require_user)):
    with get_db() as conn:
        item = get_pipeline(conn, pipeline_id)
    if not item:
        raise HTTPException(status_code=404, detail="管道不存在")
    return item


@app.post("/api/pipelines/{pipeline_id}/extract")
def api_extract(pipeline_id: int, user: str = Depends(require_user)):
    with get_db() as conn:
        item = run_extract(conn, pipeline_id)
    if not item:
        raise HTTPException(status_code=404, detail="管道不存在")
    return item


@app.post("/api/pipelines/{pipeline_id}/extract/start")
def api_extract_start(pipeline_id: int, user: str = Depends(require_user)):
    with get_db() as conn:
        item = start_extract(conn, pipeline_id)
    if not item:
        raise HTTPException(status_code=404, detail="管道不存在")
    return item


@app.post("/api/pipelines/{pipeline_id}/extract/finish")
def api_extract_finish(pipeline_id: int, user: str = Depends(require_user)):
    with get_db() as conn:
        item = finish_extract(conn, pipeline_id)
    if not item:
        raise HTTPException(status_code=404, detail="管道不存在")
    return item


@app.post("/api/pipelines/{pipeline_id}/load")
async def api_load(pipeline_id: int, request: Request, user: str = Depends(require_user)):
    body = await _json_body(request)
    try:
        with get_db() as conn:
            item = run_load(conn, pipeline_id, body)
    except DomainError as exc:
        return _domain_error(exc)
    if not item:
        raise HTTPException(status_code=404, detail="管道不存在")
    return item


# ---------------------------------------------------------------- 脱敏


@app.get("/api/masking/overview")
def api_mask_overview(user: str = Depends(require_user)):
    with get_db() as conn:
        return overview(conn)


@app.get("/api/masking/options")
def api_mask_options(user: str = Depends(require_user)):
    return {
        "strengths": list(STRENGTHS.values()),
        "mask_types": [{"key": k, "name": v} for k, v in MASK_TYPES.items()],
    }


@app.get("/api/masking/policy")
def api_get_policy(user: str = Depends(require_user)):
    with get_db() as conn:
        from app.db import get_policy

        return get_policy(conn)


@app.post("/api/masking/policy")
async def api_set_policy(request: Request, user: str = Depends(require_user)):
    body = await _json_body(request)
    raw = body.get("strength", body.get("level", body.get("name")))
    try:
        with get_db() as conn:
            return set_policy(conn, raw)
    except DomainError as exc:
        return _domain_error(exc)


@app.get("/api/masking/rules")
def api_rules(user: str = Depends(require_user)):
    with get_db() as conn:
        return {"items": list_rules(conn)}


@app.post("/api/masking/rules")
async def api_create_rule(request: Request, user: str = Depends(require_user)):
    body = await _json_body(request)
    try:
        with get_db() as conn:
            return save_rule(conn, body)
    except DomainError as exc:
        return _domain_error(exc)


@app.put("/api/masking/rules/{rule_id}")
async def api_update_rule(rule_id: int, request: Request, user: str = Depends(require_user)):
    body = await _json_body(request)
    try:
        with get_db() as conn:
            rule = update_rule(conn, rule_id, body)
    except DomainError as exc:
        return _domain_error(exc)
    if not rule:
        raise HTTPException(status_code=404, detail="脱敏规则不存在")
    return rule


@app.delete("/api/masking/rules/{rule_id}")
def api_delete_rule(rule_id: int, user: str = Depends(require_user)):
    with get_db() as conn:
        try:
            ok = delete_rule(conn, rule_id)
        except DomainError as exc:
            return _domain_error(exc)
    if not ok:
        raise HTTPException(status_code=404, detail="脱敏规则不存在")
    return {"ok": True}


@app.get("/api/pipelines/{pipeline_id}/mask/sample")
def api_mask_sample(pipeline_id: int, user: str = Depends(require_user)):
    with get_db() as conn:
        item = get_pipeline(conn, pipeline_id)
        if not item:
            raise HTTPException(status_code=404, detail="管道不存在")
        rows = mask_detail(conn, pipeline_id)["sample"]
    return {"items": rows, "count": len(rows)}


@app.post("/api/pipelines/{pipeline_id}/mask")
async def api_mask_submit(pipeline_id: int, request: Request, user: str = Depends(require_user)):
    body = await _json_body(request)
    try:
        with get_db() as conn:
            result = submit_mask(conn, pipeline_id, body.get("claimer") or user, body)
    except DomainError as exc:
        return _domain_error(exc)
    if not result:
        raise HTTPException(status_code=404, detail="管道不存在")
    return result


@app.post("/api/pipelines/{pipeline_id}/mask/claim")
async def api_mask_claim(pipeline_id: int, request: Request, user: str = Depends(require_user)):
    """与 /mask 等价，便于外部按“争抢提交”语义调用。"""
    return await api_mask_submit(pipeline_id, request, user)


@app.get("/api/pipelines/{pipeline_id}/mask")
def api_mask_detail(pipeline_id: int, user: str = Depends(require_user)):
    with get_db() as conn:
        detail = mask_detail(conn, pipeline_id)
    if not detail:
        raise HTTPException(status_code=404, detail="管道不存在")
    return detail


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def create_app() -> FastAPI:
    return app
