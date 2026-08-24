# 镜像先安装可复现的运行期 lock，再拷贝源码，这样应用模块改动不会让依赖层缓存失效。
# 最后一步的包安装使用 --no-deps，因为 requirements.lock 已经为这个 Linux 环境解析并安装了
# 完整依赖图；再解析一次只会引入与 lock 不一致的版本。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace

COPY requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.lock

COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
COPY mcp_server ./mcp_server
COPY data ./data

# 安装项目 wheel 是为了验证 app/ 与 mcp_server/ 都真的进入了可分发包：只依赖工作目录运行的话，
# 打包遗漏要等到部署时才暴露，而 MCP 子进程正是用 `python -m mcp_server.server` 启动的。
RUN python -m pip install --no-cache-dir --no-deps .

EXPOSE 8000

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
