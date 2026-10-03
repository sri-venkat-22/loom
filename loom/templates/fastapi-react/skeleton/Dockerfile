# Build the frontend
FROM node:22-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm install
COPY web/ ./
RUN npm run build

# Run the API, which serves the built frontend too
FROM python:3.11-slim
WORKDIR /app
COPY api/requirements.txt api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt
COPY api/ api/
COPY --from=web /web/dist web/dist
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --app-dir api --host 0.0.0.0 --port ${PORT}"]
