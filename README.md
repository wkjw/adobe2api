# adobe2api

A small Adobe Firefly image generation service with:

- FastAPI backend
- Async job-style generation endpoint
- Browser UI dashboard for prompt input, progress polling, and image preview

## 1) Install

```bash
pip install -r requirements.txt
```

## 2) Configure

Set environment variables:

```bash
set ADOBE_ACCESS_TOKEN=your_ims_access_token
set ADOBE_API_KEY=clio-playground-web
```

## 3) Run

```bash
uvicorn app:app --host 0.0.0.0 --port 6001 --reload
```

Run in this folder (`D:\my_project\adobe2api`).

## API

- `GET /v1/models`
  - returns OpenAI-compatible model list
  - current models:
    - `firefly-nano-banana-pro-1k`
    - `firefly-nano-banana-pro-2k`

- `POST /v1/images/generations`
  - OpenAI-compatible image generation endpoint
  - body example: `{ "prompt": "...", "model": "firefly-nano-banana-pro-2k" }`
  - `model` controls resolution by suffix (`-1k` / `-2k`)

- `POST /api/v1/generate`
  - body: `{ "prompt": "...", "aspect_ratio": "16:9", "model": "firefly-nano-banana-pro-2k" }`
  - returns: `{ "task_id": "...", "status": "queued" }`

- `GET /api/v1/generate/{task_id}`
  - returns task status and image URL when complete

- `GET /api/v1/health`

- `POST /api/v1/refresh-profile/import`
  - import refresh bundle from `at-refresh-capture-extension`

- `GET /api/v1/refresh-profile/status`
  - read auto-refresh status

- `POST /api/v1/refresh-profile/refresh-now`
  - trigger one immediate refresh attempt

- `DELETE /api/v1/refresh-profile`
  - clear imported refresh bundle

## UI

Open:

- `http://127.0.0.1:6001/`

Default base port is `6001`.

Generated files are stored in:

- `data/generated/`

Config files are stored in:

- `config/config.json`
- `config/tokens.json`
- `config/refresh_profile.json`
