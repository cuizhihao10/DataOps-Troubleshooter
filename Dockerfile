# The image installs the reproducible runtime lock before copying source code so
# dependency layers remain cached while application modules change. The final
# package install uses --no-deps because requirements.lock already resolved and
# installed the complete dependency graph for this Linux environment.
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

# Installing the project wheel verifies that both app/ and mcp_server/ are part
# of the distributable package; relying only on the working directory would hide
# packaging errors until deployment.
RUN python -m pip install --no-cache-dir --no-deps .

EXPOSE 8000

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
