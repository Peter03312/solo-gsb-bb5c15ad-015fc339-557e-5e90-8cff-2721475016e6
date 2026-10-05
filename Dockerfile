FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SCHEMA_PATH=/app/db/init.sql

WORKDIR /app

# 系统依赖：psycopg2-binary 自带 libpq，无需额外 apt 包
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY db /app/db

EXPOSE 8000

# 容器内等待数据库并幂等初始化 schema（init.sql 同时挂载给 postgres 首启执行）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
