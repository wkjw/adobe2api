import os
import json
import logging
import threading
import time
import uuid
import base64
import binascii
import io
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List, Any, Callable
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

for _res in ("1k", "2k", "4k"):
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
    "firefly-sora2-4s-9x16": {
        "duration": 4,
        "aspect_ratio": "9:16",
        "description": "Firefly Sora2 video model (4s 9:16)",
    },
    "firefly-sora2-4s-16x9": {
        "duration": 4,
        "aspect_ratio": "16:9",
        "description": "Firefly Sora2 video model (4s 16:9)",
    },
    "firefly-sora2-8s-9x16": {
        "duration": 8,
        "aspect_ratio": "9:16",
        "description": "Firefly Sora2 video model (8s 9:16)",
    },
    "firefly-sora2-8s-16x9": {
        "duration": 8,
        "aspect_ratio": "16:9",
        "description": "Firefly Sora2 video model (8s 16:9)",
    },
    "firefly-sora2-12s-9x16": {
        "duration": 12,
        "aspect_ratio": "9:16",
        "description": "Firefly Sora2 video model (12s 9:16)",
    },
    "firefly-sora2-12s-16x9": {
        "duration": 12,
        "aspect_ratio": "16:9",
        "description": "Firefly Sora2 video model (12s 16:9)",
    },
}

for _dur in (4, 6, 8):
    for _ratio in ("16:9", "9:16"):
        for _res in ("1080p", "720p"):
            _model_id = f"firefly-veo31-fast-{_dur}s-{RATIO_SUFFIX_MAP[_ratio]}-{_res}"
            VIDEO_MODEL_CATALOG[_model_id] = {
                "engine": "veo31-fast",
                "upstream_model": "google:firefly:colligo:veo31-fast",
                "duration": _dur,
                "aspect_ratio": _ratio,
                "resolution": _res,
                "description": f"Firefly Veo31 Fast video model ({_dur}s {_ratio} {_res})",
            }


class AdobeRequestError(Exception):
    pass


class QuotaExhaustedError(AdobeRequestError):
    pass


class AuthError(AdobeRequestError):
    pass


class UpstreamTemporaryError(AdobeRequestError):
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        error_type: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = str(error_type or "").strip().lower()


class AdobeClient:
    submit_url = "https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async"
    video_submit_url = "https://firefly-3p.ff.adobe.io/v2/3p-videos/generate-async"
    upload_url = "https://firefly-3p.ff.adobe.io/v2/storage/image"

    def __init__(self) -> None:
        self.api_key = "clio-playground-web"
        self.impersonate = "chrome124"
        self.proxy = ""
        self.generate_timeout = 300
        self.retry_enabled = True
        self.retry_max_attempts = 3
        self.retry_backoff_seconds = 1.0
        self.retry_on_status_codes = [429, 451, 500, 502, 503, 504]
        self.retry_on_error_types = {"timeout", "connection", "proxy"}
        self.token_rotation_strategy = "round_robin"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        self.sec_ch_ua = (
            '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'
        )

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
        self.retry_enabled = bool(cfg.get("retry_enabled", True))
        try:
            attempts = int(cfg.get("retry_max_attempts", 3))
        except Exception:
            attempts = 3
        self.retry_max_attempts = max(1, min(attempts, 10))

        try:
            backoff = float(cfg.get("retry_backoff_seconds", 1.0))
        except Exception:
            backoff = 1.0
        self.retry_backoff_seconds = max(0.0, min(backoff, 30.0))

        status_codes_raw = cfg.get(
            "retry_on_status_codes", [429, 451, 500, 502, 503, 504]
        )
        parsed_status_codes: list[int] = []
        if isinstance(status_codes_raw, list):
            for item in status_codes_raw:
                try:
                    val = int(item)
                except Exception:
                    continue
                if 100 <= val <= 599:
                    parsed_status_codes.append(val)
        self.retry_on_status_codes = sorted(set(parsed_status_codes)) or [
            429,
            451,
            500,
            502,
            503,
            504,
        ]

        error_types_raw = cfg.get(
            "retry_on_error_types", ["timeout", "connection", "proxy"]
        )
        parsed_error_types: set[str] = set()
        if isinstance(error_types_raw, list):
            for item in error_types_raw:
                txt = str(item or "").strip().lower()
                if txt:
                    parsed_error_types.add(txt)
        self.retry_on_error_types = parsed_error_types or {
            "timeout",
            "connection",
            "proxy",
        }

        strategy = (
            str(cfg.get("token_rotation_strategy", "round_robin") or "round_robin")
            .strip()
            .lower()
        )
        if strategy not in {"round_robin", "random"}:
            strategy = "round_robin"
        self.token_rotation_strategy = strategy
        if self.proxy:
            logger.warning("proxy enabled for upstream requests: %s", self.proxy)
        else:
            logger.warning("proxy disabled for upstream requests")

    def _retry_delay_for_attempt(self, attempt: int) -> float:
        base = float(self.retry_backoff_seconds or 0.0)
        if base <= 0:
            return 0.0
        safe_attempt = max(1, int(attempt))
        return min(30.0, base * (2 ** (safe_attempt - 1)))

    def should_retry_temporary_error(self, exc: UpstreamTemporaryError) -> bool:
        if not self.retry_enabled:
            return False
        if isinstance(exc, UpstreamTemporaryError):
            if exc.status_code is not None:
                try:
                    return int(exc.status_code) in set(self.retry_on_status_codes)
                except Exception:
                    return False
            if exc.error_type:
                return exc.error_type in set(self.retry_on_error_types)
        return False

    @staticmethod
    def _classify_network_error_type(exc: Exception) -> str:
        text = str(exc or "").strip().lower()
        if "timed out" in text or "timeout" in text:
            return "timeout"
        if "proxy" in text:
            return "proxy"
        if (
            "connection" in text
            or "dns" in text
            or "resolve" in text
            or "refused" in text
            or "reset" in text
            or "unreachable" in text
        ):
            return "connection"
        return "network"

    def _requests_proxies(self) -> Optional[dict]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def _session(self):
        if CurlSession is None:
            return None
        kwargs = {"impersonate": self.impersonate, "timeout": 60}
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
            try:
                return requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=60,
                    proxies=self._requests_proxies(),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", error_type="timeout"
                )
            except requests.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}", error_type="connection"
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}", error_type="network"
                )
        try:
            with session:
                resp = session.post(url, headers=headers, json=payload)
        except Exception as exc:
            raise UpstreamTemporaryError(
                f"upstream session error: {exc}",
                error_type=self._classify_network_error_type(exc),
            )
        # Some environments return intermittent 451 via curl_cffi path.
        # Retry once with plain requests for better stability.
        if resp.status_code == 451:
            try:
                return requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=60,
                    proxies=self._requests_proxies(),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", status_code=451, error_type="timeout"
                )
            except requests.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", status_code=451, error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}",
                    status_code=451,
                    error_type="connection",
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}",
                    status_code=451,
                    error_type="network",
                )
        return resp

    def _post_bytes(self, url: str, headers: dict, payload: bytes):
        session = self._session()
        if session is None:
            try:
                return requests.post(
                    url,
                    headers=headers,
                    data=payload,
                    timeout=60,
                    proxies=self._requests_proxies(),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", error_type="timeout"
                )
            except requests.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}", error_type="connection"
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}", error_type="network"
                )
        try:
            with session:
                resp = session.post(url, headers=headers, data=payload)
        except Exception as exc:
            raise UpstreamTemporaryError(
                f"upstream session error: {exc}",
                error_type=self._classify_network_error_type(exc),
            )
        return resp

    def _get(self, url: str, headers: dict, timeout: int = 60):
        session = self._session()
        if session is None:
            try:
                return requests.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    proxies=self._requests_proxies(),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", error_type="timeout"
                )
            except requests.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}", error_type="connection"
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}", error_type="network"
                )
        try:
            with session:
                resp = session.get(url, headers=headers)
        except Exception as exc:
            raise UpstreamTemporaryError(
                f"upstream session error: {exc}",
                error_type=self._classify_network_error_type(exc),
            )
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
        elif level == "4K":
            ratio_map = {
                "1:1": {"width": 4096, "height": 4096},
                "16:9": {"width": 5504, "height": 3072},
                "9:16": {"width": 3072, "height": 5504},
                "4:3": {"width": 4096, "height": 3072},
                "3:4": {"width": 3072, "height": 4096},
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

    def upload_image(
        self, token: str, image_bytes: bytes, mime_type: str = "image/jpeg"
    ) -> str:
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
            if resp.status_code in (429, 451) or resp.status_code >= 500:
                raise UpstreamTemporaryError(
                    f"upload image failed: {resp.status_code} {resp.text[:300]}",
                    status_code=resp.status_code,
                    error_type="status",
                )
            raise AdobeRequestError(
                f"upload image failed: {resp.status_code} {resp.text[:300]}"
            )

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
        c1["referenceBlobs"] = [
            {"id": img_id, "usage": "general"} for img_id in source_image_ids
        ]
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
    def _video_size(aspect_ratio: str, resolution: str = "720p") -> dict:
        res = str(resolution or "720p").lower()
        if res == "1080p":
            if aspect_ratio == "16:9":
                return {"width": 1920, "height": 1080}
            return {"width": 1080, "height": 1920}
        if aspect_ratio == "16:9":
            return {"width": 1280, "height": 720}
        return {"width": 720, "height": 1280}

    @staticmethod
    def _coerce_progress_percent(value: Any) -> Optional[float]:
        if value is None:
            return None

        val: Optional[float] = None
        if isinstance(value, (int, float)):
            val = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.endswith("%"):
                text = text[:-1].strip()
            try:
                val = float(text)
            except Exception:
                return None
        elif isinstance(value, dict):
            for key in (
                "progress",
                "percentage",
                "percent",
                "task_progress",
                "taskProgress",
                "value",
            ):
                nested = AdobeClient._coerce_progress_percent(value.get(key))
                if nested is not None:
                    return nested
            return None
        else:
            return None

        if val <= 1.0:
            val = val * 100.0
        if val < 0:
            return 0.0
        if val > 100:
            return 100.0
        return val

    @staticmethod
    def _is_in_progress_status(status_val: str) -> bool:
        return str(status_val or "").upper() in {
            "IN_PROGRESS",
            "RUNNING",
            "PROCESSING",
            "PENDING",
            "QUEUED",
            "STARTED",
        }

    def _extract_progress_percent(self, latest: dict, poll_resp) -> Optional[float]:
        if not isinstance(latest, dict):
            latest = {}

        task_obj = latest.get("task") if isinstance(latest.get("task"), dict) else {}
        result_obj = (
            latest.get("result") if isinstance(latest.get("result"), dict) else {}
        )
        meta_obj = latest.get("meta") if isinstance(latest.get("meta"), dict) else {}
        metadata_obj = (
            latest.get("metadata") if isinstance(latest.get("metadata"), dict) else {}
        )

        candidates: list[Any] = [
            latest.get("progress"),
            latest.get("percentage"),
            latest.get("percent"),
            latest.get("task_progress"),
            latest.get("taskProgress"),
            task_obj.get("progress"),
            task_obj.get("percentage"),
            result_obj.get("progress"),
            result_obj.get("percentage"),
            meta_obj.get("progress"),
            metadata_obj.get("progress"),
            poll_resp.headers.get("x-task-progress"),
            poll_resp.headers.get("x-progress"),
            poll_resp.headers.get("progress"),
        ]

        for raw in candidates:
            parsed = self._coerce_progress_percent(raw)
            if parsed is not None:
                return parsed
        return None

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
    def _extract_job_id(raw_url: str) -> str:
        try:
            parsed = urlparse(str(raw_url or ""))
            path_parts = [p for p in parsed.path.split("/") if p]
            if path_parts:
                return path_parts[-1]
        except Exception:
            pass
        return ""

    @staticmethod
    def _build_video_prompt_json(
        prompt: str, duration: int, negative_prompt: str = ""
    ) -> str:
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
        video_conf: dict,
        prompt: str,
        aspect_ratio: str,
        duration: int,
        source_image_ids: Optional[list[str]] = None,
        negative_prompt: str = "",
        generate_audio: bool = True,
    ) -> dict:
        seed_val = int(time.time()) % 999999
        engine = str(video_conf.get("engine") or "sora2")
        upstream_model = str(
            video_conf.get("upstream_model") or "openai:firefly:colligo:sora2"
        )
        resolution = str(video_conf.get("resolution") or "720p")
        if engine == "veo31-fast":
            payload = {
                "n": 1,
                "seeds": [seed_val],
                "modelId": "veo",
                "modelVersion": "3.1-fast-generate",
                "output": {"storeInputs": True},
                "prompt": prompt,
                "size": self._video_size(aspect_ratio, resolution),
                "generateAudio": bool(generate_audio),
                "referenceBlobs": [],
                "generationMetadata": {"module": "text2video"},
                "modelSpecificPayload": {
                    "parameters": {
                        "durationSeconds": int(duration),
                        "aspectRatio": aspect_ratio,
                        "addWaterMark": False,
                    }
                },
            }
            if source_image_ids:
                for idx, image_id in enumerate(source_image_ids[:2], start=1):
                    payload["referenceBlobs"].append(
                        {
                            "id": str(image_id),
                            "usage": "general",
                            "promptReference": idx,
                        }
                    )
            return payload

        payload = {
            "n": 1,
            "seeds": [seed_val],
            "modelId": "sora",
            "modelVersion": "sora-2",
            "size": self._video_size(aspect_ratio, resolution),
            "duration": int(duration),
            "fps": 24,
            "prompt": self._build_video_prompt_json(
                prompt=prompt, duration=duration, negative_prompt=negative_prompt
            ),
            "generationMetadata": {"module": "text2video"},
            "model": upstream_model,
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
            payload["referenceBlobs"] = [
                {"id": first_id, "usage": "general", "promptReference": 1}
            ]
            reference_frames = [{"localBlobRef": first_id}, None]
            if engine == "veo31-fast" and len(source_image_ids) > 1:
                last_id = str(source_image_ids[1])
                payload["referenceBlobs"].append(
                    {"id": last_id, "usage": "general", "promptReference": 2}
                )
                reference_frames[1] = {"localBlobRef": last_id}
            payload["referenceFrames"] = reference_frames
        return payload

    def generate_video(
        self,
        token: str,
        video_conf: dict,
        prompt: str,
        aspect_ratio: str = "9:16",
        duration: int = 12,
        source_image_ids: Optional[list[str]] = None,
        timeout: int = 600,
        negative_prompt: str = "",
        generate_audio: bool = True,
        progress_cb: Optional[Callable[[dict], None]] = None,
    ) -> tuple[bytes, dict]:
        payload = self._build_video_payload(
            video_conf=video_conf,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            duration=duration,
            source_image_ids=source_image_ids,
            negative_prompt=negative_prompt,
            generate_audio=generate_audio,
        )
        submit_resp = self._post_json(
            self.video_submit_url, headers=self._submit_headers(token), payload=payload
        )

        if submit_resp.status_code in (401, 403):
            access_error = submit_resp.headers.get("x-access-error")
            if access_error == "taste_exhausted":
                raise QuotaExhaustedError("Adobe quota exhausted for this account")
            raise AuthError("Token invalid or expired")

        if submit_resp.status_code != 200:
            if submit_resp.status_code in (429, 451) or submit_resp.status_code >= 500:
                raise UpstreamTemporaryError(
                    f"video submit failed: {submit_resp.status_code} {submit_resp.text[:300]}",
                    status_code=submit_resp.status_code,
                    error_type="status",
                )
            raise AdobeRequestError(
                f"video submit failed: {submit_resp.status_code} {submit_resp.text[:300]}"
            )

        submit_data = submit_resp.json()
        poll_url = submit_resp.headers.get("x-override-status-link") or (
            (submit_data.get("links") or {}).get("result") or {}
        ).get("href")
        if not poll_url:
            raise AdobeRequestError("video submit succeeded but no poll url returned")
        poll_url = self._normalize_video_poll_url(str(poll_url))
        upstream_job_id = self._extract_job_id(poll_url)
        if progress_cb:
            try:
                progress_cb(
                    {
                        "task_status": "IN_PROGRESS",
                        "task_progress": 0.0,
                        "upstream_job_id": upstream_job_id,
                        "retry_after": int(submit_resp.headers.get("retry-after") or 0)
                        or None,
                    }
                )
            except Exception:
                pass

        start = time.time()
        while True:
            poll_resp = self._get(
                poll_url, headers=self._poll_headers(token), timeout=60
            )
            if poll_resp.status_code in (401, 403):
                raise AuthError("Token invalid or expired")
            if poll_resp.status_code != 200:
                if poll_resp.status_code in (429, 451) or poll_resp.status_code >= 500:
                    raise UpstreamTemporaryError(
                        f"video poll failed: {poll_resp.status_code} {poll_resp.text[:300]}",
                        status_code=poll_resp.status_code,
                        error_type="status",
                    )
                raise AdobeRequestError(
                    f"video poll failed: {poll_resp.status_code} {poll_resp.text[:300]}"
                )

            latest = poll_resp.json()
            status_header = str(poll_resp.headers.get("x-task-status") or "").upper()
            status_val = str(latest.get("status") or "").upper() or status_header
            progress_val = self._extract_progress_percent(latest, poll_resp)

            if progress_cb and self._is_in_progress_status(status_val):
                try:
                    progress_cb(
                        {
                            "task_status": "IN_PROGRESS",
                            "task_progress": progress_val
                            if progress_val is not None
                            else 0.0,
                            "upstream_job_id": upstream_job_id,
                            "retry_after": int(
                                poll_resp.headers.get("retry-after") or 0
                            )
                            or None,
                        }
                    )
                except Exception:
                    pass

            outputs = latest.get("outputs") or []
            if outputs:
                video_url = ((outputs[0] or {}).get("video") or {}).get("presignedUrl")
                if not video_url:
                    raise AdobeRequestError("video job finished without video url")
                video_resp = self._get(video_url, headers={"accept": "*/*"}, timeout=60)
                video_resp.raise_for_status()
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "COMPLETED",
                                "task_progress": 100.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                            }
                        )
                    except Exception:
                        pass
                return video_resp.content, latest

            if status_val in {"FAILED", "CANCELLED", "ERROR"}:
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "FAILED",
                                "task_progress": progress_val
                                if progress_val is not None
                                else 0.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                                "error": f"video job failed: {latest}",
                            }
                        )
                    except Exception:
                        pass
                raise AdobeRequestError(f"video job failed: {latest}")

            if time.time() - start > timeout:
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "FAILED",
                                "task_progress": progress_val
                                if "progress_val" in locals()
                                and progress_val is not None
                                else 0.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                                "error": "video generation timed out",
                            }
                        )
                    except Exception:
                        pass
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
        progress_cb: Optional[Callable[[dict], None]] = None,
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
            submit_resp = self._post_json(
                self.submit_url, headers=self._submit_headers(token), payload=payload
            )
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
                raise UpstreamTemporaryError(
                    f"submit failed: {submit_resp.status_code} {submit_resp.text[:300]}",
                    status_code=submit_resp.status_code,
                    error_type="status",
                )
            if last_error:
                raise AdobeRequestError(
                    f"submit failed: {submit_resp.status_code} {last_error}"
                )
            raise AdobeRequestError(
                f"submit failed: {submit_resp.status_code} {submit_resp.text[:300]}"
            )

        submit_data = submit_resp.json()
        poll_url = submit_resp.headers.get("x-override-status-link") or (
            (submit_data.get("links") or {}).get("result") or {}
        ).get("href")
        if not poll_url:
            raise AdobeRequestError("submit succeeded but no poll url returned")

        upstream_job_id = self._extract_job_id(poll_url)
        if progress_cb:
            try:
                progress_cb(
                    {
                        "task_status": "IN_PROGRESS",
                        "task_progress": 0.0,
                        "upstream_job_id": upstream_job_id,
                        "retry_after": int(submit_resp.headers.get("retry-after") or 0)
                        or None,
                    }
                )
            except Exception:
                pass

        start = time.time()
        latest = {}
        # 延长轮询间隔，减少请求次数
        sleep_time = 3.0
        while True:
            poll_resp = self._get(
                poll_url, headers=self._poll_headers(token), timeout=60
            )
            if poll_resp.status_code != 200:
                logger.error(
                    "poll failed status=%s body=%s",
                    poll_resp.status_code,
                    poll_resp.text[:500],
                )
                if poll_resp.status_code in (429, 451) or poll_resp.status_code >= 500:
                    raise UpstreamTemporaryError(
                        f"poll failed: {poll_resp.status_code} {poll_resp.text[:300]}",
                        status_code=poll_resp.status_code,
                        error_type="status",
                    )
                raise AdobeRequestError(
                    f"poll failed: {poll_resp.status_code} {poll_resp.text[:300]}"
                )

            latest = poll_resp.json()
            status_header = str(poll_resp.headers.get("x-task-status") or "").upper()
            status_val = str(latest.get("status") or "").upper() or status_header
            progress_val = self._extract_progress_percent(latest, poll_resp)

            if progress_cb and self._is_in_progress_status(status_val):
                try:
                    progress_cb(
                        {
                            "task_status": "IN_PROGRESS",
                            "task_progress": progress_val
                            if progress_val is not None
                            else 0.0,
                            "upstream_job_id": upstream_job_id,
                            "retry_after": int(
                                poll_resp.headers.get("retry-after") or 0
                            )
                            or None,
                        }
                    )
                except Exception:
                    pass

            outputs = latest.get("outputs") or []
            if outputs:
                image_url = ((outputs[0] or {}).get("image") or {}).get("presignedUrl")
                if not image_url:
                    raise AdobeRequestError("job finished without image url")
                img_resp = self._get(image_url, headers={"accept": "*/*"}, timeout=30)
                img_resp.raise_for_status()
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "COMPLETED",
                                "task_progress": 100.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                            }
                        )
                    except Exception:
                        pass
                return img_resp.content, latest

            if status_val in {"FAILED", "CANCELLED", "ERROR"}:
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "FAILED",
                                "task_progress": progress_val
                                if progress_val is not None
                                else 0.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                                "error": f"image job failed: {latest}",
                            }
                        )
                    except Exception:
                        pass
                raise AdobeRequestError(f"image job failed: {latest}")

            if time.time() - start > timeout:
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "FAILED",
                                "task_progress": progress_val
                                if progress_val is not None
                                else 0.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                                "error": "image generation timed out",
                            }
                        )
                    except Exception:
                        pass
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
    operation: str
    preview_url: Optional[str] = None
    preview_kind: Optional[str] = None
    model: Optional[str] = None
    prompt_preview: Optional[str] = None
    error: Optional[str] = None
    task_status: Optional[str] = None
    task_progress: Optional[float] = None
    upstream_job_id: Optional[str] = None
    retry_after: Optional[int] = None
    token_id: Optional[str] = None
    token_account_name: Optional[str] = None
    token_account_email: Optional[str] = None
    token_source: Optional[str] = None
    token_attempt: Optional[int] = None


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

    def upsert(self, item_id: str, payload: dict) -> None:
        if not item_id:
            return
        if not isinstance(payload, dict):
            return
        with self._lock:
            with self._file_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()

            updated = False
            out_lines: list[str] = []
            for line in lines:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                if isinstance(item, dict) and str(item.get("id") or "") == item_id:
                    item.update(payload)
                    updated = True
                out_lines.append(json.dumps(item, ensure_ascii=False) + "\n")

            if not updated:
                item = {"id": item_id}
                item.update(payload)
                out_lines.append(json.dumps(item, ensure_ascii=False) + "\n")

            if len(out_lines) > self._max_items:
                out_lines = out_lines[-self._max_items :]

            with self._file_path.open("w", encoding="utf-8") as f:
                f.writelines(out_lines)

    def list(self, limit: int = 20, page: int = 1) -> tuple[list[dict], int]:
        safe_limit = min(max(int(limit or 20), 1), 100)
        safe_page = max(int(page or 1), 1)
        with self._lock:
            with self._file_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()

        total = len(lines)
        if total <= 0:
            return [], 0

        end = total - (safe_page - 1) * safe_limit
        start = max(0, end - safe_limit)
        if end <= 0:
            return [], total

        selected = lines[start:end]
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
        return data, total

    def stats(
        self,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> dict:
        with self._lock:
            with self._file_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()

        total_requests = 0
        failed_requests = 0
        generated_images = 0
        generated_videos = 0
        in_progress_requests = 0

        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except Exception:
                continue
            if not isinstance(item, dict):
                continue

            try:
                ts_val = float(item.get("ts") or 0)
            except Exception:
                ts_val = 0.0
            if start_ts is not None and ts_val < float(start_ts):
                continue
            if end_ts is not None and ts_val > float(end_ts):
                continue

            total_requests += 1

            try:
                status_code = int(item.get("status_code") or 0)
            except Exception:
                status_code = 0
            if status_code >= 400:
                failed_requests += 1

            task_status = str(item.get("task_status") or "").upper()
            if task_status == "IN_PROGRESS":
                in_progress_requests += 1

            preview_kind = str(item.get("preview_kind") or "").strip().lower()
            if 200 <= status_code < 300:
                if preview_kind == "image":
                    generated_images += 1
                elif preview_kind == "video":
                    generated_videos += 1

        return {
            "total_requests": total_requests,
            "failed_requests": failed_requests,
            "generated_images": generated_images,
            "generated_videos": generated_videos,
            "generated_total": generated_images + generated_videos,
            "in_progress_requests": in_progress_requests,
        }

    def clear(self) -> None:
        with self._lock:
            with self._file_path.open("w", encoding="utf-8") as f:
                f.write("")


# 极简配置启动
app = FastAPI(
    title="adobe2api",
    version="0.1.0",
    docs_url=None,  # 关闭 swagger，节省资源
    redoc_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/generated", StaticFiles(directory=GENERATED_DIR), name="generated_files")

store = JobStore()
log_store = RequestLogStore(DATA_DIR / "request_logs.jsonl", max_items=5000)
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


def _set_request_task_progress(
    request: Request,
    task_status: str,
    task_progress: Optional[float] = None,
    upstream_job_id: Optional[str] = None,
    retry_after: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    patch: dict[str, Any] = {"task_status": str(task_status or "").upper()}
    if task_progress is not None:
        try:
            progress_val = float(task_progress)
            if progress_val < 0:
                progress_val = 0.0
            if progress_val > 100:
                progress_val = 100.0
            patch["task_progress"] = round(progress_val, 2)
        except Exception:
            pass
    if upstream_job_id:
        patch["upstream_job_id"] = str(upstream_job_id)
    if retry_after is not None:
        try:
            patch["retry_after"] = int(retry_after)
        except Exception:
            pass
    if error:
        patch["error"] = str(error)[:240]

    try:
        request.state.log_task_status = patch.get("task_status")
        request.state.log_task_progress = patch.get("task_progress")
        request.state.log_upstream_job_id = patch.get("upstream_job_id")
        request.state.log_retry_after = patch.get("retry_after")
        if patch.get("error"):
            request.state.log_error = patch.get("error")
    except Exception:
        pass

    # Do not write partial records here.
    # Final request logs are emitted either by per-attempt logging
    # (_append_attempt_log) or by middleware finalization.


def _set_request_token_context(request: Request, token: str, attempt: int) -> dict:
    meta = token_manager.get_meta_by_value(token)
    try:
        request.state.log_token_id = meta.get("token_id")
        request.state.log_token_account_name = meta.get("token_account_name")
        request.state.log_token_account_email = meta.get("token_account_email")
        request.state.log_token_source = meta.get("token_source")
        request.state.log_token_attempt = int(attempt)
    except Exception:
        pass
    return meta


def _append_attempt_log(
    request: Request,
    operation: str,
    token_meta: dict,
    attempt: int,
    attempt_started: float,
    status_code: int,
    error: Optional[str] = None,
) -> None:
    try:
        root_log_id = str(getattr(request.state, "log_id", "") or uuid.uuid4().hex[:12])
        attempt_id = f"{root_log_id}-a{attempt}"
        method = str(getattr(request, "method", "POST") or "POST").upper()
        path = str(getattr(getattr(request, "url", None), "path", "") or "")
        model = getattr(request.state, "log_model", None)
        prompt_preview = getattr(request.state, "log_prompt_preview", None)
        preview_url = getattr(request.state, "log_preview_url", None)
        preview_kind = getattr(request.state, "log_preview_kind", None)
        task_status = getattr(request.state, "log_task_status", None)
        task_progress = getattr(request.state, "log_task_progress", None)
        upstream_job_id = getattr(request.state, "log_upstream_job_id", None)
        retry_after = getattr(request.state, "log_retry_after", None)
        duration_sec = int(max(0.0, time.time() - float(attempt_started)))
        log_store.add(
            RequestLogRecord(
                id=attempt_id,
                ts=time.time(),
                method=method,
                path=path,
                status_code=int(status_code),
                duration_sec=duration_sec,
                operation=operation,
                preview_url=preview_url,
                preview_kind=preview_kind,
                model=model,
                prompt_preview=prompt_preview,
                error=(str(error)[:240] if error else None),
                task_status=task_status,
                task_progress=task_progress,
                upstream_job_id=upstream_job_id,
                retry_after=retry_after,
                token_id=str(token_meta.get("token_id") or "") or None,
                token_account_name=(
                    str(token_meta.get("token_account_name") or "") or None
                ),
                token_account_email=(
                    str(token_meta.get("token_account_email") or "") or None
                ),
                token_source=str(token_meta.get("token_source") or "") or None,
                token_attempt=int(attempt),
            )
        )
        request.state.log_has_attempt_logs = True
    except Exception:
        pass


@app.middleware("http")
async def request_logger(request: Request, call_next):
    started = time.time()
    method = request.method.upper()
    path = request.url.path
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
            if path in {
                "/v1/images/generations",
                "/v1/chat/completions",
                "/api/v1/generate",
            }:
                body_meta = _extract_logging_fields(raw_body)
                request.state.log_model = body_meta.get("model")
                request.state.log_prompt_preview = body_meta.get("prompt_preview")
            request.state.log_id = uuid.uuid4().hex[:12]
        except Exception:
            pass

    response = None
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {str(exc)}"[:240]
        logger.exception(
            "Unhandled exception in request pipeline method=%s path=%s log_id=%s",
            method,
            path,
            getattr(request.state, "log_id", ""),
        )
        raise
    finally:
        if should_log:
            has_attempt_logs = bool(
                getattr(request.state, "log_has_attempt_logs", False)
            )
            if not has_attempt_logs:
                duration_sec = int(time.time() - started)
                preview_url = getattr(request.state, "log_preview_url", None)
                preview_kind = getattr(request.state, "log_preview_kind", None)
                task_status = getattr(request.state, "log_task_status", None)
                task_progress = getattr(request.state, "log_task_progress", None)
                upstream_job_id = getattr(request.state, "log_upstream_job_id", None)
                retry_after = getattr(request.state, "log_retry_after", None)
                error_final = getattr(request.state, "log_error", None) or error_text
                token_id = getattr(request.state, "log_token_id", None)
                token_account_name = getattr(
                    request.state, "log_token_account_name", None
                )
                token_account_email = getattr(
                    request.state, "log_token_account_email", None
                )
                token_source = getattr(request.state, "log_token_source", None)
                token_attempt = getattr(request.state, "log_token_attempt", None)
                log_id = (
                    str(getattr(request.state, "log_id", "") or "")
                    or uuid.uuid4().hex[:12]
                )
                log_store.upsert(
                    log_id,
                    asdict(
                        RequestLogRecord(
                            id=log_id,
                            ts=time.time(),
                            method=method,
                            path=path,
                            status_code=status_code,
                            duration_sec=duration_sec,
                            operation=operation,
                            preview_url=preview_url,
                            preview_kind=preview_kind,
                            model=body_meta.get("model"),
                            prompt_preview=body_meta.get("prompt_preview"),
                            error=error_final,
                            task_status=task_status,
                            task_progress=task_progress,
                            upstream_job_id=upstream_job_id,
                            retry_after=retry_after,
                            token_id=token_id,
                            token_account_name=token_account_name,
                            token_account_email=token_account_email,
                            token_source=token_source,
                            token_attempt=token_attempt,
                        )
                    ),
                )
    return response


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1200)
    aspect_ratio: str = Field(default="16:9")
    output_resolution: str = Field(default="2K")
    model: Optional[str] = None


class TokenAddRequest(BaseModel):
    token: str


class TokenBatchAddRequest(BaseModel):
    tokens: List[str]


class ExportSelectionRequest(BaseModel):
    ids: Optional[List[str]] = None


class TokenCreditsBatchRefreshRequest(BaseModel):
    ids: Optional[List[str]] = None


class ConfigUpdateRequest(BaseModel):
    api_key: Optional[str] = None
    proxy: Optional[str] = None
    use_proxy: Optional[bool] = None
    generate_timeout: Optional[int] = None
    refresh_interval_hours: Optional[int] = None
    retry_enabled: Optional[bool] = None
    retry_max_attempts: Optional[int] = None
    retry_backoff_seconds: Optional[float] = None
    retry_on_status_codes: Optional[List[int]] = None
    retry_on_error_types: Optional[List[str]] = None
    token_rotation_strategy: Optional[str] = None


class RefreshProfileImportRequest(BaseModel):
    bundle: dict
    name: Optional[str] = None


class RefreshProfileBatchImportItem(BaseModel):
    bundle: dict
    name: Optional[str] = None


class RefreshProfileBatchImportRequest(BaseModel):
    items: List[RefreshProfileBatchImportItem]


class RefreshCookieImportRequest(BaseModel):
    cookie: Any
    name: Optional[str] = None


class RefreshCookieBatchImportItem(BaseModel):
    cookie: Any
    name: Optional[str] = None


class RefreshCookieBatchImportRequest(BaseModel):
    items: List[RefreshCookieBatchImportItem]


class RefreshProfileEnabledRequest(BaseModel):
    enabled: bool


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
    negative_prompt = str(
        data.get("negative_prompt") or data.get("negativePrompt") or ""
    ).strip()
    return generate_audio, negative_prompt


def _run_with_token_retries(
    request: Request,
    operation_name: str,
    run_once: Callable[[str], Any],
) -> Any:
    max_attempts = client.retry_max_attempts if client.retry_enabled else 1
    max_attempts = max(1, int(max_attempts))
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        token = token_manager.get_available(strategy=client.token_rotation_strategy)
        if not token:
            break
        token_meta = _set_request_token_context(request, token, attempt)
        attempt_started = time.time()

        try:
            result = run_once(token)
            _append_attempt_log(
                request=request,
                operation=operation_name,
                token_meta=token_meta,
                attempt=attempt,
                attempt_started=attempt_started,
                status_code=200,
            )
            return result
        except QuotaExhaustedError as exc:
            token_manager.report_exhausted(token)
            last_exc = exc
            retryable = attempt < max_attempts
            retry_reason = "quota_exhausted"
            _append_attempt_log(
                request=request,
                operation=operation_name,
                token_meta=token_meta,
                attempt=attempt,
                attempt_started=attempt_started,
                status_code=429,
                error=str(exc),
            )
        except AuthError as exc:
            token_manager.report_invalid(token)
            last_exc = exc
            retryable = attempt < max_attempts
            retry_reason = "auth"
            _append_attempt_log(
                request=request,
                operation=operation_name,
                token_meta=token_meta,
                attempt=attempt,
                attempt_started=attempt_started,
                status_code=401,
                error=str(exc),
            )
        except UpstreamTemporaryError as exc:
            last_exc = exc
            retryable = attempt < max_attempts and client.should_retry_temporary_error(
                exc
            )
            status_part = f"status={exc.status_code}" if exc.status_code else "status=?"
            type_part = f"type={exc.error_type or 'temporary'}"
            retry_reason = f"upstream_temporary {status_part} {type_part}"
            _append_attempt_log(
                request=request,
                operation=operation_name,
                token_meta=token_meta,
                attempt=attempt,
                attempt_started=attempt_started,
                status_code=int(exc.status_code or 503),
                error=str(exc),
            )
        except HTTPException as exc:
            _append_attempt_log(
                request=request,
                operation=operation_name,
                token_meta=token_meta,
                attempt=attempt,
                attempt_started=attempt_started,
                status_code=int(exc.status_code or 500),
                error=str(exc.detail),
            )
            raise
        except Exception:
            _append_attempt_log(
                request=request,
                operation=operation_name,
                token_meta=token_meta,
                attempt=attempt,
                attempt_started=attempt_started,
                status_code=500,
                error="Unhandled runtime error",
            )
            raise

        if retryable:
            delay = client._retry_delay_for_attempt(attempt)
            logger.warning(
                "retrying operation=%s attempt=%s/%s reason=%s delay=%.2fs strategy=%s",
                operation_name,
                attempt,
                max_attempts,
                retry_reason,
                delay,
                client.token_rotation_strategy,
            )
            _set_request_task_progress(
                request,
                task_status="IN_PROGRESS",
                error=f"retry {attempt}/{max_attempts}: {retry_reason}",
            )
            if delay > 0:
                time.sleep(delay)
            continue
        break

    if last_exc is not None:
        raise last_exc
    raise HTTPException(
        status_code=503, detail="No active tokens available in the pool"
    )


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
                raise HTTPException(
                    status_code=400,
                    detail="Only http/https or data URL images are supported",
                )
            resp = requests.get(image_url, timeout=30)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to fetch image_url: {resp.status_code}",
                )
            image_bytes = resp.content
            mime_type = (resp.headers.get("content-type") or "image/jpeg").split(";")[
                0
            ].strip() or "image/jpeg"

        if not image_bytes:
            raise HTTPException(status_code=400, detail="image_url is empty")
        if len(image_bytes) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="image too large, max 10MB")

        loaded.append((image_bytes, _normalize_image_mime(mime_type)))

    return loaded


def _prepare_video_source_image(
    image_bytes: bytes, aspect_ratio: str, resolution: str = "720p"
) -> tuple[bytes, str]:
    if not image_bytes:
        raise HTTPException(status_code=400, detail="image_url is empty")
    if Image is None:
        return image_bytes, "image/jpeg"

    res = str(resolution or "720p").lower()
    if res == "1080p":
        target_size = (1920, 1080) if aspect_ratio == "16:9" else (1080, 1920)
    else:
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
            cropped = resized.crop(
                (left, top, left + target_size[0], top + target_size[1])
            )

            out = io.BytesIO()
            cropped.save(out, format="PNG")
            return out.getvalue(), "image/png"
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid image for video: {exc}")


def _resolve_ratio_and_resolution(
    data: dict, model_id: Optional[str]
) -> tuple[str, str, str]:
    ratio = str(data.get("aspect_ratio") or "").strip() or _ratio_from_size(
        data.get("size", "1024x1024")
    )
    if ratio not in SUPPORTED_RATIOS:
        ratio = "1:1"

    resolved_model_id = model_id or DEFAULT_MODEL_ID
    if resolved_model_id not in MODEL_CATALOG:
        resolved_model_id = DEFAULT_MODEL_ID
    model_conf = MODEL_CATALOG[resolved_model_id]

    output_resolution = model_conf["output_resolution"]
    if not model_id:
        quality = str(data.get("quality", "2k")).lower()
        if quality in ("4k", "ultra"):
            output_resolution = "4K"
        elif quality in ("hd", "2k"):
            output_resolution = "2K"
        else:
            output_resolution = "1K"

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
    return _public_generated_url(request, f"{job_id}.png")


def _public_generated_url(request: Request, filename: str) -> str:
    safe_name = str(filename or "").lstrip("/")
    path = f"/generated/{safe_name}"

    override = str(
        os.getenv("ADOBE_PUBLIC_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or ""
    ).strip()
    if override:
        return f"{override.rstrip('/')}{path}"

    forwarded_host = str(request.headers.get("x-forwarded-host") or "").strip()
    if forwarded_host:
        forwarded_proto = str(
            request.headers.get("x-forwarded-proto") or "http"
        ).strip()
        return f"{forwarded_proto}://{forwarded_host}{path}"

    # Prefer relative URLs so Docker/Reverse Proxy deployments remain reachable
    # even when request host resolution is internal.
    return path


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
def list_logs(limit: int = 20, page: int = 1):
    logs, total = log_store.list(limit=limit, page=page)
    safe_limit = min(max(int(limit or 20), 1), 100)
    safe_page = max(int(page or 1), 1)
    total_pages = (total + safe_limit - 1) // safe_limit if total > 0 else 1
    if safe_page > total_pages:
        safe_page = total_pages
    return {
        "logs": logs,
        "page": safe_page,
        "limit": safe_limit,
        "total": total,
        "total_pages": total_pages,
    }


def _resolve_logs_stats_range(range_key: str) -> tuple[str, float, float]:
    now_dt = datetime.now()
    now_ts = time.time()
    key = str(range_key or "today").strip().lower()
    if key == "today":
        start_dt = datetime(now_dt.year, now_dt.month, now_dt.day)
    elif key == "7d":
        start_dt = now_dt - timedelta(days=7)
    elif key == "30d":
        start_dt = now_dt - timedelta(days=30)
    else:
        raise HTTPException(
            status_code=400, detail="range must be one of: today, 7d, 30d"
        )
    return key, start_dt.timestamp(), now_ts


@app.get("/api/v1/logs/stats")
def logs_stats(range: str = "today"):
    range_key, start_ts, end_ts = _resolve_logs_stats_range(range)
    payload = log_store.stats(start_ts=start_ts, end_ts=end_ts)
    payload.update({"range": range_key, "start_ts": start_ts, "end_ts": end_ts})
    return payload


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
    tokens = token_manager.list_all()
    for item in tokens:
        if not bool(item.get("auto_refresh")):
            item["auto_refresh_enabled"] = None
            continue
        pid = str(item.get("refresh_profile_id") or "").strip()
        item["auto_refresh_enabled"] = refresh_manager.is_profile_enabled(pid)
    return {"tokens": tokens}


@app.post("/api/v1/tokens")
def add_token(req: TokenAddRequest):
    if not req.token.strip():
        raise HTTPException(status_code=400, detail="Empty token")
    token_manager.add(req.token)
    return {"status": "ok"}


@app.post("/api/v1/tokens/batch")
def add_tokens_batch(req: TokenBatchAddRequest):
    if not req.tokens:
        raise HTTPException(status_code=400, detail="tokens is required")

    added_count = 0
    for raw in req.tokens:
        token = str(raw or "").strip()
        if not token:
            continue
        token_manager.add(token)
        added_count += 1

    if added_count == 0:
        raise HTTPException(status_code=400, detail="no valid token provided")

    return {"status": "ok", "added_count": added_count}


@app.post("/api/v1/tokens/export")
def export_tokens(req: ExportSelectionRequest):
    token_ids = req.ids if isinstance(req.ids, list) else None
    exported = token_manager.export_tokens(token_ids)
    return {
        "status": "ok",
        "total": len(exported),
        "selected": bool(token_ids),
        "tokens": exported,
    }


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
        raise HTTPException(
            status_code=400,
            detail="exhausted/invalid token cannot be reactivated; replace with a fresh token",
        )
    token_manager.set_status(tid, status)
    return {"status": "ok"}


@app.post("/api/v1/tokens/{tid}/refresh")
def refresh_token_now(tid: str):
    token_info = token_manager.get_by_id(tid)
    if not token_info:
        raise HTTPException(status_code=404, detail="token not found")

    profile_id = str(token_info.get("refresh_profile_id") or "").strip()
    if not profile_id:
        raise HTTPException(
            status_code=400,
            detail="this token is not bound to an auto refresh profile",
        )

    try:
        result = refresh_manager.refresh_once(profile_id)
        return {"status": "ok", "result": result}
    except KeyError:
        raise HTTPException(status_code=404, detail="refresh profile not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/api/v1/tokens/{tid}/auto-refresh")
def set_token_auto_refresh_enabled(tid: str, enabled: bool):
    token_info = token_manager.get_by_id(tid)
    if not token_info:
        raise HTTPException(status_code=404, detail="token not found")

    profile_id = str(token_info.get("refresh_profile_id") or "").strip()
    if not profile_id:
        raise HTTPException(
            status_code=400,
            detail="this token is not bound to an auto refresh profile",
        )
    try:
        profile = refresh_manager.set_enabled(profile_id, bool(enabled))
        return {"status": "ok", "profile": profile}
    except KeyError:
        raise HTTPException(status_code=404, detail="refresh profile not found")


@app.post("/api/v1/tokens/{tid}/credits/refresh")
def refresh_token_credits(tid: str):
    token_info = token_manager.get_by_id(tid)
    if not token_info:
        raise HTTPException(status_code=404, detail="token not found")
    try:
        result = refresh_manager.refresh_credits_for_token_id(tid)
        return {"status": "ok", **result}
    except KeyError:
        raise HTTPException(status_code=404, detail="token not found")
    except Exception as exc:
        token_manager.set_credits_error(tid, str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/v1/tokens/credits/refresh-batch")
def refresh_tokens_credits_batch(req: TokenCreditsBatchRefreshRequest):
    ids = req.ids if isinstance(req.ids, list) else None
    token_ids: List[str] = []
    if ids:
        token_ids = [str(x or "").strip() for x in ids if str(x or "").strip()]
    else:
        token_ids = token_manager.list_active_ids()

    if not token_ids:
        raise HTTPException(status_code=400, detail="no token to refresh")

    refreshed = []
    failed = []
    for tid in token_ids:
        try:
            refreshed.append(refresh_manager.refresh_credits_for_token_id(tid))
        except Exception as exc:
            token_manager.set_credits_error(tid, str(exc))
            failed.append({"token_id": tid, "detail": str(exc)})

    return {
        "status": "ok" if not failed else "partial",
        "total": len(token_ids),
        "refreshed_count": len(refreshed),
        "failed_count": len(failed),
        "refreshed": refreshed,
        "failed": failed,
    }


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
    if "refresh_interval_hours" in incoming:
        try:
            interval_hours = int(incoming["refresh_interval_hours"])
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="refresh_interval_hours must be an integer between 1 and 24",
            )
        if interval_hours < 1 or interval_hours > 24:
            raise HTTPException(
                status_code=400,
                detail="refresh_interval_hours must be between 1 and 24",
            )
        update_data["refresh_interval_hours"] = interval_hours
    if "retry_enabled" in incoming:
        update_data["retry_enabled"] = bool(incoming["retry_enabled"])
    if "retry_max_attempts" in incoming:
        try:
            retry_max_attempts = int(incoming["retry_max_attempts"])
        except Exception:
            raise HTTPException(
                status_code=400, detail="retry_max_attempts must be an integer"
            )
        if retry_max_attempts < 1 or retry_max_attempts > 10:
            raise HTTPException(
                status_code=400,
                detail="retry_max_attempts must be between 1 and 10",
            )
        update_data["retry_max_attempts"] = retry_max_attempts
    if "retry_backoff_seconds" in incoming:
        try:
            retry_backoff_seconds = float(incoming["retry_backoff_seconds"])
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="retry_backoff_seconds must be a number",
            )
        if retry_backoff_seconds < 0 or retry_backoff_seconds > 30:
            raise HTTPException(
                status_code=400,
                detail="retry_backoff_seconds must be between 0 and 30",
            )
        update_data["retry_backoff_seconds"] = retry_backoff_seconds
    if "retry_on_status_codes" in incoming:
        raw_codes = incoming["retry_on_status_codes"] or []
        if not isinstance(raw_codes, list):
            raise HTTPException(
                status_code=400, detail="retry_on_status_codes must be a list"
            )
        status_codes: list[int] = []
        for item in raw_codes:
            try:
                code = int(item)
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="retry_on_status_codes contains invalid value",
                )
            if code < 100 or code > 599:
                raise HTTPException(
                    status_code=400,
                    detail="retry_on_status_codes must be HTTP status codes",
                )
            status_codes.append(code)
        update_data["retry_on_status_codes"] = sorted(set(status_codes))
    if "retry_on_error_types" in incoming:
        raw_types = incoming["retry_on_error_types"] or []
        if not isinstance(raw_types, list):
            raise HTTPException(
                status_code=400, detail="retry_on_error_types must be a list"
            )
        error_types: list[str] = []
        for item in raw_types:
            txt = str(item or "").strip().lower()
            if txt:
                error_types.append(txt)
        update_data["retry_on_error_types"] = sorted(set(error_types))
    if "token_rotation_strategy" in incoming:
        strategy = str(incoming["token_rotation_strategy"] or "").strip().lower()
        if strategy not in {"round_robin", "random"}:
            raise HTTPException(
                status_code=400,
                detail="token_rotation_strategy must be one of: round_robin, random",
            )
        update_data["token_rotation_strategy"] = strategy
    config_manager.update_all(update_data)
    client.apply_config(config_manager.get_all())
    return config_manager.get_all()


@app.get("/api/v1/refresh-profiles")
def refresh_profiles_list():
    return {"profiles": refresh_manager.list_profiles()}


@app.post("/api/v1/refresh-profiles/export")
def refresh_profiles_export(req: ExportSelectionRequest):
    profile_ids = req.ids if isinstance(req.ids, list) else None
    exported = refresh_manager.export_bundles(profile_ids)
    return {
        "status": "ok",
        "total": len(exported),
        "selected": bool(profile_ids),
        "items": exported,
    }


@app.post("/api/v1/refresh-profiles/export-cookies")
def refresh_profiles_export_cookies(req: ExportSelectionRequest):
    profile_ids = req.ids if isinstance(req.ids, list) else None
    exported = refresh_manager.export_cookies(profile_ids)
    return {
        "status": "ok",
        "total": len(exported),
        "selected": bool(profile_ids),
        "items": exported,
    }


@app.post("/api/v1/refresh-profiles/import")
def refresh_profiles_import(req: RefreshProfileImportRequest):
    try:
        profile = refresh_manager.import_bundle(req.bundle, name=req.name)
        refresh_result = None
        refresh_error = ""
        try:
            refresh_result = refresh_manager.refresh_once(str(profile.get("id") or ""))
        except Exception as exc:
            refresh_error = str(exc)
        return {
            "status": "ok" if not refresh_error else "partial",
            "profile": profile,
            "refresh_result": refresh_result,
            "refresh_error": refresh_error,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/refresh-profiles/import-cookie")
def refresh_profiles_import_cookie(req: RefreshCookieImportRequest):
    try:
        profile = refresh_manager.import_cookie(req.cookie, name=req.name)
        refresh_result = None
        refresh_error = ""
        try:
            refresh_result = refresh_manager.refresh_once(str(profile.get("id") or ""))
        except Exception as exc:
            refresh_error = str(exc)
        return {
            "status": "ok" if not refresh_error else "partial",
            "profile": profile,
            "refresh_result": refresh_result,
            "refresh_error": refresh_error,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/refresh-profiles/import-batch")
def refresh_profiles_import_batch(req: RefreshProfileBatchImportRequest):
    if not req.items:
        raise HTTPException(status_code=400, detail="items is required")

    imported = []
    failed = []
    refreshed = []
    refresh_failed = []
    for idx, item in enumerate(req.items):
        try:
            profile = refresh_manager.import_bundle(item.bundle, name=item.name)
            imported.append(profile)
            try:
                refresh_result = refresh_manager.refresh_once(
                    str(profile.get("id") or "")
                )
                refreshed.append(
                    {
                        "index": idx,
                        "profile_id": profile.get("id"),
                        "profile_name": profile.get("name"),
                        "result": refresh_result,
                    }
                )
            except Exception as exc:
                refresh_failed.append(
                    {
                        "index": idx,
                        "profile_id": profile.get("id"),
                        "profile_name": profile.get("name"),
                        "detail": str(exc),
                    }
                )
        except ValueError as exc:
            failed.append(
                {
                    "index": idx,
                    "name": item.name,
                    "detail": str(exc),
                }
            )

    result = {
        "status": (
            "ok"
            if (not failed and not refresh_failed)
            else ("partial" if imported else "failed")
        ),
        "total": len(req.items),
        "imported_count": len(imported),
        "failed_count": len(failed),
        "refreshed_count": len(refreshed),
        "refresh_failed_count": len(refresh_failed),
        "profiles": imported,
        "failed": failed,
        "refreshed": refreshed,
        "refresh_failed": refresh_failed,
    }
    if not imported:
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/api/v1/refresh-profiles/import-cookie-batch")
def refresh_profiles_import_cookie_batch(req: RefreshCookieBatchImportRequest):
    if not req.items:
        raise HTTPException(status_code=400, detail="items is required")

    imported = []
    failed = []
    refreshed = []
    refresh_failed = []
    for idx, item in enumerate(req.items):
        try:
            profile = refresh_manager.import_cookie(item.cookie, name=item.name)
            imported.append(profile)
            try:
                refresh_result = refresh_manager.refresh_once(
                    str(profile.get("id") or "")
                )
                refreshed.append(
                    {
                        "index": idx,
                        "profile_id": profile.get("id"),
                        "profile_name": profile.get("name"),
                        "result": refresh_result,
                    }
                )
            except Exception as exc:
                refresh_failed.append(
                    {
                        "index": idx,
                        "profile_id": profile.get("id"),
                        "profile_name": profile.get("name"),
                        "detail": str(exc),
                    }
                )
        except ValueError as exc:
            failed.append(
                {
                    "index": idx,
                    "name": item.name,
                    "detail": str(exc),
                }
            )

    result = {
        "status": (
            "ok"
            if (not failed and not refresh_failed)
            else ("partial" if imported else "failed")
        ),
        "total": len(req.items),
        "imported_count": len(imported),
        "failed_count": len(failed),
        "refreshed_count": len(refreshed),
        "refresh_failed_count": len(refresh_failed),
        "profiles": imported,
        "failed": failed,
        "refreshed": refreshed,
        "refresh_failed": refresh_failed,
    }
    if not imported:
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/api/v1/refresh-profiles/{profile_id}/refresh-now")
def refresh_profiles_refresh_now(profile_id: str):
    try:
        return refresh_manager.refresh_once(profile_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="profile not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/api/v1/refresh-profiles/{profile_id}/enabled")
def refresh_profiles_set_enabled(profile_id: str, req: RefreshProfileEnabledRequest):
    try:
        profile = refresh_manager.set_enabled(profile_id, req.enabled)
        return {"status": "ok", "profile": profile}
    except KeyError:
        raise HTTPException(status_code=404, detail="profile not found")


@app.delete("/api/v1/refresh-profiles/{profile_id}")
def refresh_profiles_delete(profile_id: str):
    try:
        refresh_manager.remove_profile(profile_id)
        return {"status": "ok"}
    except KeyError:
        raise HTTPException(status_code=404, detail="profile not found")


# --- Generation API (OpenAI Compatible structure) ---


@app.post("/v1/images/generations")
def openai_generate(data: dict, request: Request):
    """
    Minimal OpenAI compatible endpoint for image generation
    """
    _require_service_api_key(request)

    prompt = data.get("prompt", "").strip()
    if not prompt:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "prompt is required",
                    "type": "invalid_request_error",
                }
            },
        )

    model_id = data.get("model")
    if str(model_id or "").strip() in VIDEO_MODEL_CATALOG:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "Use /v1/chat/completions for video generation",
                    "type": "invalid_request_error",
                }
            },
        )
    ratio, output_resolution, resolved_model_id = _resolve_ratio_and_resolution(
        data, model_id
    )

    try:
        _set_request_task_progress(
            request, task_status="IN_PROGRESS", task_progress=0.0
        )

        def _run_once(token: str):
            def _image_progress_cb(update: dict):
                _set_request_task_progress(
                    request,
                    task_status=str(update.get("task_status") or "IN_PROGRESS"),
                    task_progress=update.get("task_progress"),
                    upstream_job_id=update.get("upstream_job_id"),
                    retry_after=update.get("retry_after"),
                    error=update.get("error"),
                )

            image_bytes, _meta = client.generate(
                token=token,
                prompt=prompt,
                aspect_ratio=ratio,
                output_resolution=output_resolution,
                timeout=client.generate_timeout,
                progress_cb=_image_progress_cb,
            )

            job_id = uuid.uuid4().hex
            out_path = GENERATED_DIR / f"{job_id}.png"
            out_path.write_bytes(image_bytes)
            image_url = _public_image_url(request, job_id)
            _set_request_preview(request, image_url, kind="image")
            return {
                "created": int(time.time()),
                "model": resolved_model_id,
                "data": [{"url": image_url}],
            }

        return _run_with_token_retries(
            request=request,
            operation_name="images.generations",
            run_once=_run_once,
        )

    except QuotaExhaustedError:
        _set_request_task_progress(
            request,
            task_status="FAILED",
            task_progress=0.0,
            error="Token quota exhausted",
        )
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "message": "Token quota exhausted",
                    "type": "rate_limit_error",
                }
            },
        )
    except AuthError:
        _set_request_task_progress(
            request,
            task_status="FAILED",
            task_progress=0.0,
            error="Token invalid or expired",
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": "Token invalid or expired",
                    "type": "authentication_error",
                }
            },
        )
    except UpstreamTemporaryError as exc:
        _set_request_task_progress(
            request, task_status="FAILED", task_progress=0.0, error=str(exc)
        )
        return JSONResponse(
            status_code=503,
            content={"error": {"message": str(exc), "type": "server_error"}},
        )
    except HTTPException as exc:
        _set_request_task_progress(
            request, task_status="FAILED", task_progress=0.0, error=str(exc.detail)
        )
        err_type = (
            "invalid_request_error"
            if 400 <= int(exc.status_code) < 500
            else "server_error"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail), "type": err_type}},
        )
    except Exception as exc:
        logger.exception(
            "Unhandled error in /v1/images/generations log_id=%s model=%s",
            getattr(request.state, "log_id", ""),
            resolved_model_id,
        )
        _set_request_task_progress(
            request, task_status="FAILED", task_progress=0.0, error=str(exc)
        )
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(exc), "type": "server_error"}},
        )


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
    if output_resolution not in {"1K", "2K", "4K"}:
        raise HTTPException(status_code=400, detail="unsupported output_resolution")

    # If model is provided, use model suffix mapping as source of truth.
    if data.model:
        model_conf = _resolve_model(data.model)
        output_resolution = model_conf["output_resolution"]

    job = store.create(prompt=prompt, aspect_ratio=ratio)

    def runner(job_id: str):
        store.update(job_id, status="running", progress=5.0)
        max_attempts = client.retry_max_attempts if client.retry_enabled else 1
        max_attempts = max(1, int(max_attempts))
        last_error = "No active tokens available in the pool"

        for attempt in range(1, max_attempts + 1):
            token = token_manager.get_available(strategy=client.token_rotation_strategy)
            if not token:
                break

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
                image_url = _public_image_url(request, job_id)
                store.update(
                    job_id,
                    status="succeeded",
                    progress=max(progress, 100.0),
                    image_url=image_url,
                )
                return
            except QuotaExhaustedError:
                token_manager.report_exhausted(token)
                last_error = "Token quota exhausted."
                retryable = attempt < max_attempts
            except AuthError:
                token_manager.report_invalid(token)
                last_error = "Token invalid or expired."
                retryable = attempt < max_attempts
            except UpstreamTemporaryError as exc:
                last_error = str(exc)
                retryable = (
                    attempt < max_attempts and client.should_retry_temporary_error(exc)
                )
            except Exception as exc:
                store.update(job_id, status="failed", error=str(exc))
                return

            if retryable:
                delay = client._retry_delay_for_attempt(attempt)
                if delay > 0:
                    time.sleep(delay)
                continue
            break

        store.update(job_id, status="failed", error=last_error)

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
            content={
                "error": {
                    "message": "messages or prompt is required",
                    "type": "invalid_request_error",
                }
            },
        )

    model_id = str(data.get("model") or "").strip()
    if (
        model_id.startswith("firefly-sora2")
        or model_id.startswith("firefly-veo31-fast")
    ) and model_id not in VIDEO_MODEL_CATALOG:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "Invalid video model. Use /v1/models to get supported firefly-sora2-* or firefly-veo31-fast-* models",
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
    video_resolution = (
        str(video_conf.get("resolution") or "720p") if video_conf else "720p"
    )
    if video_conf:
        ratio = str(video_conf.get("aspect_ratio") or ratio)
    video_engine = str(video_conf.get("engine") or "sora2") if video_conf else ""
    generate_audio = True
    negative_prompt = ""
    if is_video_model:
        generate_audio, negative_prompt = _resolve_video_options(data)
    else:
        ratio, output_resolution, resolved_model_id = _resolve_ratio_and_resolution(
            data, model_id or None
        )

    try:
        input_images = _load_input_images(data.get("messages") or [])
        _set_request_task_progress(
            request, task_status="IN_PROGRESS", task_progress=0.0
        )

        def _run_once(token: str):
            source_image_ids: list[str] = []
            image_url = ""
            response_label = "generated image"

            if is_video_model:
                max_video_inputs = 2 if video_engine == "veo31-fast" else 1
                if len(input_images) > max_video_inputs:
                    raise HTTPException(
                        status_code=400,
                        detail=f"video model supports at most {max_video_inputs} input image(s)",
                    )
                for image_bytes, _image_mime in input_images[:max_video_inputs]:
                    prepared_bytes, prepared_mime = _prepare_video_source_image(
                        image_bytes,
                        ratio,
                        video_resolution,
                    )
                    source_image_ids.append(
                        client.upload_image(token, prepared_bytes, prepared_mime)
                    )

                def _video_progress_cb(update: dict):
                    _set_request_task_progress(
                        request,
                        task_status=str(update.get("task_status") or "IN_PROGRESS"),
                        task_progress=update.get("task_progress"),
                        upstream_job_id=update.get("upstream_job_id"),
                        retry_after=update.get("retry_after"),
                        error=update.get("error"),
                    )

                video_bytes, video_meta = client.generate_video(
                    token=token,
                    video_conf=video_conf or {},
                    prompt=prompt,
                    aspect_ratio=ratio,
                    duration=duration,
                    source_image_ids=source_image_ids,
                    timeout=max(int(client.generate_timeout), 600),
                    negative_prompt=negative_prompt,
                    generate_audio=generate_audio,
                    progress_cb=_video_progress_cb,
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
                    source_image_ids.append(
                        client.upload_image(
                            token, image_bytes, image_mime or "image/jpeg"
                        )
                    )

                def _image_progress_cb(update: dict):
                    _set_request_task_progress(
                        request,
                        task_status=str(update.get("task_status") or "IN_PROGRESS"),
                        task_progress=update.get("task_progress"),
                        upstream_job_id=update.get("upstream_job_id"),
                        retry_after=update.get("retry_after"),
                        error=update.get("error"),
                    )

                image_bytes, _meta = client.generate(
                    token=token,
                    prompt=prompt,
                    aspect_ratio=ratio,
                    output_resolution=output_resolution,
                    source_image_ids=source_image_ids,
                    timeout=client.generate_timeout,
                    progress_cb=_image_progress_cb,
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
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
            if bool(data.get("stream", False)):
                return StreamingResponse(
                    _sse_chat_stream(response_payload), media_type="text/event-stream"
                )
            return response_payload

        return _run_with_token_retries(
            request=request,
            operation_name="chat.completions",
            run_once=_run_once,
        )
    except QuotaExhaustedError:
        _set_request_task_progress(
            request,
            task_status="FAILED",
            task_progress=0.0,
            error="Token quota exhausted",
        )
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "message": "Token quota exhausted",
                    "type": "rate_limit_error",
                }
            },
        )
    except AuthError:
        _set_request_task_progress(
            request,
            task_status="FAILED",
            task_progress=0.0,
            error="Token invalid or expired",
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": "Token invalid or expired",
                    "type": "authentication_error",
                }
            },
        )
    except UpstreamTemporaryError as exc:
        _set_request_task_progress(
            request, task_status="FAILED", task_progress=0.0, error=str(exc)
        )
        return JSONResponse(
            status_code=503,
            content={"error": {"message": str(exc), "type": "server_error"}},
        )
    except HTTPException as exc:
        _set_request_task_progress(
            request, task_status="FAILED", task_progress=0.0, error=str(exc.detail)
        )
        err_type = (
            "invalid_request_error"
            if 400 <= int(exc.status_code) < 500
            else "server_error"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail), "type": err_type}},
        )
    except Exception as exc:
        logger.exception(
            "Unhandled error in /v1/chat/completions log_id=%s model=%s resolved_model=%s is_video_model=%s",
            getattr(request.state, "log_id", ""),
            model_id,
            resolved_model_id,
            is_video_model,
        )
        _set_request_task_progress(
            request, task_status="FAILED", task_progress=0.0, error=str(exc)
        )
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(exc), "type": "server_error"}},
        )


if __name__ == "__main__":
    import uvicorn

    # 为了在容器中更好工作，使用环境变量
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "6001")))
