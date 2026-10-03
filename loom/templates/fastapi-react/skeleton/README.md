# app

A FastAPI backend (`api/`) with a React + Vite + TypeScript frontend (`web/`).

## Develop

```
pip install -r api/requirements.txt
npm --prefix web install

uvicorn app.main:app --app-dir api --reload --port 8000
npm --prefix web run dev
```

Open the address Vite prints; it proxies `/api` to the backend.

## Test

```
python -m pytest -q api
npm --prefix web test -- --run
```

## Run in a container

```
docker build -t app .
docker run -p 8000:8000 app
```
