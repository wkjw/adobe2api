import json
import base64
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"
DATA_FILE = CONFIG_DIR / "tokens.json"
LEGACY_DATA_FILE = DATA_DIR / "tokens.json"

class TokenManager:
    ERROR_COOLDOWN_SECONDS = 180

    def __init__(self):
        self._lock = threading.Lock()
        self.tokens: List[Dict] = []
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self):
        with self._lock:
            source = DATA_FILE if DATA_FILE.exists() else LEGACY_DATA_FILE
            if source.exists():
                try:
                    self.tokens = json.loads(source.read_text(encoding="utf-8"))
                    now_ts = time.time()
                    for t in self.tokens:
                        if not isinstance(t, dict):
                            continue
                        t.setdefault("id", uuid.uuid4().hex[:8])
                        t.setdefault("value", "")
                        t.setdefault("status", "active")
                        t.setdefault("fails", 0)
                        t.setdefault("added_at", now_ts)
                        t.setdefault("error_until", 0)
                    if source == LEGACY_DATA_FILE and not DATA_FILE.exists():
                        DATA_FILE.write_text(json.dumps(self.tokens, indent=2), encoding="utf-8")
                except Exception:
                    self.tokens = []

    def save(self):
        DATA_FILE.write_text(json.dumps(self.tokens, indent=2), encoding="utf-8")

    def add(self, value: str, meta: Optional[Dict] = None):
        with self._lock:
            value = value.strip()
            if value.startswith("Bearer "):
                value = value[7:].strip()
            meta = dict(meta or {})
                
            for t in self.tokens:
                if t["value"] == value:
                    if meta:
                        t.update(meta)
                        self.save()
                    return t
            
            new_token = {
                "id": uuid.uuid4().hex[:8],
                "value": value,
                "status": "active",
                "fails": 0,
                "added_at": time.time(),
                "error_until": 0,
            }
            if meta:
                new_token.update(meta)
            self.tokens.append(new_token)
            self.save()
            return new_token

    def upsert_auto_refresh_token(self, value: str):
        with self._lock:
            value = value.strip()
            if value.startswith("Bearer "):
                value = value[7:].strip()

            # Keep only one auto-refresh token record.
            auto_entries = [t for t in self.tokens if t.get("auto_refresh") is True]
            now_ts = time.time()
            if auto_entries:
                target = auto_entries[0]
                target["value"] = value
                target["status"] = "active"
                target["fails"] = 0
                target["error_until"] = 0
                target["updated_at"] = now_ts
                for extra in auto_entries[1:]:
                    self.tokens = [t for t in self.tokens if t is not extra]
                self.save()
                return dict(target)

            new_token = {
                "id": uuid.uuid4().hex[:8],
                "value": value,
                "status": "active",
                "fails": 0,
                "added_at": now_ts,
                "updated_at": now_ts,
                "error_until": 0,
                "source": "auto_refresh",
                "auto_refresh": True,
            }
            self.tokens.append(new_token)
            self.save()
            return dict(new_token)

    def remove(self, tid: str):
        with self._lock:
            self.tokens = [t for t in self.tokens if t["id"] != tid]
            self.save()

    def get_by_id(self, tid: str) -> Optional[Dict]:
        with self._lock:
            for t in self.tokens:
                if t.get("id") == tid:
                    return dict(t)
        return None

    def set_status(self, tid: str, status: str):
        with self._lock:
            for t in self.tokens:
                if t["id"] == tid:
                    t["status"] = status
                    t["fails"] = 0 if status == "active" else t["fails"]
                    if status == "active":
                        t["error_until"] = 0
            self.save()

    def get_available(self) -> Optional[str]:
        with self._lock:
            active = [t for t in self.tokens if t["status"] == "active"]
            if active:
                active.sort(key=lambda x: x["fails"])
                return active[0]["value"]

            # Auto-revive one recoverable token after cooldown.
            now_ts = time.time()
            recoverable = [
                t
                for t in self.tokens
                if t["status"] == "error" and float(t.get("error_until", 0) or 0) <= now_ts
            ]
            if not recoverable:
                return None
            recoverable.sort(key=lambda x: x["fails"])
            chosen = recoverable[0]
            chosen["status"] = "active"
            chosen["fails"] = max(0, int(chosen.get("fails", 0)) - 1)
            chosen["error_until"] = 0
            self.save()
            return chosen["value"]

    def report_exhausted(self, value: str):
        with self._lock:
            for t in self.tokens:
                if t["value"] == value:
                    t["status"] = "exhausted"
                    t["error_until"] = 0
            self.save()

    def report_invalid(self, value: str):
        with self._lock:
            for t in self.tokens:
                if t["value"] == value:
                    t["status"] = "invalid"
                    t["error_until"] = 0
            self.save()

    def report_error(self, value: str):
        with self._lock:
            for t in self.tokens:
                if t["value"] == value:
                    t["fails"] += 1
                    t["status"] = "error"
                    t["error_until"] = time.time() + self.ERROR_COOLDOWN_SECONDS
            self.save()

    def report_success(self, value: str):
        with self._lock:
            for t in self.tokens:
                if t["value"] == value:
                    t["fails"] = max(0, int(t.get("fails", 0)) - 1)
                    if t["status"] == "error":
                        t["status"] = "active"
                        t["error_until"] = 0
            self.save()

    @staticmethod
    def _decode_jwt_payload(value: str) -> Optional[dict]:
        token = str(value or "").strip()
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        try:
            raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
            data = json.loads(raw.decode("utf-8", errors="ignore"))
            if isinstance(data, dict):
                return data
        except Exception:
            return None
        return None

    @classmethod
    def _decode_jwt_exp(cls, value: str) -> Optional[int]:
        data = cls._decode_jwt_payload(value)
        if not data:
            return None

        exp = data.get("exp")
        if isinstance(exp, (int, float)):
            return int(exp)

        # Adobe tokens often expose created_at + expires_in in payload instead of exp.
        created_at = data.get("created_at")
        expires_in = data.get("expires_in")
        try:
            created_at_val = int(str(created_at).strip())
            expires_in_val = int(str(expires_in).strip())
        except Exception:
            return None

        if created_at_val <= 0 or expires_in_val <= 0:
            return None

        # Some fields are milliseconds (e.g. 1771862511913 / 86400000)
        if created_at_val > 10_000_000_000:
            created_at_val = int(created_at_val / 1000)
        if expires_in_val > 86400 * 2:
            expires_in_val = int(expires_in_val / 1000)

        return created_at_val + expires_in_val

    def list_all(self):
        with self._lock:
            res = []
            now_ts = int(time.time())
            for t in self.tokens:
                # mask value
                val = t["value"]
                masked = val[:15] + "..." + val[-10:] if len(val) > 30 else "***"
                exp_ts = self._decode_jwt_exp(val)
                remaining_seconds = None
                exp_readable = None
                if exp_ts is not None:
                    remaining_seconds = exp_ts - now_ts
                    try:
                        exp_readable = datetime.fromtimestamp(exp_ts).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        exp_readable = str(exp_ts)
                res.append({
                    "id": t["id"],
                    "value": masked,
                    "status": t["status"],
                    "fails": t["fails"],
                    "added_at": t["added_at"],
                    "error_until": t.get("error_until", 0),
                    "source": t.get("source", "manual"),
                    "auto_refresh": bool(t.get("auto_refresh", False)),
                    "expires_at": exp_ts,
                    "expires_at_text": exp_readable,
                    "remaining_seconds": remaining_seconds,
                    "is_expired": bool(exp_ts is not None and remaining_seconds is not None and remaining_seconds <= 0),
                })
            return res

token_manager = TokenManager()
