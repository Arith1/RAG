# ── 构建阶段：安装 Python 依赖（uv，锁定 uv.lock 保证可复现）──
# python:3.14-slim 与 pyproject.toml 的 requires-python = ">=3.14" 保持一致
# （项目本地环境也是 Python 3.14，容器与本地解释器对齐避免依赖编译差异）
FROM python:3.14.5-slim

# 日志直接输出到 stdout（uvicorn 可被 docker logs 捕获），不写 __pycache__；
# PYTHONIOENCODING=utf-8：Windows 宿主/控制台 GBK 下避免中文日志乱码（E4）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8

# 复制 uv 到镜像（固定版本号，避免 :latest 漂移导致构建不可重现）
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /uvx /bin/

WORKDIR /app

# E2：从已入库的 uv.lock 冻结安装依赖（--frozen 锁版本，构建可复现）。
# --no-install-project：不安装项目本身（包目录无 __init__.py，运行时以源码目录方式 import）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

# E3：非 root 运行（创建应用用户；resources 数据卷需对该用户可写，否则见 README 调整）
RUN useradd --create-home --uid 10001 appuser

# 拷贝应用代码（.dockerignore 已排除 .venv/.git/resources 等；.venv 为构建产物保留）
COPY . .

USER appuser

# 启动 FastAPI 服务（用锁定的 venv 解释器）
EXPOSE 8010
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "rag个人知识库.api.main:app", "--host", "0.0.0.0", "--port", "8010"]
