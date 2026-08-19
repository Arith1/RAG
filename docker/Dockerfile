# ── 构建阶段：安装 Python 依赖（uv）──
FROM python:3.14-slim

# 复制 uv 到镜像（官方推荐方式）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# 只安装 pyproject.toml 声明的依赖（不安装项目本身——包目录无 __init__.py，
# 运行时以源码目录方式 import，代码直接 COPY 进镜像即可）
COPY ../pyproject.toml ./
RUN uv pip install --system --no-cache-dir -r pyproject.toml

# 拷贝应用代码（.dockerignore 已排除 .venv/.git/resources 等）
COPY .. .

# 启动 FastAPI 服务
EXPOSE 8010
CMD ["uvicorn", "rag个人知识库.api.main:app", "--host", "0.0.0.0", "--port", "8010"]
