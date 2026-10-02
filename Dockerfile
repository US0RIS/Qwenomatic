FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin qwenomatic

COPY pyproject.toml README.md /app/
COPY supervisor /app/supervisor
COPY runtime /app/runtime
COPY storage /app/storage
COPY dashboard /app/dashboard
COPY deploy /app/deploy
RUN pip install --no-cache-dir .

RUN mkdir -p /data && chown -R qwenomatic:qwenomatic /data /home/qwenomatic
USER qwenomatic

ENTRYPOINT ["qwenomatic"]
CMD ["run"]
