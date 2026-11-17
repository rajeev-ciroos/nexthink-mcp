FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home app
USER app
EXPOSE 8000
CMD ["nexthink-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
