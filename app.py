import os
import json
import logging
import threading
import time
import uuid
import base64
import binascii
import io
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List, Any
from urllib.parse import unquote_to_bytes, urlparse

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from curl_cffi.requests import Session as CurlSession
except Exception:
    CurlSession = None

try:
    from PIL import Image
except Exception:
    Image = None

from core.token_mgr import token_manager
from core.config_mgr import config_manager
from core.refresh_mgr import refresh_manager


logger = logging.getLogger("adobe2api")


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
GENERATED_DIR = DATA_DIR / "generated"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)


SUPPORTED_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4"}
RATIO_SUFFIX_MAP = {
    "1:1": "1x1",
    "16:9": "16x9",
    "9:16": "9x16",
    "4:3": "4x3",
    "3:4": "3x4",
}

# OpenAI-compatible exposed model list.
MODEL_CATALOG = {}

for _res in ("1k", "2k"):
    for _ratio, _suffix in RATIO_SUFFIX_MAP.items():
        _id = f"firefly-nano-banana-pro-{_res}-{_suffix}"
        MODEL_CATALOG[_id] = {
            "upstream_model": "google:firefly:colligo:nano-banana-pro",
            "output_resolution": _res.upper(),
            "aspect_ratio": _ratio,
            "description": f"Firefly Nano Banana Pro ({_res.upper()} {_ratio})",
        }
DEFAULT_MODEL_ID = "firefly-nano-banana-pro-2k-16x9"
VIDEO_MODEL_CATALOG = {
    "firefly-sora2-4s-9x16": {"duration": 4, "aspect_ratio": "9:16", "description": "Firefly Sora2 video model (4s 9:16)"},
    "firefly-sora2-4s-16x9": {"duration": 4, "aspect_ratio": "16:9", "description": "Firefly Sora2 video model (4s 16:9)"},
    "firefly-sora2-8s-9x16": {"duration": 8, "aspect_ratio": "9:16", "description": "Firefly Sora2 video model (8s 9:16)"},
    "firefly-sora2-8s-16x9": {"duration": 8, "aspect_ratio": "16:9", "description": "Firefly Sora2 video model (8s 16:9)"},
    "firefly-sora2-12s-9x16": {"duration": 12, "aspect_ratio": "9:16", "description": "Firefly Sora2 video model (12s 9:16)"},
    "firefly-sora2-12s-16x9": {"duration": 12, "aspect_ratio": "16:9", "description": "Firefly Sora2 video model (12s 16:9)"},
}


class AdobeRequestError(Exception):
    pass

class QuotaExhaustedError(AdobeRequestError):
    pass

class AuthError(AdobeRequestError):
    pass


class UpstreamTemporaryError(AdobeRequestError):
    pass


class AdobeClient:
    submit_url = "https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async"
    video_submit_url = "https://firefly-3p.ff.adobe.io/v2/3p-videos/generate-async"
    upload_url = "https://firefly-3p.ff.adobe.io/v2/storage/image"

    def __init__(self) -> None:
        self.api_key = "clio-playground-web"
        self.impersonate = "chrome124"
        self.proxy = ""
        self.generate_timeout = 300
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        self.sec_ch_ua = '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'

        self.apply_config(config_manager.get_all())

        # Environment variables can override file config.
        env_api_key = os.getenv("ADOBE_API_KEY")
        env_impersonate = os.getenv("ADOBE_IMPERSONATE")
        env_proxy = os.getenv("ADOBE_PROXY")
        env_user_agent = os.getenv("ADOBE_USER_AGENT")
        env_sec_ch_ua = os.getenv("ADOBE_SEC_CH_UA")
        env_generate_timeout = os.getenv("ADOBE_GENERATE_TIMEOUT")

        if env_api_key:
            self.api_key = env_api_key.strip() or self.api_key
        if env_impersonate:
            self.impersonate = env_impersonate.strip() or self.impersonate
        if env_proxy is not None:
            self.proxy = env_proxy.strip()
        if env_user_agent:
            self.user_agent = env_user_agent.strip() or self.user_agent
        if env_sec_ch_ua:
            self.sec_ch_ua = env_sec_ch_ua.strip() or self.sec_ch_ua
        if env_generate_timeout:
            try:
                self.generate_timeout = int(env_generate_timeout)
                if self.generate_timeout <= 0:
                    self.generate_timeout = 300
            except Exception:
                pass

    def apply_config(self, cfg: dict) -> None:
        proxy = str(cfg.get("proxy", "")).strip()
        use_proxy = bool(cfg.get("use_proxy", False))
        timeout_val = cfg.get("generate_timeout", 300)
        try:
            timeout_val = int(timeout_val)
        except Exception:
            timeout_val = 300
        self.generate_timeout = timeout_val if timeout_val > 0 else 300
        self.proxy = proxy if use_proxy and proxy else ""
        if self.proxy:
            logger.warning("proxy enabled for upstream requests: %s", self.proxy)
        else:
            logger.warning("proxy disabled for upstream requests")

    def _requests_proxies(self) -> Optional[dict]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def _session(self):
        if CurlSession is None:
            return None
        kwargs = {"impersonate": self.impersonate, "timeout": 20}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        return CurlSession(**kwargs)

    def _browser_headers(self) -> dict:
        return {
            "user-agent": self.user_agent,
            "origin": "https://firefly.adobe.com",
            "referer": "https://firefly.adobe.com/",
            "accept-language": "en-US,en;q=0.9",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-site": "same-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }

    def _submit_headers(self, token: str) -> dict:
        # Based on captured generate-async request.
        headers = self._browser_headers()
        headers.update(
            {
                "Authorization": f"Bearer {token}",
                "x-api-key": self.api_key,
                "content-type": "application/json",
                "accept": "*/*",
            }
        )
        return headers

    def _submit_headers_minimal(self, token: str) -> dict:
        # Strictly match custom headers seen in captured request.
        return {
            "Authorization": f"Bearer {token}",
            "x-api-key": self.api_key,
            "content-type": "application/json",
            "accept": "*/*",
        }

    def _poll_headers(self, token: str) -> dict:
        # Captured poll requests mostly send Authorization only.
        return {
            "Authorization": f"Bearer {token}",
            "accept": "*/*",
            "referer": "https://firefly.adobe.com/",
            "origin": "https://firefly.adobe.com",
            "user-agent": self.user_agent,
        }

    def _post_json(self, url: str, headers: dict, payload: dict):
        session = self._session()
        if session is None:
            return requests.post(url, headers=headers, json=payload, timeout=20, proxies=self._requests_proxies())
        with session:
            resp = session.post(url, headers=headers, json=payload)
        # Some environments return intermittent 451 via curl_cffi path.
        # Retry once with plain requests for better stability.
        if resp.status_code == 451:
            return requests.post(url, headers=headers, json=payload, timeout=20, proxies=self._requests_proxies())
        return resp

    def _post_bytes(self, url: str, headers: dict, payload: bytes):
        session = self._session()
        if session is None:
            return requests.post(url, headers=headers, data=payload, timeout=30, proxies=self._requests_proxies())
        with session:
            resp = session.post(url, headers=headers, data=payload)
        return resp

    def _get(self, url: str, headers: dict, timeout: int = 20):
        session = self._session()
        if session is None:
            return requests.get(url, headers=headers, timeout=timeout, proxies=self._requests_proxies())
        with session:
            resp = session.get(url, headers=headers)
        return resp

    @staticmethod
    def _size_from_ratio(ratio: str, output_resolution: str = "2K") -> dict:
        level = (output_resolution or "2K").upper()
        if level == "1K":
            ratio_map = {
                "1:1": {"width": 1024, "height": 1024},
                "16:9": {"width": 1360, "height": 768},
                "9:16": {"width": 768, "height": 1360},
                "4:3": {"width": 1152, "height": 864},
                "3:4": {"width": 864, "height": 1152},
            }
        else:
            ratio_map = {
                "1:1": {"width": 2048, "height": 2048},
                "16:9": {"width": 2752, "height": 1536},
                "9:16": {"width": 1536, "height": 2752},
                "4:3": {"width": 2048, "height": 1536},
                "3:4": {"width": 1536, "height": 2048},
            }
        return ratio_map.get(ratio, ratio_map["16:9"])

    def upload_image(self, token: str, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
        if not image_bytes:
            raise AdobeRequestError("image is empty")

        headers = {
            "authorization": f"Bearer {token}",
            "x-api-key": self.api_key,
            "content-type": mime_type,
            "accept": "application/json",
        }
        resp = self._post_bytes(self.upload_url, headers=headers, payload=image_bytes)

        if resp.status_code in (401, 403):
            raise AuthError("Token invalid or expired")
        if resp.status_code != 200:
            raise AdobeRequestError(f"upload image failed: {resp.status_code} {resp.text[:300]}")

        try:
            data = resp.json()
        except Exception:
            raise AdobeRequestError("upload image failed: invalid response")

        image_id = (((data.get("images") or [{}])[0]) or {}).get("id")
        if not image_id:
            raise AdobeRequestError("upload image succeeded but no image id returned")
        return str(image_id)

    def _build_payload_candidates(
        self,
        prompt: str,
        aspect_ratio: str,
        output_resolution: str,
        source_image_ids: Optional[list[str]] = None,
    ) -> list[dict]:
        base_payload = {
            "modelId": "gemini-flash",
            "modelVersion": "nano-banana-2",
            "n": 1,
            "prompt": prompt,
            "size": self._size_from_ratio(aspect_ratio, output_resolution),
            "seeds": [int(time.time()) % 999999],
            "groundSearch": False,
            "skipCai": False,
            "output": {"storeInputs": True},
            "generationMetadata": {"module": "text2image"},
            "modelSpecificPayload": {
                "aspectRatio": aspect_ratio,
                "parameters": {"addWatermark": False},
            },
        }

        if not source_image_ids:
            base_payload["referenceBlobs"] = []
            return [base_payload]

        candidates: list[dict] = []
        edited = dict(base_payload)
        edited["generationMetadata"] = {"module": "image2image"}

        c1 = dict(edited)
        c1["referenceBlobs"] = [{"id": img_id, "usage": "general"} for img_id in source_image_ids]
        candidates.append(c1)

        c4 = dict(edited)
        c4["referenceBlobs"] = []
        c4["imagePrompt"] = {"referenceImage": source_image_ids[0]}
        candidates.append(c4)

        c5 = dict(edited)
        c5["referenceBlobs"] = []
        c5["imagePrompt"] = {"referenceImage": {"id": source_image_ids[0]}}
        candidates.append(c5)

        return candidates

    @staticmethod
    def _video_size(aspect_ratio: str) -> dict:
        if aspect_ratio == "16:9":
            return {"width": 1280, "height": 720}
        return {"width": 720, "height": 1280}

    @staticmethod
    def _normalize_video_poll_url(raw_url: str) -> str:
        if not raw_url:
            return raw_url
        try:
            parsed = urlparse(raw_url)
            host = parsed.netloc
            path_parts = [p for p in parsed.path.split("/") if p]
            if not host or not path_parts:
                return raw_url
            if not host.startswith("firefly-epo"):
                return raw_url
            job_id = path_parts[-1]
            if not job_id:
                return raw_url
            return f"https://bks-epo8522.adobe.io/v2/jobs/result/{job_id}?host={host}/"
        except Exception:
            return raw_url

    @staticmethod
    def _build_video_prompt_json(prompt: str, duration: int, negative_prompt: str = "") -> str:
        payload = {
            "id": 1,
            "duration_sec": int(duration),
            "prompt_text": prompt,
        }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        return json.dumps(payload, ensure_ascii=False)

    def _build_video_payload(
        self,
        prompt: str,
        aspect_ratio: str,
        duration: int,
        source_image_ids: Optional[list[str]] = None,
        negative_prompt: str = "",
        generate_audio: bool = True,
    ) -> dict:
        seed_val = int(time.time()) % 999999
        payload = {
            "n": 1,
            "seeds": [seed_val],
            "modelId": "sora",
            "modelVersion": "sora-2",
            "size": self._video_size(aspect_ratio),
            "duration": int(duration),
            "fps": 24,
            "prompt": self._build_video_prompt_json(prompt=prompt, duration=duration, negative_prompt=negative_prompt),
            "generationMetadata": {"module": "text2video"},
            "model": "openai:firefly:colligo:sora2",
            "generateAudio": bool(generate_audio),
            "generateLoop": False,
            "transparentBackground": False,
            "seed": str(seed_val),
            "locale": "en-US",
            "camera": {
                "angle": "none",
                "shotSize": "none",
                "motion": None,
                "promptStyle": None,
            },
            "negativePrompt": negative_prompt or "",
            "jobMode": "standard",
            "debugGenerationEndpoint": "",
            "referenceBlobs": [],
            "referenceFrames": [],
            "referenceImages": [],
            "referenceVideo": None,
            "cameraMotionReferenceVideo": None,
            "characterReference": None,
            "editReferenceVideo": None,
            "output": {"storeInputs": True},
        }
        if source_image_ids:
            first_id = str(source_image_ids[0])
            payload["referenceBlobs"] = [{"id": first_id, "usage": "general", "promptReference": 1}]
            payload["referenceFrames"] = [{"localBlobRef": first_id}, None]
        return payload

    def generate_video(
        self,
        token: str,
        prompt: str,
        aspect_ratio: str = "9:16",
        duration: int = 12,
        source_image_ids: Optional[list[str]] = None,
        timeout: int = 600,
        negative_prompt: str = "",
        generate_audio: bool = True,
    ) -> tuple[bytes, dict]:
        payload = self._build_video_payload(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            duration=duration,
            source_image_ids=source_image_ids,
            negative_prompt=negative_prompt,
            generate_audio=generate_audio,
        )
        submit_resp = self._post_json(self.video_submit_url, headers=self._submit_headers(token), payload=payload)

        if submit_resp.status_code in (401, 403):
            access_error = submit_resp.headers.get("x-access-error")
            if access_error == "taste_exhausted":
                raise QuotaExhaustedError("Adobe quota exhausted for this account")
            raise AuthError("Token invalid or expired")

        if submit_resp.status_code != 200:
            if submit_resp.status_code in (429, 451) or submit_resp.status_code >= 500:
                raise UpstreamTemporaryError(f"video submit failed: {submit_resp.status_code} {submit_resp.text[:300]}")
            raise AdobeRequestError(f"video submit failed: {submit_resp.status_code} {submit_resp.text[:300]}")

        submit_data = submit_resp.json()
        poll_url = submit_resp.headers.get("x-override-status-link") or ((submit_data.get("links") or {}).get("result") or {}).get("href")
        if not poll_url:
            raise AdobeRequestError("video submit succeeded but no poll url returned")
        poll_url = self._normalize_video_poll_url(str(poll_url))

        start = time.time()
        while True:
            poll_resp = self._get(poll_url, headers=self._poll_headers(token), timeout=20)
            if poll_resp.status_code in (401, 403):
                raise AuthError("Token invalid or expired")
            if poll_resp.status_code != 200:
                if poll_resp.status_code in (429, 451) or poll_resp.status_code >= 500:
                    raise UpstreamTemporaryError(f"video poll failed: {poll_resp.status_code} {poll_resp.text[:300]}")
                raise AdobeRequestError(f"video poll failed: {poll_resp.status_code} {poll_resp.text[:300]}")

            latest = poll_resp.json()
            outputs = latest.get("outputs") or []
            if outputs:
                video_url = (((outputs[0] or {}).get("video") or {}).get("presignedUrl"))
                if not video_url:
                    raise AdobeRequestError("video job finished without video url")
                video_resp = self._get(video_url, headers={"accept": "*/*"}, timeout=60)
                video_resp.raise_for_status()
                return video_resp.content, latest

            status_val = str(latest.get("status") or "").upper()
            if status_val in {"FAILED", "CANCELLED", "ERROR"}:
                raise AdobeRequestError(f"video job failed: {latest}")

            if time.time() - start > timeout:
                raise AdobeRequestError("video generation timed out")
            time.sleep(3.0)

    def generate(
        self,
        token: str,
        prompt: str,
        aspect_ratio: str = "16:9",
        output_resolution: str = "2K",
        source_image_ids: Optional[list[str]] = None,
        timeout: int = 180,
    ) -> tuple[bytes, dict]:
        submit_resp = None
        last_error = ""
        for payload in self._build_payload_candidates(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            output_resolution=output_resolution,
            source_image_ids=source_image_ids,
        ):
            # 禁用重试等，最大程度节省资源
            submit_resp = self._post_json(self.submit_url, headers=self._submit_headers(token), payload=payload)
            if submit_resp.status_code == 200:
                break

            if submit_resp.status_code in (401, 403):
                break

            last_error = submit_resp.text[:300]

        if submit_resp is None:
            raise AdobeRequestError("submit failed: no response")

        if submit_resp.status_code in (401, 403):
            access_error = submit_resp.headers.get("x-access-error")
            logger.warning(
                "submit auth failed status=%s access_error=%s body=%s",
                submit_resp.status_code,
                access_error,
                submit_resp.text[:300],
            )
            if access_error == "taste_exhausted":
                raise QuotaExhaustedError("Adobe quota exhausted for this account")
            raise AuthError("Token invalid or expired")

        if submit_resp.status_code != 200:
            logger.error(
                "submit failed status=%s body=%s",
                submit_resp.status_code,
                submit_resp.text[:500],
            )
            if submit_resp.status_code in (429, 451) or submit_resp.status_code >= 500:
                raise UpstreamTemporaryError(f"submit failed: {submit_resp.status_code} {submit_resp.text[:300]}")
            if last_error:
                raise AdobeRequestError(f"submit failed: {submit_resp.status_code} {last_error}")
            raise AdobeRequestError(f"submit failed: {submit_resp.status_code} {submit_resp.text[:300]}")

        submit_data = submit_resp.json()
        poll_url = submit_resp.headers.get("x-override-status-link") or ((submit_data.get("links") or {}).get("result") or {}).get("href")
        if not poll_url:
            raise AdobeRequestError("submit succeeded but no poll url returned")

        start = time.time()
        latest = {}
        # 延长轮询间隔，减少请求次数
        sleep_time = 3.0
        while True:
            poll_resp = self._get(poll_url, headers=self._poll_headers(token), timeout=20)
            if poll_resp.status_code != 200:
                logger.error(
                    "poll failed status=%s body=%s",
                    poll_resp.status_code,
                    poll_resp.text[:500],
                )
                raise AdobeRequestError(f"poll failed: {poll_resp.status_code} {poll_resp.text[:300]}")
            
            latest = poll_resp.json()
            outputs = latest.get("outputs") or []
            if outputs:
                image_url = (((outputs[0] or {}).get("image") or {}).get("presignedUrl"))
                if not image_url:
                    raise AdobeRequestError("job finished without image url")
                img_resp = self._get(image_url, headers={"accept": "*/*"}, timeout=30)
                img_resp.raise_for_status()
                return img_resp.content, latest

            if time.time() - start > timeout:
                raise AdobeRequestError("generation timed out")
            time.sleep(sleep_time)


@dataclass
class JobRecord:
    id: str
    prompt: str
    aspect_ratio: str
    status: str = "queued"
    progress: float = 0.0
    image_url: Optional[str] = None
    error: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0


class JobStore:
    def __init__(self, max_items: int = 200) -> None:
        self._items: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._max_items = max_items

    def _cleanup(self):
        # 限制内存占用
        if len(self._items) > self._max_items:
            sorted_items = sorted(self._items.values(), key=lambda x: x.created_at)
            for item in sorted_items[:50]:
                self._items.pop(item.id, None)

    def create(self, prompt: str, aspect_ratio: str) -> JobRecord:
        now = time.time()
        item = JobRecord(
            id=uuid.uuid4().hex,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._cleanup()
            self._items[item.id] = item
        return item

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            return self._items.get(job_id)

    def update(self, job_id: str, **kwargs) -> None:
        with self._lock:
            item = self._items.get(job_id)
            if not item:
                return
            for k, v in kwargs.items():
                setattr(item, k, v)
            item.updated_at = time.time()


@dataclass
class RequestLogRecord:
    id: str
    ts: float
    method: str
    path: str
    status_code: int
    duration_sec: int
    proxy_used: bool
    operation: str
    preview_url: Optional[str] = None
    preview_kind: Optional[str] = None
    model: Optional[str] = None
    prompt_preview: Optional[str] = None
    error: Optional[str] = None


class RequestLogStore:
    def __init__(self, file_path: Path, max_items: int = 500) -> None:
        self._file_path = file_path
        self._lock = threading.Lock()
        self._max_items = max_items
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._file_path.exists():
            self._file_path.touch()

    def _truncate_to_max_locked(self) -> None:
        with self._file_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= self._max_items:
            return
        kept = lines[-self._max_items :]
        with self._file_path.open("w", encoding="utf-8") as f:
            f.writelines(kept)

    def add(self, item: RequestLogRecord) -> None:
        payload = asdict(item)
        with self._lock:
            with self._file_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._truncate_to_max_locked()

    def list(self, limit: int = 100) -> list[dict]:
        safe_limit = min(max(int(limit or 100), 1), 500)
        with self._lock:
            with self._file_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        selected = lines[-safe_limit:]
        data: list[dict] = []
        for line in reversed(selected):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    data.append(item)
            except Exception:
                continue
        return data

    def clear(self) -> None:
        with self._lock:
            with self._file_path.open("w", encoding="utf-8") as f:
                f.write("")


# 极简配置启动
app = FastAPI(
    title="adobe2api", 
    version="0.1.0",
    docs_url=None, # 关闭 swagger，节省资源
    redoc_url=None
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/generated", StaticFiles(directory=GENERATED_DIR), name="generated_files")

store = JobStore()
log_store = RequestLogStore(DATA_DIR / "request_logs.jsonl")
client = AdobeClient()
refresh_manager.start()


def _extract_logging_fields(raw_body: bytes) -> dict[str, Optional[str]]:
    if not raw_body:
        return {"model": None, "prompt_preview": None}
    try:
        import json

        data: Any = json.loads(raw_body.decode("utf-8"))
        if not isinstance(data, dict):
            return {"model": None, "prompt_preview": None}

        model = str(data.get("model") or "").strip() or None
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            prompt = _extract_prompt_from_messages(data.get("messages") or [])
        if prompt:
            prompt = prompt.replace("\r", " ").replace("\n", " ").strip()
            prompt = prompt[:180]
        return {"model": model, "prompt_preview": prompt or None}
    except Exception:
        return {"model": None, "prompt_preview": None}


def _set_request_preview(request: Request, url: str, kind: str = "image") -> None:
    if not url:
        return
    try:
        request.state.log_preview_url = url
        request.state.log_preview_kind = kind
    except Exception:
        pass


@app.middleware("http")
async def request_logger(request: Request, call_next):
    started = time.time()
    method = request.method.upper()
    path = request.url.path
    proxy_used = False
    preview_url = None
    preview_kind = None
    raw_body = b""
    body_meta = {"model": None, "prompt_preview": None}
    error_text = None
    status_code = 500

    op_map = {
        "/v1/chat/completions": "chat.completions",
        "/v1/images/generations": "images.generations",
    }
    operation = op_map.get(path, "")
    should_log = bool(operation)

    if method in {"POST", "PUT", "PATCH"} and should_log:
        try:
            raw_body = await request.body()
            request._body = raw_body
            if path in {"/v1/images/generations", "/v1/chat/completions", "/api/v1/generate"}:
                body_meta = _extract_logging_fields(raw_body)
        except Exception:
            pass

    response = None
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as exc:
        error_text = str(exc)[:240]
        raise
    finally:
        if should_log:
            duration_sec = int(time.time() - started)
            proxy_used = bool(client.proxy)
            preview_url = getattr(request.state, "log_preview_url", None)
            preview_kind = getattr(request.state, "log_preview_kind", None)
            log_store.add(
                RequestLogRecord(
                    id=uuid.uuid4().hex[:12],
                    ts=time.time(),
                    method=method,
                    path=path,
                    status_code=status_code,
                    duration_sec=duration_sec,
                    proxy_used=proxy_used,
                    operation=operation,
                    preview_url=preview_url,
                    preview_kind=preview_kind,
                    model=body_meta.get("model"),
                    prompt_preview=body_meta.get("prompt_preview"),
                    error=error_text,
                )
            )
    return response


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1200)
    aspect_ratio: str = Field(default="16:9")
    output_resolution: str = Field(default="2K")
    model: Optional[str] = None

class TokenAddRequest(BaseModel):
    token: str


class ConfigUpdateRequest(BaseModel):
    api_key: Optional[str] = None
    proxy: Optional[str] = None
    use_proxy: Optional[bool] = None
    generate_timeout: Optional[int] = None


class RefreshBundleImportRequest(BaseModel):
    bundle: dict


def _resolve_model(model_id: Optional[str]) -> dict:
    if not model_id:
        return MODEL_CATALOG[DEFAULT_MODEL_ID]
    if model_id not in MODEL_CATALOG:
        raise HTTPException(status_code=400, detail=f"Invalid model: {model_id}")
    return MODEL_CATALOG[model_id]


def _ratio_from_size(size: str) -> str:
    mapping = {
        "1024x1024": "1:1",
        "1536x1536": "1:1",
        "2048x2048": "1:1",
        "1024x1792": "9:16",
        "1536x2752": "9:16",
        "1792x1024": "16:9",
        "2752x1536": "16:9",
        "2048x1536": "4:3",
        "1536x2048": "3:4",
    }
    return mapping.get(str(size or "").strip(), "1:1")


def _resolve_video_options(data: dict) -> tuple[bool, str]:
    generate_audio = bool(data.get("generate_audio", data.get("generateAudio", True)))
    negative_prompt = str(data.get("negative_prompt") or data.get("negativePrompt") or "").strip()
    return generate_audio, negative_prompt


def _extract_prompt_from_messages(messages) -> str:
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        chunks = []
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                chunks.append(content.strip())
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = str(part.get("text") or "").strip()
                    if txt:
                        chunks.append(txt)
        return "\n".join(chunks).strip()
    return ""


def _data_url_to_bytes(url: str) -> tuple[bytes, str]:
    raw = str(url or "").strip()
    if not raw.startswith("data:"):
        raise ValueError("not a data url")
    head, sep, body = raw.partition(",")
    if not sep:
        raise ValueError("invalid data url")

    mime_type = "image/jpeg"
    mime_part = head[5:]
    if ";" in mime_part:
        mime_type = (mime_part.split(";", 1)[0] or "image/jpeg").strip()
    elif mime_part:
        mime_type = mime_part.strip()

    if ";base64" in head:
        try:
            return base64.b64decode(body, validate=True), mime_type
        except binascii.Error:
            raise ValueError("invalid base64 image data")

    return unquote_to_bytes(body), mime_type


def _extract_image_urls_from_messages(messages, max_items: int = 6) -> list[str]:
    urls: list[str] = []
    if not isinstance(messages, list):
        return urls
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            return urls
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "image_url":
                continue
            image_url = part.get("image_url")
            if isinstance(image_url, str):
                image_url = image_url.strip()
            elif isinstance(image_url, dict):
                image_url = str(image_url.get("url") or "").strip()
            else:
                image_url = ""
            if image_url:
                urls.append(image_url)
                if len(urls) >= max_items:
                    return urls
        return urls
    return urls


def _normalize_image_mime(mime_type: str) -> str:
    allowed = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
    normalized = str(mime_type or "").lower()
    if normalized == "image/jpg":
        normalized = "image/jpeg"
    if normalized not in allowed:
        normalized = "image/jpeg"
    return normalized


def _load_input_images(messages) -> list[tuple[bytes, str]]:
    image_urls = _extract_image_urls_from_messages(messages, max_items=6)
    if not image_urls:
        return []

    loaded: list[tuple[bytes, str]] = []
    for image_url in image_urls:
        if image_url.startswith("data:"):
            try:
                image_bytes, mime_type = _data_url_to_bytes(image_url)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        else:
            if not image_url.lower().startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="Only http/https or data URL images are supported")
            resp = requests.get(image_url, timeout=30)
            if resp.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Failed to fetch image_url: {resp.status_code}")
            image_bytes = resp.content
            mime_type = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip() or "image/jpeg"

        if not image_bytes:
            raise HTTPException(status_code=400, detail="image_url is empty")
        if len(image_bytes) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="image too large, max 10MB")

        loaded.append((image_bytes, _normalize_image_mime(mime_type)))

    return loaded


def _prepare_video_source_image(image_bytes: bytes, aspect_ratio: str) -> tuple[bytes, str]:
    if not image_bytes:
        raise HTTPException(status_code=400, detail="image_url is empty")
    if Image is None:
        return image_bytes, "image/jpeg"

    target_size = (1280, 720) if aspect_ratio == "16:9" else (720, 1280)
    try:
        with Image.open(io.BytesIO(image_bytes)) as src:
            src = src.convert("RGB")
            src_ratio = src.width / max(1, src.height)
            tgt_ratio = target_size[0] / target_size[1]

            if src_ratio > tgt_ratio:
                new_h = target_size[1]
                new_w = int(new_h * src_ratio)
            else:
                new_w = target_size[0]
                new_h = int(new_w / max(src_ratio, 1e-6))

            resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = max(0, (new_w - target_size[0]) // 2)
            top = max(0, (new_h - target_size[1]) // 2)
            cropped = resized.crop((left, top, left + target_size[0], top + target_size[1]))

            out = io.BytesIO()
            cropped.save(out, format="PNG")
            return out.getvalue(), "image/png"
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid image for video: {exc}")


def _resolve_ratio_and_resolution(data: dict, model_id: Optional[str]) -> tuple[str, str, str]:
    ratio = str(data.get("aspect_ratio") or "").strip() or _ratio_from_size(data.get("size", "1024x1024"))
    if ratio not in SUPPORTED_RATIOS:
        ratio = "1:1"

    resolved_model_id = model_id or DEFAULT_MODEL_ID
    if resolved_model_id not in MODEL_CATALOG:
        resolved_model_id = DEFAULT_MODEL_ID
    model_conf = MODEL_CATALOG[resolved_model_id]

    output_resolution = model_conf["output_resolution"]
    if not model_id:
        quality = str(data.get("quality", "2k")).lower()
        output_resolution = "2K" if quality in ("hd", "2k") else "1K"

    model_ratio = model_conf.get("aspect_ratio")
    if model_ratio:
        ratio = model_ratio

    return ratio, output_resolution, resolved_model_id


def _extract_access_key(request: Request) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def _require_service_api_key(request: Request) -> None:
    required = str(config_manager.get("api_key", "")).strip()
    if not required:
        return
    provided = _extract_access_key(request)
    if provided != required:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _public_image_url(request: Request, job_id: str) -> str:
    return str(request.url_for("generated_files", path=f"{job_id}.png"))


def _public_generated_url(request: Request, filename: str) -> str:
    return str(request.url_for("generated_files", path=filename))


def _video_ext_from_meta(meta: dict) -> str:
    content_type = str(meta.get("contentType") or "").lower()
    if "webm" in content_type:
        return "webm"
    if "ogg" in content_type or "ogv" in content_type:
        return "ogv"
    return "mp4"


def _sse_chat_stream(payload: dict):
    import json

    cid = payload["id"]
    created = payload["created"]
    model = payload["model"]
    content = payload["choices"][0]["message"]["content"]

    first = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": None,
            }
        ],
    }
    last = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }

    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"

@app.get("/api/v1/health")
def health():
    return {"status": "ok", "pool_size": len(token_manager.list_all())}


@app.get("/api/v1/logs")
def list_logs(limit: int = 100):
    return {"logs": log_store.list(limit=limit)}


@app.delete("/api/v1/logs")
def clear_logs():
    log_store.clear()
    return {"status": "ok"}


@app.get("/v1/models")
def list_models(request: Request):
    _require_service_api_key(request)
    data = []
    for model_id, conf in MODEL_CATALOG.items():
        data.append(
            {
                "id": model_id,
                "object": "model",
                "owned_by": "adobe2api",
                "description": conf["description"],
            }
        )
    for model_id, conf in VIDEO_MODEL_CATALOG.items():
        data.append(
            {
                "id": model_id,
                "object": "model",
                "owned_by": "adobe2api",
                "description": conf["description"],
            }
        )
    return {"object": "list", "data": data}


@app.get("/", include_in_schema=False)
def page_root():
    return FileResponse(STATIC_DIR / "admin.html")


# --- Token Management APIs ---

@app.get("/api/v1/tokens")
def list_tokens():
    return {"tokens": token_manager.list_all()}

@app.post("/api/v1/tokens")
def add_token(req: TokenAddRequest):
    if not req.token.strip():
        raise HTTPException(status_code=400, detail="Empty token")
    token_manager.add(req.token)
    return {"status": "ok"}

@app.delete("/api/v1/tokens/{tid}")
def delete_token(tid: str):
    token_manager.remove(tid)
    return {"status": "ok"}

@app.put("/api/v1/tokens/{tid}/status")
def set_token_status(tid: str, status: str):
    if status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail="Invalid status")
    token_info = token_manager.get_by_id(tid)
    if not token_info:
        raise HTTPException(status_code=404, detail="token not found")
    if status == "active" and token_info.get("status") in {"exhausted", "invalid"}:
        raise HTTPException(status_code=400, detail="exhausted/invalid token cannot be reactivated; replace with a fresh token")
    token_manager.set_status(tid, status)
    return {"status": "ok"}


@app.get("/api/v1/config")
def get_config():
    return config_manager.get_all()


@app.put("/api/v1/config")
def update_config(req: ConfigUpdateRequest):
    incoming = req.model_dump(exclude_unset=True)
    if not incoming:
        return config_manager.get_all()

    update_data = {}
    if "api_key" in incoming:
        update_data["api_key"] = str(incoming["api_key"] or "").strip()
    if "proxy" in incoming:
        update_data["proxy"] = str(incoming["proxy"] or "").strip()
    if "use_proxy" in incoming:
        update_data["use_proxy"] = bool(incoming["use_proxy"])
    if "generate_timeout" in incoming:
        try:
            timeout_val = int(incoming["generate_timeout"])
        except Exception:
            timeout_val = 300
        update_data["generate_timeout"] = timeout_val if timeout_val > 0 else 300
    config_manager.update_all(update_data)
    client.apply_config(config_manager.get_all())
    return config_manager.get_all()


@app.get("/api/v1/refresh-profile/status")
def refresh_profile_status():
    return refresh_manager.status()


@app.post("/api/v1/refresh-profile/import")
def refresh_profile_import(req: RefreshBundleImportRequest):
    try:
        refresh_manager.import_bundle(req.bundle)
        return {"status": "ok", "detail": "refresh profile imported"}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/refresh-profile/refresh-now")
def refresh_profile_refresh_now():
    try:
        return refresh_manager.refresh_once()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/v1/refresh-profile")
def refresh_profile_clear():
    refresh_manager.clear_bundle()
    return {"status": "ok"}


# --- Generation API (OpenAI Compatible structure) ---

@app.post("/v1/images/generations")
def openai_generate(data: dict, request: Request):
    """
    Minimal OpenAI compatible endpoint for image generation
    """
    _require_service_api_key(request)

    prompt = data.get("prompt", "").strip()
    if not prompt:
        return JSONResponse(status_code=400, content={"error": {"message": "prompt is required", "type": "invalid_request_error"}})

    model_id = data.get("model")
    if str(model_id or "").strip() in VIDEO_MODEL_CATALOG:
        return JSONResponse(status_code=400, content={"error": {"message": "Use /v1/chat/completions for video generation", "type": "invalid_request_error"}})
    ratio, output_resolution, resolved_model_id = _resolve_ratio_and_resolution(data, model_id)

    token = token_manager.get_available()
    if not token:
        return JSONResponse(status_code=503, content={"error": {"message": "No active tokens available in the pool", "type": "server_error"}})

    try:
        image_bytes, meta = client.generate(
            token=token,
            prompt=prompt,
            aspect_ratio=ratio,
            output_resolution=output_resolution,
            timeout=client.generate_timeout,
        )
        
        # 保存图片以便通过URL返回
        job_id = uuid.uuid4().hex
        out_path = GENERATED_DIR / f"{job_id}.png"
        out_path.write_bytes(image_bytes)
        
        image_url = _public_image_url(request, job_id)
        _set_request_preview(request, image_url, kind="image")

        return {
            "created": int(time.time()),
            "model": resolved_model_id,
            "data": [
                {"url": image_url}
            ]
        }
        
    except QuotaExhaustedError:
        token_manager.report_exhausted(token)
        return JSONResponse(status_code=429, content={"error": {"message": "Token quota exhausted", "type": "rate_limit_error"}})
    except AuthError:
        token_manager.report_invalid(token)
        return JSONResponse(status_code=401, content={"error": {"message": "Token invalid or expired", "type": "authentication_error"}})
    except UpstreamTemporaryError as exc:
        return JSONResponse(status_code=503, content={"error": {"message": str(exc), "type": "server_error"}})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": {"message": str(exc), "type": "server_error"}})


@app.post("/api/v1/generate")
def create_job(data: GenerateRequest, request: Request):
    _require_service_api_key(request)

    prompt = data.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt cannot be empty")

    ratio = data.aspect_ratio.strip() or "16:9"
    if ratio not in SUPPORTED_RATIOS:
        raise HTTPException(status_code=400, detail="unsupported aspect ratio")

    output_resolution = (data.output_resolution or "2K").upper()
    if output_resolution not in {"1K", "2K"}:
        raise HTTPException(status_code=400, detail="unsupported output_resolution")

    # If model is provided, use model suffix mapping as source of truth.
    if data.model:
        model_conf = _resolve_model(data.model)
        output_resolution = model_conf["output_resolution"]

    job = store.create(prompt=prompt, aspect_ratio=ratio)
    base_url = str(request.base_url).rstrip("/")

    def runner(job_id: str):
        store.update(job_id, status="running", progress=5.0)
        
        token = token_manager.get_available()
        if not token:
            store.update(job_id, status="failed", error="No active tokens available in the pool")
            return
            
        try:
            image_bytes, meta = client.generate(
                token=token,
                prompt=prompt,
                aspect_ratio=ratio,
                output_resolution=output_resolution,
            )
            out_path = GENERATED_DIR / f"{job_id}.png"
            out_path.write_bytes(image_bytes)
            progress = float(meta.get("progress") or 100.0)
            image_url = f"{base_url}/generated/{job_id}.png"
            store.update(job_id, status="succeeded", progress=max(progress, 100.0), image_url=image_url)
        except QuotaExhaustedError as exc:
            token_manager.report_exhausted(token)
            store.update(job_id, status="failed", error="Token quota exhausted.")
        except AuthError as exc:
            token_manager.report_invalid(token)
            store.update(job_id, status="failed", error="Token invalid or expired.")
        except UpstreamTemporaryError as exc:
            store.update(job_id, status="failed", error=str(exc))
        except Exception as exc:
            store.update(job_id, status="failed", error=str(exc))

    threading.Thread(target=runner, args=(job.id,), daemon=True).start()

    return {"task_id": job.id, "status": job.status}


@app.get("/api/v1/generate/{task_id}")
def get_job(task_id: str, request: Request):
    _require_service_api_key(request)

    job = store.get(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="task not found")
    return asdict(job)


@app.post("/v1/chat/completions")
def chat_completions(data: dict, request: Request):
    _require_service_api_key(request)

    prompt = _extract_prompt_from_messages(data.get("messages") or [])
    if not prompt:
        prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "messages or prompt is required", "type": "invalid_request_error"}},
        )

    model_id = str(data.get("model") or "").strip()
    if model_id.startswith("firefly-sora2") and model_id not in VIDEO_MODEL_CATALOG:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "Invalid video model. Use /v1/models to get supported firefly-sora2-* models",
                    "type": "invalid_request_error",
                }
            },
        )
    video_conf = VIDEO_MODEL_CATALOG.get(model_id)
    is_video_model = video_conf is not None
    resolved_model_id = model_id if is_video_model else None
    ratio = "9:16"
    output_resolution = "2K"
    duration = int(video_conf["duration"]) if video_conf else 12
    if video_conf:
        ratio = str(video_conf.get("aspect_ratio") or ratio)
    generate_audio = True
    negative_prompt = ""
    if is_video_model:
        generate_audio, negative_prompt = _resolve_video_options(data)
    else:
        ratio, output_resolution, resolved_model_id = _resolve_ratio_and_resolution(data, model_id or None)

    token = token_manager.get_available()
    if not token:
        return JSONResponse(status_code=503, content={"error": {"message": "No active tokens available in the pool", "type": "server_error"}})

    try:
        input_images = _load_input_images(data.get("messages") or [])
        source_image_ids: list[str] = []
        image_url = ""
        response_label = "generated image"

        if is_video_model:
            if input_images:
                prepared_bytes, prepared_mime = _prepare_video_source_image(input_images[0][0], ratio)
                source_image_ids.append(client.upload_image(token, prepared_bytes, prepared_mime))

            video_bytes, video_meta = client.generate_video(
                token=token,
                prompt=prompt,
                aspect_ratio=ratio,
                duration=duration,
                source_image_ids=source_image_ids,
                timeout=max(int(client.generate_timeout), 600),
                negative_prompt=negative_prompt,
                generate_audio=generate_audio,
            )
            job_id = uuid.uuid4().hex
            video_ext = _video_ext_from_meta(video_meta)
            filename = f"{job_id}.{video_ext}"
            out_path = GENERATED_DIR / filename
            out_path.write_bytes(video_bytes)
            image_url = _public_generated_url(request, filename)
            _set_request_preview(request, image_url, kind="video")
            response_label = "generated video"
        else:
            for image_bytes, image_mime in input_images:
                source_image_ids.append(client.upload_image(token, image_bytes, image_mime or "image/jpeg"))

            image_bytes, _meta = client.generate(
                token=token,
                prompt=prompt,
                aspect_ratio=ratio,
                output_resolution=output_resolution,
                source_image_ids=source_image_ids,
                timeout=client.generate_timeout,
            )
            job_id = uuid.uuid4().hex
            out_path = GENERATED_DIR / f"{job_id}.png"
            out_path.write_bytes(image_bytes)
            image_url = _public_image_url(request, job_id)
            _set_request_preview(request, image_url, kind="image")

        response_payload = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": resolved_model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"![{response_label}]({image_url})\n\n{image_url}",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        if bool(data.get("stream", False)):
            return StreamingResponse(_sse_chat_stream(response_payload), media_type="text/event-stream")
        return response_payload
    except QuotaExhaustedError:
        token_manager.report_exhausted(token)
        return JSONResponse(status_code=429, content={"error": {"message": "Token quota exhausted", "type": "rate_limit_error"}})
    except AuthError:
        token_manager.report_invalid(token)
        return JSONResponse(status_code=401, content={"error": {"message": "Token invalid or expired", "type": "authentication_error"}})
    except UpstreamTemporaryError as exc:
        return JSONResponse(status_code=503, content={"error": {"message": str(exc), "type": "server_error"}})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": {"message": str(exc), "type": "server_error"}})


if __name__ == "__main__":
    import uvicorn
    # 为了在容器中更好工作，使用环境变量
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "6001")))
