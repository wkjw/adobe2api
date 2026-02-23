import json
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import requests

from core.config_mgr import config_manager
from core.token_mgr import token_manager


BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
PROFILE_FILE = CONFIG_DIR / "refresh_profile.json"


class RefreshManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._runner_started = False
        self._stop_event = threading.Event()
        self._profile: Dict = {}
        self._state: Dict = {
            "enabled": False,
            "has_profile": False,
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": "",
            "last_http_status": None,
            "next_retry_at": None,
            "consecutive_failures": 0,
        }
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._load_profile()

    def _load_profile(self):
        with self._lock:
            if PROFILE_FILE.exists():
                try:
                    self._profile = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
                    self._state["has_profile"] = bool(self._profile)
                    self._state["enabled"] = bool(self._profile)
                    self._state["next_retry_at"] = time.time() if self._profile else None
                except Exception:
                    self._profile = {}

    def _save_profile(self):
        PROFILE_FILE.write_text(json.dumps(self._profile, indent=2), encoding="utf-8")

    @staticmethod
    def _validate_bundle(bundle: Dict) -> Dict:
        if not isinstance(bundle, dict):
            raise ValueError("bundle must be an object")

        endpoint = bundle.get("endpoint")
        if not isinstance(endpoint, dict):
            raise ValueError("bundle.endpoint is required")

        url = str(endpoint.get("url") or "").strip()
        if not url.startswith("https://adobeid-na1.services.adobe.com/ims/check/v6/token"):
            raise ValueError("invalid endpoint url")

        form = endpoint.get("form")
        headers = endpoint.get("headers")
        if not isinstance(form, dict):
            raise ValueError("bundle.endpoint.form is required")
        if not isinstance(headers, dict):
            raise ValueError("bundle.endpoint.headers is required")

        for key in ("client_id", "scope"):
            if not str(form.get(key) or "").strip():
                raise ValueError(f"bundle form missing {key}")
        if not str(headers.get("Cookie") or "").strip():
            raise ValueError("bundle headers missing Cookie")

        normalized_headers = {
            "Accept": str(headers.get("Accept") or "*/*"),
            "Accept-Language": str(headers.get("Accept-Language") or "en-US,en;q=0.9"),
            "Content-Type": str(headers.get("Content-Type") or "application/x-www-form-urlencoded;charset=UTF-8"),
            "Cookie": str(headers.get("Cookie") or "").strip(),
            "Origin": str(headers.get("Origin") or "https://firefly.adobe.com"),
            "Referer": str(headers.get("Referer") or "https://firefly.adobe.com/"),
            "User-Agent": str(headers.get("User-Agent") or "Mozilla/5.0"),
        }

        normalized_form = {
            "client_id": str(form.get("client_id") or "").strip(),
            "guest_allowed": str(form.get("guest_allowed") or "true").strip() or "true",
            "scope": str(form.get("scope") or "").strip(),
        }

        return {
            "type": "adobe_refresh_bundle",
            "version": int(bundle.get("version") or 1),
            "imported_at": int(time.time()),
            "endpoint": {
                "url": url,
                "method": "POST",
                "form": normalized_form,
                "headers": normalized_headers,
            },
        }

    def import_bundle(self, bundle: Dict):
        normalized = self._validate_bundle(bundle)
        with self._lock:
            self._profile = normalized
            self._state["enabled"] = True
            self._state["has_profile"] = True
            self._state["last_error"] = ""
            self._state["consecutive_failures"] = 0
            self._state["next_retry_at"] = time.time()
            self._save_profile()

    def clear_bundle(self):
        with self._lock:
            self._profile = {}
            self._state = {
                "enabled": False,
                "has_profile": False,
                "last_attempt_at": None,
                "last_success_at": None,
                "last_error": "",
                "last_http_status": None,
                "next_retry_at": None,
                "consecutive_failures": 0,
            }
            if PROFILE_FILE.exists():
                PROFILE_FILE.unlink()

    def status(self) -> Dict:
        with self._lock:
            endpoint = None
            if self._profile:
                endpoint = {
                    "url": self._profile.get("endpoint", {}).get("url", ""),
                    "client_id": self._profile.get("endpoint", {}).get("form", {}).get("client_id", ""),
                }
            return {
                **self._state,
                "endpoint": endpoint,
            }

    @staticmethod
    def _parse_expires_seconds(raw_value) -> Optional[int]:
        try:
            val = int(str(raw_value or "0").strip())
        except Exception:
            return None
        if val <= 0:
            return None
        # Adobe often returns milliseconds-like value (~86400000).
        if val > 86400 * 2:
            val = int(val / 1000)
        return val if val > 0 else None

    def _requests_proxies(self):
        proxy = str(config_manager.get("proxy", "") or "").strip()
        use_proxy = bool(config_manager.get("use_proxy", False))
        if not (use_proxy and proxy):
            return None
        return {"http": proxy, "https": proxy}

    def refresh_once(self) -> Dict:
        with self._lock:
            profile = dict(self._profile)
            if not profile:
                raise ValueError("refresh profile not imported")
            endpoint = profile.get("endpoint", {})
            url = endpoint.get("url")
            headers = dict(endpoint.get("headers") or {})
            form = dict(endpoint.get("form") or {})
            self._state["last_attempt_at"] = int(time.time())

        resp = requests.post(url, headers=headers, data=form, timeout=30, proxies=self._requests_proxies())
        now_ts = int(time.time())
        with self._lock:
            self._state["last_http_status"] = int(resp.status_code)

        if resp.status_code != 200:
            raise RuntimeError(f"refresh request failed: {resp.status_code} {resp.text[:200]}")

        try:
            data = resp.json()
        except Exception:
            raise RuntimeError("refresh response is not valid json")

        token = str(data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("refresh response missing access_token")

        token_manager.upsert_auto_refresh_token(token)

        with self._lock:
            self._state["last_success_at"] = now_ts
            self._state["last_error"] = ""
            self._state["consecutive_failures"] = 0
            seconds = self._parse_expires_seconds(data.get("expires_in"))
            wait_seconds = 1800
            if seconds:
                wait_seconds = max(300, min(seconds - 600, 3600))
            self._state["next_retry_at"] = time.time() + wait_seconds

        return {
            "status": "ok",
            "expires_in": data.get("expires_in"),
            "next_retry_at": self._state.get("next_retry_at"),
        }

    def _mark_failure(self, message: str):
        with self._lock:
            fails = int(self._state.get("consecutive_failures", 0)) + 1
            self._state["consecutive_failures"] = fails
            self._state["last_error"] = message[:500]
            delays = [60, 180, 600, 1800]
            delay = delays[min(fails - 1, len(delays) - 1)]
            self._state["next_retry_at"] = time.time() + delay

    def start(self):
        with self._lock:
            if self._runner_started:
                return
            self._runner_started = True
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                st = self.status()
                if not st.get("enabled") or not st.get("has_profile"):
                    time.sleep(2.0)
                    continue
                next_retry = st.get("next_retry_at")
                if next_retry and time.time() < float(next_retry):
                    time.sleep(2.0)
                    continue
                self.refresh_once()
            except Exception as exc:
                self._mark_failure(str(exc))
            time.sleep(1.0)


refresh_manager = RefreshManager()
