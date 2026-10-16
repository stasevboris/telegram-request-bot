FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 botuser \
    && mkdir -p /app/data /app/credentials \
    && chown -R botuser:botuser /app

COPY --chown=botuser:botuser *.py ./
COPY --chown=botuser:botuser tests ./tests
USER botuser

CMD ["python", "app.py"]
