"""FastAPI 应用入口。

启动顺序：等待数据库 -> 幂等初始化 schema -> 对外提供 HTTP API。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .compliance.engine import ComplianceError
from .config import settings
from .db import initialize_schema, wait_for_database
from .routers import router
from .storage import ServiceError

app = FastAPI(
    title="铁路司机值勤合规核算服务",
    version="1.0.0",
    description=(
        "录入司机值勤片段（驾驶 / 其他值勤 / 休息），按任意滚动 24 小时 10 小时上限、"
        "驾驶运行段 4 小时上限（连续休息满 30 分钟重置）核算合规；支持修订更正、"
        "审核发布固化与历史版本对照。"
    ),
)


@app.on_event("startup")
def on_startup() -> None:
    wait_for_database()
    initialize_schema()


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "detail": exc.detail}},
    )


@app.exception_handler(ComplianceError)
async def compliance_error_handler(request: Request, exc: ComplianceError):
    # 片段结构性错误（重叠等）；正常情况下数据库 GiST 约束会先于引擎拦截，
    # 此处为引擎直连计算时的兜底
    return JSONResponse(
        status_code=409,
        content={"error": {"code": "invalid_segments", "detail": str(exc)}},
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root():
    return {"service": "railway-duty-compliance", "docs": "/docs", "api": "/api/v1"}


app.include_router(router)


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.http_host,
        port=settings.http_port,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
