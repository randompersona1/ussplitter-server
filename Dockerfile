FROM nvidia/cuda:12.1.0-base-ubuntu22.04 AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app
ENV TORCH_HOME=/app/models
ENV VIRTUAL_ENV=/app/.venv

# We set the timezone because ffmpegs dependancy `tzdata` will otherwise prompt us to set it interactively
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install python dependancies from uv
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --frozen --no-install-project --link-mode=copy --no-dev --group torch


FROM nvidia/cuda:12.1.0-base-ubuntu22.04
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/


RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*


COPY --from=builder /app /app
COPY --from=builder /root/.local/share/uv /root/.local/share/uv

WORKDIR /app

# Use UID and GID 1000
RUN useradd -m -u 1000 -s /bin/bash app
RUN chown -R app:app /app
USER app

COPY pyproject.toml uv.lock .python-version README.md LICENSE src ./

RUN uv sync --frozen --no-dev --group torch

EXPOSE 5000

# Run the application
CMD ["./.venv/bin/waitress-serve", "--listen", "0.0.0.0:5000", "--threads", "4", "ussplitter_server.server:app"]