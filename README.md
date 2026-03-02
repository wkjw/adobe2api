# adobe2api

Adobe Firefly/OpenAI-compatible gateway service.

Current design:

- External unified entry: `/v1/chat/completions` (image + video)
- Optional image-only endpoint: `/v1/images/generations`
- Token pool management (manual token + auto-refresh token)
- Admin web UI: token/config/logs/refresh profile import

## 1) Run

Install dependencies:

```bash
pip install -r requirements.txt
```

Start service (run in `adobe2api/`):

```bash
uvicorn app:app --host 0.0.0.0 --port 6001 --reload
```

Open admin UI:

- `http://127.0.0.1:6001/`
- Default admin login: `admin / admin`
- You can change credentials in `系统配置` after login, or via `config/config.json` (`admin_username`, `admin_password`)

### Docker deployment

This project supports Docker and Docker Compose.

Build + run (Docker):

```bash
docker build -t adobe2api .
docker run -d --name adobe2api \
  -p 6001:6001 \
  -e TZ=Asia/Shanghai \
  -e PORT=6001 \
  -e ADOBE_API_KEY=clio-playground-web \
  -v ./data:/app/data \
  -v ./config:/app/config \
  adobe2api
```

Run with Compose:

```bash
docker compose up -d --build
```

Compose file: `docker-compose.yml`

## 2) Auth to this service

Service API key is configured in `config/config.json` (`api_key`).

- If set, call with either:
  - `Authorization: Bearer <api_key>`
  - `X-API-Key: <api_key>`

Admin UI and admin APIs require login session cookie via `/api/v1/auth/login`.

## 3) External API usage

### 3.0 Supported model families

Current supported model families are:

- `firefly-nano-banana-*` (image, maps to upstream `nano-banana-2`)
- `firefly-nano-banana2-*` (image, maps to upstream `nano-banana-3`)
- `firefly-nano-banana-pro-*` (image)
- `firefly-sora2-*` (video)
- `firefly-sora2-pro-*` (video)
- `firefly-veo31-*` (video)
- `firefly-veo31-ref-*` (video, reference-image mode)
- `firefly-veo31-fast-*` (video)

Nano Banana image models (`nano-banana-2`):

- Pattern: `firefly-nano-banana-{resolution}-{ratio}`
- Resolution: `1k` / `2k` / `4k`
- Ratio suffix: `1x1` / `16x9` / `9x16` / `4x3` / `3x4`
- Examples:
  - `firefly-nano-banana-2k-16x9`
  - `firefly-nano-banana-4k-1x1`

Nano Banana 2 image models (`nano-banana-3`):

- Pattern: `firefly-nano-banana2-{resolution}-{ratio}`
- Resolution: `1k` / `2k` / `4k`
- Ratio suffix: `1x1` / `16x9` / `9x16` / `4x3` / `3x4`
- Examples:
  - `firefly-nano-banana2-2k-16x9`
  - `firefly-nano-banana2-4k-1x1`

Nano Banana Pro image models (legacy-compatible):

- Pattern: `firefly-nano-banana-pro-{resolution}-{ratio}`
- Resolution: `1k` / `2k` / `4k`
- Ratio suffix: `1x1` / `16x9` / `9x16` / `4x3` / `3x4`
- Examples:
  - `firefly-nano-banana-pro-2k-16x9`
  - `firefly-nano-banana-pro-4k-1x1`

Sora2 video models:

- Pattern: `firefly-sora2-{duration}-{ratio}`
- Duration: `4s` / `8s` / `12s`
- Ratio: `9x16` / `16x9`
- Examples:
  - `firefly-sora2-4s-16x9`
  - `firefly-sora2-8s-9x16`

Sora2 Pro video models:

- Pattern: `firefly-sora2-pro-{duration}-{ratio}`
- Duration: `4s` / `8s` / `12s`
- Ratio: `9x16` / `16x9`
- Examples:
  - `firefly-sora2-pro-4s-16x9`
  - `firefly-sora2-pro-8s-9x16`

Veo31 video models:

- Pattern: `firefly-veo31-{duration}-{ratio}-{resolution}`
- Duration: `4s` / `6s` / `8s`
- Ratio: `16x9` / `9x16`
- Resolution: `1080p` / `720p`
- Supports up to 2 reference images:
  - 1 image: first-frame reference
  - 2 images: first-frame + last-frame reference
- Audio defaults to enabled
- Examples:
  - `firefly-veo31-4s-16x9-1080p`
  - `firefly-veo31-6s-9x16-720p`

Veo31 Ref video models (reference-image mode):

- Pattern: `firefly-veo31-ref-{duration}-{ratio}-{resolution}`
- Duration: `4s` / `6s` / `8s`
- Ratio: `16x9` / `9x16`
- Resolution: `1080p` / `720p`
- Always uses reference image mode (not first/last frame mode)
- Supports up to 3 reference images (mapped to upstream `referenceBlobs[].usage="asset"`)
- Examples:
  - `firefly-veo31-ref-4s-9x16-720p`
  - `firefly-veo31-ref-6s-16x9-1080p`
  - `firefly-veo31-ref-8s-9x16-1080p`

Veo31 Fast video models:

- Pattern: `firefly-veo31-fast-{duration}-{ratio}-{resolution}`
- Duration: `4s` / `6s` / `8s`
- Ratio: `16x9` / `9x16`
- Resolution: `1080p` / `720p`
- Supports up to 2 reference images:
  - 1 image: first-frame reference
  - 2 images: first-frame + last-frame reference
- Audio defaults to enabled
- Examples:
  - `firefly-veo31-fast-4s-16x9-1080p`
  - `firefly-veo31-fast-6s-9x16-720p`

### 3.1 List models

```bash
curl -X GET "http://127.0.0.1:6001/v1/models" \
  -H "Authorization: Bearer <service_api_key>"
```

### 3.2 Unified endpoint: `/v1/chat/completions`

Text-to-image:

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-nano-banana-pro-2k-16x9",
    "messages": [{"role":"user","content":"a cinematic mountain sunrise"}]
  }'
```

Image-to-image (pass image in latest user message):

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-nano-banana-pro-2k-16x9",
    "messages": [{
      "role":"user",
      "content":[
        {"type":"text","text":"turn this photo into watercolor style"},
        {"type":"image_url","image_url":{"url":"https://example.com/input.jpg"}}
      ]
    }]
  }'
```

Text-to-video:

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-sora2-4s-16x9",
    "messages": [{"role":"user","content":"a drone shot over snowy forest"}]
  }'
```

Veo31 single-image semantics:

- `firefly-veo31-*` / `firefly-veo31-fast-*`: frame mode
  - 1 image => first frame
  - 2 images => first frame + last frame
- `firefly-veo31-ref-*`: reference-image mode
  - 1~3 images => reference images

Image-to-video:

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-sora2-8s-9x16",
    "messages": [{
      "role":"user",
      "content":[
        {"type":"text","text":"animate this character walking forward"},
        {"type":"image_url","image_url":{"url":"https://example.com/character.png"}}
      ]
    }]
  }'
```

### 3.3 Image endpoint: `/v1/images/generations`

```bash
curl -X POST "http://127.0.0.1:6001/v1/images/generations" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-nano-banana-pro-4k-16x9",
    "prompt": "futuristic city skyline at dusk"
  }'
```

## 4) Admin APIs

- `GET /api/v1/tokens`
- `POST /api/v1/tokens`
- `DELETE /api/v1/tokens/{id}`
- `PUT /api/v1/tokens/{id}/status?status=active|disabled`
- `POST /api/v1/tokens/{id}/refresh`
- `GET /api/v1/config`
- `PUT /api/v1/config`
- `GET /api/v1/logs?limit=200`
- `DELETE /api/v1/logs`
- `GET /api/v1/refresh-profiles`
- `POST /api/v1/refresh-profiles/import`
- `POST /api/v1/refresh-profiles/import-batch`
- `POST /api/v1/refresh-profiles/{id}/refresh-now`
- `PUT /api/v1/refresh-profiles/{id}/enabled`
- `DELETE /api/v1/refresh-profiles/{id}`

## 5) Refresh-bundle plugin usage

Project root includes standalone plugin:

- `at-refresh-capture-extension/`

Purpose:

- capture minimal data required by Adobe check-token endpoint
- export `adobe_refresh_bundle` JSON (sensitive)

Load plugin in Chrome:

1. Open `chrome://extensions`
2. Enable Developer mode
3. Click `Load unpacked`
4. Select `at-refresh-capture-extension`

Capture and import flow:

1. Open `https://firefly.adobe.com/` and login
2. Click plugin popup -> `Export Refresh Bundle`
3. Open admin UI `Token 管理` tab
4. Click `导入 Refresh Bundle`
5. Paste JSON or upload file
6. Click `导入` (service auto-runs one refresh immediately)
7. Token list will show one `自动刷新=是` token per refresh profile

Batch import notes:

- Upload multiple `.json` files at once in the admin dialog, then click `导入`
- Or paste JSON array:
  - `[{"name":"account-a","bundle":{...}}, {"name":"account-b","bundle":{...}}]`

## 6) Storage paths

- Generated media: `data/generated/`
- Request logs: `data/request_logs.jsonl`
- Token pool: `config/tokens.json`
- Service config: `config/config.json`
- Refresh profile (local private): `config/refresh_profile.json`

Generated media retention policy:

- Files under `data/generated/` are preserved and served via `/generated/*`
- Auto-prune is enabled by size threshold (oldest files first)
  - `generated_max_size_mb` (default `1024`)
  - `generated_prune_size_mb` (default `200`)
- When total generated file size exceeds `generated_max_size_mb`, service deletes old files until at least `generated_prune_size_mb` is reclaimed and total size falls back under threshold

## 7) Security notes

- Refresh bundle contains high-sensitivity cookie/session data.
- Do not commit/share refresh bundle files.
- Rotate Adobe session if sensitive data was exposed.
