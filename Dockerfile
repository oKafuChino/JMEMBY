FROM python:3.12-slim
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid ${APP_GID} app && useradd --uid ${APP_UID} --gid app --create-home app
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
ENTRYPOINT ["python", "-m", "jmemby.main"]
CMD ["run"]
