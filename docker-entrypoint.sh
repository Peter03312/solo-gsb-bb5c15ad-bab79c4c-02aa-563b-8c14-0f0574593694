#!/bin/sh
# 启动时自动建表并创建初始账号（若数据库为空），然后运行服务。
set -e

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
