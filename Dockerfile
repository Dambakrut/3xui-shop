FROM python:3.12-slim-bullseye

WORKDIR /
ENV PYTHONPATH=/

COPY pyproject.toml poetry.lock README.md /
RUN pip install poetry==2.1.4 && poetry install --no-root --only main --no-interaction

COPY ./app /app
