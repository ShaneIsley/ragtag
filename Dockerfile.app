# The answer service. Installs only the app's dependencies (no ingest group).
# Runs the same way locally and on AWS Lambda, where the Lambda Web Adapter
# extension passes requests to uvicorn; outside Lambda the adapter does nothing.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-install-project

FROM python:3.12-slim
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0 /lambda-adapter /opt/extensions/lambda-adapter
RUN useradd --create-home --uid 10001 app
WORKDIR /srv
COPY --from=build /srv/.venv /srv/.venv
COPY app/ app/
COPY prompts/ prompts/
# COPY keeps the host's file modes; make the code readable whatever they were.
RUN chmod -R a+rX app prompts
# The release this image is: the commit it was built from, which /health reports.
ARG APP_VERSION=""
ENV PATH="/srv/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    AWS_LWA_PORT=8000 AWS_LWA_READINESS_CHECK_PATH=/health APP_VERSION=$APP_VERSION
USER app
EXPOSE 8000
# On Lambda the adapter forwards requests from 127.0.0.1; trust X-Forwarded-*
# headers from there only (uvicorn's default), so URLs use https. No request log:
# it was the only place a visitor's IP address could be recorded, and each request's
# trace already holds its route, status and duration (decisions, "Request log").
CMD ["uvicorn", "app.main:server", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]
