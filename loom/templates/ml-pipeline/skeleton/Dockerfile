FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir .
COPY configs/ configs/
# Mount the data and collect the outputs:
#   docker run -v "$PWD/data:/app/data" -v "$PWD/models:/app/models" -v "$PWD/reports:/app/reports" pipeline
ENTRYPOINT ["pipeline"]
CMD ["train", "--config", "configs/config.yaml"]
