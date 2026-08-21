# ── 构建阶段：安装 Python 依赖（uv）──
# python:3.14-slim 与 pyproject.toml 的 requires-python = ">=3.14" 保持一致
# （项目本地环境也是 Python 3.14，容器与本地解释器对齐避免依赖编译差异）
FROM python:3.14-slim

# 复制 uv 到镜像（固定版本号，避免 :latest 漂移导致构建不可重现）
COPY --from=ghcr.io/astral-sh/uv:0.5.29 /uv /uvx /bin/

WORKDIR /app

# 只安装 pyproject.toml 声明的依赖（不安装项目本身——包目录无 __init__.py，
# 运行时以源码目录方式 import，代码直接 COPY 进镜像即可）
COPY pyproject.toml ./
RUN uv pip install --system --no-cache-dir -r pyproject.toml

# 拷贝应用代码（.dockerignore 已排除 .venv/.git/resources 等）
COPY . .

# 启动 FastAPI 服务
EXPOSE 8010
CMD ["uvicorn", "rag个人知识库.api.main:app", "--host", "0.0.0.0", "--port", "8010"]
