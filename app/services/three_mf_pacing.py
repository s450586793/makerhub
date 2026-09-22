from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import time
from typing import Any

from app.core.timezone import now as china_now
from app.services import account_health
from app.services.three_mf import describe_three_mf_failure, is_three_mf_daily_download_limited


logger = logging.getLogger(__name__)


def _finite_seconds(value: Any) -> float:
    try:
        number = float(value)
        return max(number, 0.0) if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _retry_after_seconds(value: Any, now: float) -> float:
    raw = str(value or "").strip()
    try:
        return _finite_seconds(float(raw))
    except ValueError:
        try:
            date = parsedate_to_datetime(raw)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(date.timestamp() - now, 0.0)
        except (TypeError, ValueError, OverflowError):
            return 0.0


def _authorization_interval() -> float:
    # 与现有环境变量保持兼容，延迟在拿到浏览器跨进程锁之后计算。
    from app.services.legacy_archiver import _three_mf_download_wait_seconds

    return _finite_seconds(_three_mf_download_wait_seconds())


class ThreeMfAuthorizationAttempt:
    """调用方必须持有浏览器操作锁，覆盖读取、等待、请求和结果保存。"""

    def __init__(self, platform: str, profile_id: str, state_dir: Path, *, model_url: str = "", instance_id: str = ""):
        self.platform = platform
        digest = hashlib.sha256(f"{platform}:{profile_id}".encode()).hexdigest()[:24]
        self.path = state_dir / "three_mf_pacing" / f"{digest}.json"
        self.model_url = model_url
        self.instance_id = instance_id
        self.state: dict[str, Any] = {}
        self.response: dict[str, Any] | None = None
        self.request_started = False

    def _blocked_response(self) -> dict[str, Any] | None:
        snapshot = account_health.get_account_health(self.platform)
        gate = snapshot.get("three_mf_gate")
        if gate == "verification_required":
            return {"status_code": 418, "payload": {"message": "verification required"}}
        if gate == "daily_limit" and str(snapshot.get("updated_at") or "").startswith(china_now().date().isoformat()):
            return {"status_code": 429, "payload": {"message": "Daily download limit reached"}}
        return None

    def __enter__(self):
        self.response = self._blocked_response()
        if self.response is not None:
            return self
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            self.state = stored if isinstance(stored, dict) else {}
        except (FileNotFoundError, ValueError):
            self.state = {}
        now = time.time()
        remaining = _finite_seconds(self.state.get("cooldown_until")) - now
        if remaining > 0:
            self.response = {
                "status_code": 429,
                "payload": {"message": "Too Many Requests"},
                "headers": {"retry-after": str(math.ceil(remaining))},
            }
            return self
        wait = min(max(_finite_seconds(self.state.get("next_allowed_at")) - now, 0.0), 120.0)
        if not self.state:
            wait = _authorization_interval()
        if wait > 0:
            logger.info("3MF authorization pacing: platform=%s wait_seconds=%.2f", self.platform, wait)
            time.sleep(wait)
        self.response = self._blocked_response()
        self.request_started = self.response is None
        return self

    def __exit__(self, exc_type, exc, traceback):
        if not self.request_started:
            return False
        from app.services.legacy_archiver import _extract_instance_download

        result = self.response or {}
        status = int(_finite_seconds(result.get("status_code")))
        payload = result.get("payload")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(result.get("text") or "{}")
            except (TypeError, ValueError):
                payload = {}
        payload = payload if isinstance(payload, dict) else {}
        _name, signed_url = _extract_instance_download(payload)
        authorized = 200 <= status < 300 and bool(signed_url)
        detail = json.dumps(payload, ensure_ascii=False) + " " + str(result.get("text") or "")
        daily_limit = is_three_mf_daily_download_limited(detail)
        now = time.time()
        count = min(int(_finite_seconds(self.state.get("rate_limit_count"))), 6)
        cooldown = 0.0
        if status == 429 and not daily_limit:
            count = min(count + 1, 6)
            headers = result.get("headers") if isinstance(result.get("headers"), dict) else {}
            retry_after = next((value for key, value in headers.items() if str(key).lower() == "retry-after"), "")
            cooldown = max(min(30.0 * 2 ** (count - 1), 900.0), _retry_after_seconds(retry_after, now))
            logger.info("3MF authorization cooldown: platform=%s seconds=%.2f", self.platform, cooldown)
        elif authorized:
            count = 0
        state = {
            "next_allowed_at": now + _authorization_interval(),
            "cooldown_until": now + cooldown if cooldown else 0.0,
            "rate_limit_count": count,
        }
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(state), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            # 请求已经完成，保存节奏失败不能丢弃取得的签名地址并触发重复授权。
            logger.warning("Could not persist 3MF pacing state: platform=%s", self.platform)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        # 只同步授权接口的明确验证/额度响应，普通网络错误不关闭账号。
        if not authorized and (daily_limit or status == 418 or payload.get("captchaId")):
            failure = "download_limited" if daily_limit else "verification_required"
            try:
                account_health.update_three_mf_gate(
                    self.platform,
                    gate=failure,
                    reason="three_mf_authorization_failed",
                    source="three_mf_authorization",
                    detail=describe_three_mf_failure(failure, source=self.platform),
                    model_url=self.model_url,
                    instance_id=self.instance_id,
                )
            except Exception:
                logger.warning("Could not update 3MF authorization gate: platform=%s", self.platform)
        return False
