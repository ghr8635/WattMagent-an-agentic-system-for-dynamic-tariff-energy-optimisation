# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir "."

FROM python:3.12-slim
# Pull in Debian security fixes published since the base image was built (the image scan fails
# on fixed HIGH/CRITICAL findings). Not byte-for-byte reproducible; pinning the base image by
# digest and rebuilding on a schedule is on the production checklist.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 app
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
WORKDIR /home/app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["uvicorn", "--factory", "wattmagent.api.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
