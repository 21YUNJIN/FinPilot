"""应用装配（A 的服务装配入口）。

- 统一异常 → 信封；未捕获异常不泄露堆栈与数据库细节。
- 启动时注册提供方适配器；适配器能力不足时自动金融写入关闭（由内核强制）。
- 可选挂载两客户端的构建产物（存在时），便于同一后端做端到端验收。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .envelope import error_envelope
from .errors import FinPilotError
from .gateway import auth, callbacks, domain_routes, operator, privacy, tasks
from .timeutil import format_iso, utcnow

logger = logging.getLogger("finpilot.app")

REPO_ROOT = Path(__file__).resolve().parents[4]


def register_adapters() -> dict:
    """注册可用的提供方适配器；记录结果供 /healthz 展示。

    实现移到 ``adapters.registry``，以便 Outbox / 定时任务等**独立进程**复用同一份
    注册逻辑（只在这里注册会导致 Worker 进程找不到适配器）。
    """
    from .adapters.registry import register_adapters as _register

    return _register()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="融策智能体 FinPilot API",
        version="1.0.0",
        description="个人银行智能体的可信执行后端：目标约束编译、分级授权、双重记账、异步恢复与评测。",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:5174", "http://localhost:5174"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(tasks.router)
    app.include_router(domain_routes.router)
    app.include_router(operator.router)
    app.include_router(privacy.router)
    app.include_router(callbacks.router)

    @app.exception_handler(FinPilotError)
    async def _finpilot_error_handler(request: Request, exc: FinPilotError) -> JSONResponse:
        if exc.http_status and exc.http_status >= 500:
            logger.error("finpilot error %s on %s", exc.code, request.url.path)
        return JSONResponse(status_code=exc.http_status or 500, content=error_envelope(exc))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover - 兜底
        logger.exception("unhandled error on %s", request.url.path)
        safe = FinPilotError("SYSTEM_ERROR", "系统内部错误，未产生资金效果。")
        return JSONResponse(status_code=500, content=error_envelope(safe))

    @app.get("/healthz", tags=["meta"])
    def healthz():
        from .db import healthcheck

        try:
            database = healthcheck()
        except Exception as exc:  # pragma: no cover
            database = {"ok": False, "error": type(exc).__name__}
        return {
            "status": "ok" if database.get("ok") else "degraded",
            "mode": settings.app_mode,
            "environment": settings.environment,
            "database": database,
            "adapters": register_adapters(),
            "observed_at": format_iso(utcnow()),
            "labeling": "模拟渠道与虚拟资金；mock/offline_replay/live 模式在任务与响应中明确标注",
        }

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse(url="/docs")

    # 可选：挂载客户端构建产物（存在才挂载）
    for name, dist in (("app", REPO_ROOT / "apps" / "web" / "dist"), ("im", REPO_ROOT / "apps" / "im" / "dist")):
        if dist.is_dir():
            app.mount(f"/{name}", StaticFiles(directory=str(dist), html=True), name=name)

    return app


app = create_app()
