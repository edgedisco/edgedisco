FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN useradd --system --uid 10001 inventory && mkdir /data && chown inventory:inventory /data
USER inventory
EXPOSE 8080
CMD ["ai-inventory", "server", "--host", "0.0.0.0", "--port", "8080", "--db", "/data/inventory.db"]
