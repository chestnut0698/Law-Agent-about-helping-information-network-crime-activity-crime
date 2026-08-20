"""统一模型网关：所有 LLM 调用的唯一出口。

职责（按注释分段）：错误码与审计约束、建表、幂等复用、真实/Fake 调用、
超时与 429 重试、修复队列、降级状态。禁止在此文件外创建 OpenAI 客户端。
model_runs 只记用途、版本、哈希、用量与状态，不写原文。
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

from app.config import (
    API_KEY,
    BASE_URL,
    DATABASE_PATH,
    DEEPSEEK_EXTERNAL_CALLS_ENABLED,
    GATEWAY_FAKE_MODE,
    GATEWAY_MAX_RETRIES,
    GATEWAY_MODEL_WHITELIST,
    GATEWAY_RETRY_BASE_SECONDS,
    GATEWAY_TIMEOUT_SECONDS,
    MODEL_NAME,
)
from tools.files import _insert, _row, _rows, _update, db_session, new_id, utc_now

logger = logging.getLogger(__name__)

# ---------- 错误码、敏感日志、稳定键 ----------

GATEWAY_ERROR_CODES = {
    "DEGRADED": "GATEWAY_DEGRADED",
    "TIMEOUT": "GATEWAY_TIMEOUT",
    "RATE_LIMIT": "GATEWAY_RATE_LIMIT",
    "INVALID_JSON": "GATEWAY_INVALID_JSON",
    "SCHEMA": "GATEWAY_SCHEMA_FAILED",
    "EVIDENCE": "GATEWAY_EVIDENCE_FAILED",
    "BOUNDARY": "GATEWAY_BOUNDARY_FAILED",
    "MODEL_NOT_ALLOWED": "GATEWAY_MODEL_NOT_ALLOWED",
    "UNREACHABLE": "GATEWAY_UNREACHABLE",
}

RUN_STATUS = {
    "STARTED",
    "SUCCESS",
    "REUSED",
    "INVALID_JSON",
    "SCHEMA_FAILED",
    "EVIDENCE_FAILED",
    "BOUNDARY_FAILED",
    "TIMEOUT",
    "RATE_LIMITED",
    "UNREACHABLE",
    "DEGRADED",
}

LOGIC_FAILURES = {"INVALID_JSON", "SCHEMA", "EVIDENCE", "BOUNDARY"}
TRANSIENT_FAILURES = {"TIMEOUT", "RATE_LIMIT", "UNREACHABLE"}


class _SimulatedRateLimit(Exception):
    """测试用 429，避免依赖 OpenAI 异常构造签名。"""

_SENSITIVE_LOG_PATTERNS = [
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
]


class GatewayError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        degraded: bool = False,
        run_id: str | None = None,
        details: dict | None = None,
    ):
        self.code = code
        self.message = message
        self.retryable = retryable
        self.degraded = degraded
        self.run_id = run_id
        self.details = details or {}
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.code,
            "message": self.message,
            "degraded": self.degraded,
            "run_id": self.run_id,
            "details": self.details,
        }


@dataclass
class GatewayResult:
    run_id: str
    status: str
    content: str | None = None
    parsed: Any = None
    model_name: str = ""
    purpose: str = ""
    prompt_version: str = ""
    input_hash: str = ""
    latency_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    retry_count: int = 0
    reused: bool = False
    degraded: bool = False
    error_code: str | None = None


class _Delta:
    def __init__(self, content=None, tool_calls=None, reasoning_content=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _Choice:
    def __init__(self, delta: _Delta):
        self.delta = delta


class _FakeChunk:
    def __init__(self, content: str | None = None, tool_calls=None):
        self.choices = [_Choice(_Delta(content=content, tool_calls=tool_calls))]


def canonical_hash(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def quote_hash(quote: str) -> str:
    return hashlib.sha256((quote or "").encode("utf-8")).hexdigest()


def sanitize_for_log(text: str, limit: int = 160) -> str:
    """日志不得出现身份证号、卡号、手机号或完整正文。"""
    if not text:
        return ""
    clipped = text[:limit]
    for pattern in _SENSITIVE_LOG_PATTERNS:
        clipped = pattern.sub("[redacted]", clipped)
    if len(text) > limit:
        clipped += "…"
    return clipped


def parse_json_content(text: str) -> Any:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ---------- 建表 ----------

GATEWAY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS model_runs (
    id VARCHAR(36) PRIMARY KEY,
    purpose VARCHAR(64) NOT NULL,
    prompt_version VARCHAR(64) NOT NULL,
    input_hash VARCHAR(64) NOT NULL,
    output_hash VARCHAR(64),
    model_name VARCHAR(128) NOT NULL,
    approval_id VARCHAR(64),
    status VARCHAR(32) NOT NULL,
    latency_ms INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_code VARCHAR(64),
    error_class VARCHAR(32),
    result_json TEXT,
    created_at DATETIME NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_runs_stable
ON model_runs(purpose, prompt_version, input_hash, status);

CREATE TABLE IF NOT EXISTS model_repair_queue (
    id VARCHAR(36) PRIMARY KEY,
    run_id VARCHAR(36),
    purpose VARCHAR(64) NOT NULL,
    prompt_version VARCHAR(64) NOT NULL,
    input_hash VARCHAR(64) NOT NULL,
    failure_class VARCHAR(32) NOT NULL,
    error_message TEXT NOT NULL,
    output_hash VARCHAR(64),
    created_at DATETIME NOT NULL
);
"""


def init_gateway_db(db_path=None) -> None:
    with db_session(db_path) as conn:
        conn.executescript(GATEWAY_SCHEMA_SQL)


# ---------- 网关 ----------

class ModelGateway:
    def __init__(self, db_path=None, sleep_fn=time.sleep):
        self.db_path = db_path or DATABASE_PATH
        self.sleep_fn = sleep_fn
        self._client: OpenAI | None = None
        init_gateway_db(self.db_path)

    def status(self) -> dict[str, Any]:
        degraded, reason = self.degraded_state()
        return {
            "degraded": degraded,
            "reason": reason,
            "mode": "deterministic_only" if degraded else (
                "fake" if GATEWAY_FAKE_MODE else "live"
            ),
            "external_calls_enabled": DEEPSEEK_EXTERNAL_CALLS_ENABLED,
            "fake_mode": GATEWAY_FAKE_MODE,
            "model_name": MODEL_NAME,
            "model_allowed": MODEL_NAME in GATEWAY_MODEL_WHITELIST,
        }

    def degraded_state(self) -> tuple[bool, str]:
        if GATEWAY_FAKE_MODE:
            return False, ""
        if not DEEPSEEK_EXTERNAL_CALLS_ENABLED:
            return True, "external_calls_disabled"
        if not API_KEY:
            return True, "missing_api_key"
        if MODEL_NAME not in GATEWAY_MODEL_WHITELIST:
            return True, "model_not_in_whitelist"
        return False, ""

    def list_runs(self, purpose: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            if purpose:
                return _rows(
                    conn,
                    "SELECT id, purpose, prompt_version, input_hash, output_hash, model_name, "
                    "approval_id, status, latency_ms, prompt_tokens, completion_tokens, "
                    "retry_count, error_code, error_class, created_at "
                    "FROM model_runs WHERE purpose = ? ORDER BY created_at DESC LIMIT ?",
                    (purpose, max(1, limit)),
                )
            return _rows(
                conn,
                "SELECT id, purpose, prompt_version, input_hash, output_hash, model_name, "
                "approval_id, status, latency_ms, prompt_tokens, completion_tokens, "
                "retry_count, error_code, error_class, created_at "
                "FROM model_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            )

    def list_repair_queue(self, limit: int = 50) -> list[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            return _rows(
                conn,
                "SELECT * FROM model_repair_queue ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            )

    def complete(
        self,
        *,
        purpose: str,
        prompt_version: str,
        messages: list[dict[str, Any]],
        input_payload: Any,
        approval_id: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        expect_json: bool = True,
        allow_reuse: bool = True,
        simulate: str | None = None,
        fake_content: str | None = None,
    ) -> GatewayResult:
        self._assert_model_allowed()
        input_hash = canonical_hash(input_payload)
        if allow_reuse:
            reused = self._find_success(purpose, prompt_version, input_hash)
            if reused:
                logger.info(
                    "model_run reused purpose=%s prompt_version=%s input_hash=%s run_id=%s",
                    purpose,
                    prompt_version,
                    input_hash,
                    reused["id"],
                )
                parsed = None
                if reused.get("result_json"):
                    parsed = json.loads(reused["result_json"])
                return GatewayResult(
                    run_id=reused["id"],
                    status="REUSED",
                    content=reused.get("result_json"),
                    parsed=parsed,
                    model_name=reused["model_name"],
                    purpose=purpose,
                    prompt_version=prompt_version,
                    input_hash=input_hash,
                    reused=True,
                )

        degraded, reason = self.degraded_state()
        if degraded and fake_content is None and simulate is None:
            run_id = self._insert_run(
                purpose,
                prompt_version,
                input_hash,
                approval_id,
                status="DEGRADED",
                error_code=GATEWAY_ERROR_CODES["DEGRADED"],
                error_class="DEGRADED",
            )
            logger.info(
                "model_run degraded purpose=%s reason=%s run_id=%s",
                purpose,
                reason,
                run_id,
            )
            return GatewayResult(
                run_id=run_id,
                status="DEGRADED",
                model_name=MODEL_NAME,
                purpose=purpose,
                prompt_version=prompt_version,
                input_hash=input_hash,
                degraded=True,
                error_code=GATEWAY_ERROR_CODES["DEGRADED"],
            )

        run_id = self._insert_run(
            purpose, prompt_version, input_hash, approval_id, status="STARTED"
        )
        started = time.monotonic()
        retry_count = 0
        last_error: Exception | None = None

        for attempt in range(1, GATEWAY_MAX_RETRIES + 1):
            retry_count = attempt - 1
            try:
                content, usage = self._one_attempt(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    expect_json=expect_json,
                    simulate=simulate,
                    fake_content=fake_content,
                    purpose=purpose,
                    input_payload=input_payload,
                )
                parsed = None
                if expect_json:
                    try:
                        parsed = parse_json_content(content)
                    except (json.JSONDecodeError, TypeError) as exc:
                        self._finish_logic_failure(
                            run_id,
                            purpose,
                            prompt_version,
                            input_hash,
                            failure_class="INVALID_JSON",
                            message="model returned invalid JSON",
                            raw=content,
                            retry_count=retry_count,
                            latency_ms=_elapsed_ms(started),
                        )
                        raise GatewayError(
                            GATEWAY_ERROR_CODES["INVALID_JSON"],
                            "model returned invalid JSON",
                            run_id=run_id,
                        ) from exc
                    content = json.dumps(parsed, ensure_ascii=False)

                self._update_run(
                    run_id,
                    {
                        "status": "SUCCESS",
                        "output_hash": canonical_hash(parsed if expect_json else content),
                        "result_json": content if expect_json else None,
                        "latency_ms": _elapsed_ms(started),
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "retry_count": retry_count,
                    },
                )
                logger.info(
                    "model_run ok purpose=%s prompt_version=%s input_hash=%s "
                    "latency_ms=%s retries=%s",
                    purpose,
                    prompt_version,
                    input_hash,
                    _elapsed_ms(started),
                    retry_count,
                )
                return GatewayResult(
                    run_id=run_id,
                    status="SUCCESS",
                    content=content,
                    parsed=parsed,
                    model_name=MODEL_NAME,
                    purpose=purpose,
                    prompt_version=prompt_version,
                    input_hash=input_hash,
                    latency_ms=_elapsed_ms(started),
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    retry_count=retry_count,
                )
            except GatewayError:
                raise
            except (APITimeoutError, TimeoutError) as exc:
                last_error = exc
                error_class = "TIMEOUT"
                error_code = GATEWAY_ERROR_CODES["TIMEOUT"]
            except (RateLimitError, _SimulatedRateLimit) as exc:
                last_error = exc
                error_class = "RATE_LIMIT"
                error_code = GATEWAY_ERROR_CODES["RATE_LIMIT"]
            except APIConnectionError as exc:
                last_error = exc
                error_class = "UNREACHABLE"
                error_code = GATEWAY_ERROR_CODES["UNREACHABLE"]
            except Exception as exc:
                last_error = exc
                error_class = "UNREACHABLE"
                error_code = GATEWAY_ERROR_CODES["UNREACHABLE"]

            if attempt >= GATEWAY_MAX_RETRIES:
                self._update_run(
                    run_id,
                    {
                        "status": error_class if error_class in RUN_STATUS else "UNREACHABLE",
                        "error_code": error_code,
                        "error_class": error_class,
                        "retry_count": retry_count,
                        "latency_ms": _elapsed_ms(started),
                    },
                )
                logger.info(
                    "model_run failed purpose=%s class=%s retries=%s preview=%s",
                    purpose,
                    error_class,
                    retry_count,
                    sanitize_for_log(str(last_error or "")),
                )
                raise GatewayError(
                    error_code,
                    f"{error_class.lower()} after {GATEWAY_MAX_RETRIES} attempts",
                    retryable=True,
                    run_id=run_id,
                )

            delay = GATEWAY_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            delay *= 0.5 + random.random()
            self.sleep_fn(delay)

        raise GatewayError(
            GATEWAY_ERROR_CODES["UNREACHABLE"],
            "exhausted retries",
            run_id=run_id,
        )

    def stream(
        self,
        *,
        purpose: str,
        prompt_version: str,
        messages: list[dict[str, Any]],
        tools: list | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.1,
        max_tokens: int = 4096,
        approval_id: str | None = None,
    ) -> Iterator[Any]:
        """ReAct 兼容流式出口：yield 与 OpenAI chunk 同形的对象。"""
        self._assert_model_allowed()
        input_hash = canonical_hash(
            {"n": len(messages), "tail": [m.get("role") for m in messages[-4:]]}
        )
        degraded, reason = self.degraded_state()
        if degraded:
            run_id = self._insert_run(
                purpose,
                prompt_version,
                input_hash,
                approval_id,
                status="DEGRADED",
                error_code=GATEWAY_ERROR_CODES["DEGRADED"],
                error_class="DEGRADED",
            )
            notice = (
                "当前处于仅确定性规则模式（外呼关闭或网关不可达），"
                f"原因：{reason}。模型对话暂不可用，材料与任务接口仍可使用。"
            )
            logger.info("model_run stream degraded run_id=%s reason=%s", run_id, reason)
            yield _FakeChunk(content=notice)
            return

        run_id = self._insert_run(
            purpose, prompt_version, input_hash, approval_id, status="STARTED"
        )
        started = time.monotonic()
        if GATEWAY_FAKE_MODE:
            yield _FakeChunk(content="（Fake 网关）受控助手在线，请使用材料工具读取已脱敏片段。")
            self._update_run(
                run_id,
                {
                    "status": "SUCCESS",
                    "latency_ms": _elapsed_ms(started),
                    "output_hash": canonical_hash("fake-stream"),
                },
            )
            return

        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": MODEL_NAME,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        try:
            stream = client.chat.completions.create(**kwargs)
            for chunk in stream:
                yield chunk
            self._update_run(
                run_id,
                {"status": "SUCCESS", "latency_ms": _elapsed_ms(started)},
            )
        except Exception as exc:
            self._update_run(
                run_id,
                {
                    "status": "UNREACHABLE",
                    "error_code": GATEWAY_ERROR_CODES["UNREACHABLE"],
                    "error_class": "UNREACHABLE",
                    "latency_ms": _elapsed_ms(started),
                },
            )
            logger.info(
                "model_run stream failed run_id=%s preview=%s",
                run_id,
                sanitize_for_log(str(exc)),
            )
            raise GatewayError(
                GATEWAY_ERROR_CODES["UNREACHABLE"],
                "stream failed",
                run_id=run_id,
            ) from exc

    def mark_logic_failure(
        self,
        run_id: str,
        *,
        purpose: str,
        prompt_version: str,
        input_hash: str,
        failure_class: str,
        message: str,
        raw: str | None = None,
    ) -> None:
        self._finish_logic_failure(
            run_id,
            purpose,
            prompt_version,
            input_hash,
            failure_class=failure_class,
            message=message,
            raw=raw or "",
            retry_count=0,
            latency_ms=None,
        )

    def _one_attempt(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int,
        expect_json: bool,
        simulate: str | None,
        fake_content: str | None,
        purpose: str,
        input_payload: Any,
    ) -> tuple[str, dict[str, Any]]:
        if simulate == "timeout":
            raise TimeoutError("simulated timeout")
        if simulate == "429":
            raise _SimulatedRateLimit("simulated 429")
        if simulate == "invalid_json":
            return "{not-json", {}
        if simulate == "schema":
            return json.dumps({"objects": "bad-schema"}), {}

        if fake_content is not None or GATEWAY_FAKE_MODE:
            content = fake_content if fake_content is not None else _default_fake_content(
                purpose, input_payload
            )
            return content, {"prompt_tokens": 0, "completion_tokens": 0}

        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": MODEL_NAME,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if expect_json:
            kwargs["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        usage = {}
        if getattr(response, "usage", None):
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", None),
                "completion_tokens": getattr(response.usage, "completion_tokens", None),
            }
        return content, usage

    def _get_client(self) -> OpenAI:
        if self._client is None:
            if not API_KEY:
                raise GatewayError(
                    GATEWAY_ERROR_CODES["UNREACHABLE"],
                    "missing api key",
                    degraded=True,
                )
            self._client = OpenAI(
                api_key=API_KEY,
                base_url=BASE_URL,
                timeout=GATEWAY_TIMEOUT_SECONDS,
            )
        return self._client

    def _assert_model_allowed(self) -> None:
        if MODEL_NAME not in GATEWAY_MODEL_WHITELIST:
            raise GatewayError(
                GATEWAY_ERROR_CODES["MODEL_NOT_ALLOWED"],
                f"model not in whitelist: {MODEL_NAME}",
            )

    def _find_success(
        self, purpose: str, prompt_version: str, input_hash: str
    ) -> Optional[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            return _row(
                conn,
                "SELECT * FROM model_runs WHERE purpose = ? AND prompt_version = ? "
                "AND input_hash = ? AND status = 'SUCCESS' "
                "ORDER BY created_at DESC LIMIT 1",
                (purpose, prompt_version, input_hash),
            )

    def _insert_run(
        self,
        purpose: str,
        prompt_version: str,
        input_hash: str,
        approval_id: str | None,
        *,
        status: str,
        error_code: str | None = None,
        error_class: str | None = None,
    ) -> str:
        run_id = new_id()
        with db_session(self.db_path) as conn:
            _insert(
                conn,
                "model_runs",
                {
                    "id": run_id,
                    "purpose": purpose,
                    "prompt_version": prompt_version,
                    "input_hash": input_hash,
                    "output_hash": None,
                    "model_name": MODEL_NAME,
                    "approval_id": approval_id,
                    "status": status,
                    "latency_ms": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "retry_count": 0,
                    "error_code": error_code,
                    "error_class": error_class,
                    "result_json": None,
                    "created_at": utc_now(),
                },
            )
        return run_id

    def _update_run(self, run_id: str, fields: dict[str, Any]) -> None:
        with db_session(self.db_path) as conn:
            _update(conn, "model_runs", run_id, fields)

    def _finish_logic_failure(
        self,
        run_id: str,
        purpose: str,
        prompt_version: str,
        input_hash: str,
        *,
        failure_class: str,
        message: str,
        raw: str,
        retry_count: int,
        latency_ms: int | None,
    ) -> None:
        status_map = {
            "INVALID_JSON": "INVALID_JSON",
            "SCHEMA": "SCHEMA_FAILED",
            "EVIDENCE": "EVIDENCE_FAILED",
            "BOUNDARY": "BOUNDARY_FAILED",
        }
        status = status_map.get(failure_class, "SCHEMA_FAILED")
        code_map = {
            "INVALID_JSON": GATEWAY_ERROR_CODES["INVALID_JSON"],
            "SCHEMA": GATEWAY_ERROR_CODES["SCHEMA"],
            "EVIDENCE": GATEWAY_ERROR_CODES["EVIDENCE"],
            "BOUNDARY": GATEWAY_ERROR_CODES["BOUNDARY"],
        }
        fields = {
            "status": status,
            "error_code": code_map.get(failure_class),
            "error_class": failure_class,
            "retry_count": retry_count,
            "output_hash": canonical_hash(raw) if raw else None,
        }
        if latency_ms is not None:
            fields["latency_ms"] = latency_ms
        self._update_run(run_id, fields)
        with db_session(self.db_path) as conn:
            _insert(
                conn,
                "model_repair_queue",
                {
                    "id": new_id(),
                    "run_id": run_id,
                    "purpose": purpose,
                    "prompt_version": prompt_version,
                    "input_hash": input_hash,
                    "failure_class": failure_class,
                    "error_message": sanitize_for_log(message, limit=240),
                    "output_hash": fields["output_hash"],
                    "created_at": utc_now(),
                },
            )
        logger.info(
            "model_run queued_repair purpose=%s class=%s run_id=%s preview=%s",
            purpose,
            failure_class,
            run_id,
            sanitize_for_log(raw),
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _default_fake_content(purpose: str, input_payload: Any) -> str:
    """Fake 与真实网关返回同一套 JSON 对象形状，供无外呼验证。"""
    payload = input_payload if isinstance(input_payload, dict) else {}
    if purpose == "extraction":
        objects = []
        for chunk in payload.get("chunks") or []:
            text = chunk.get("text") or ""
            match = re.search(r"人员[：:]\s*([\u4e00-\u9fff·]{1,2})", text)
            if not match:
                continue
            quote = match.group(0)
            objects.append(
                {
                    "object_type": "PERSON",
                    "surface": match.group(1),
                    "attributes": {},
                    "confidence": 0.5,
                    "evidence": [
                        {
                            "chunk_id": chunk.get("chunk_id"),
                            "quote": quote,
                            "quote_hash": quote_hash(quote),
                            "page_start": chunk.get("page_start"),
                            "page_end": chunk.get("page_end"),
                        }
                    ],
                }
            )
        return json.dumps({"objects": objects, "notes": []}, ensure_ascii=False)
    if purpose == "normalization":
        return json.dumps(
            {
                "consistencies": ["双方均有可定位出处"],
                "differences": ["记载字段不完全一致，需人工核对"],
                "questions_for_human": ["请并列核对原文后再决定是否合并"],
            },
            ensure_ascii=False,
        )
    if purpose == "clue_wording":
        hits = payload.get("rule_hits") or []
        cited = []
        for hit in hits:
            for ev in hit.get("evidence") or []:
                if ev.get("chunk_id"):
                    cited.append(ev["chunk_id"])
        return json.dumps(
            {
                "title": "疑似跨案标识重合（待核验）",
                "summary": "规则命中已成立的标识重合，仅供人工核验，不作为事实认定。",
                "cited_chunk_ids": cited,
            },
            ensure_ascii=False,
        )
    if purpose == "output_verify":
        return json.dumps(
            {
                "passed": True,
                "over_bound": False,
                "candidate_as_fact": False,
                "missing_reverse": False,
                "issues": [],
            },
            ensure_ascii=False,
        )
    return json.dumps({"ok": True}, ensure_ascii=False)


_gateway: ModelGateway | None = None


def get_gateway() -> ModelGateway:
    global _gateway
    if _gateway is None:
        _gateway = ModelGateway()
    return _gateway


def set_gateway(gateway: ModelGateway | None) -> None:
    global _gateway
    _gateway = gateway
