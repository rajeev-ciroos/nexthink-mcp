# Runs under OpenShift's restricted SCC: OpenShift starts the container with a random UID in group 0 and
# ignores USER, so everything the app reads is group-0 readable and it writes nothing outside /tmp.
# BASE_IMAGE can be swapped for a Red Hat image, e.g. registry.access.redhat.com/ubi9/python-312
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}
USER 0
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && chgrp -R 0 /app && chmod -R g=u /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
USER 1001
EXPOSE 8000
CMD ["nexthink-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
