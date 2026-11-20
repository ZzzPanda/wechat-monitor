FROM docker.io/library/python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
WORKDIR /app
RUN python -c "import sqlite3; sqlite3.connect(':memory:').close()"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "/app/server.py"]
