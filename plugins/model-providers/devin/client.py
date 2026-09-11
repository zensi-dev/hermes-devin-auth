"""Devin Cascade client — Connect-protocol protobuf over HTTP.

Speaks Connect-protocol protobuf to ``server.codeium.com``:

* ``AuthService/GetUserJwt`` (unary, ``application/proto``) exchanges the
  session token for a short-lived user JWT + optional custom API server URL.
* ``ApiServerService/GetChatMessage`` (server-streaming,
  ``application/connect+proto``, gzip frames) streams chat deltas.
* ``ApiServerService/GetCliModelConfigs`` (unary) lists available model uids.

The public surface is the OpenAI-shaped ``chat.completions.create`` contract
Hermes' chat-completions transport consumes (same duck-type the ACP shim
implements): ``stream=True`` yields ``ChatCompletionChunk``-shaped objects,
``stream=False`` returns a ``ChatCompletion``-shaped object.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import proto_wire as pw
from .oauth import normalize_session_token

logger = logging.getLogger(__name__)

DEVIN_API_URL = "https://server.codeium.com"
CHAT_MESSAGE_PATH = "/exa.api_server_pb.ApiServerService/GetChatMessage"
AUTH_PATH = "/exa.auth_pb.AuthService/GetUserJwt"
MODEL_CONFIGS_PATH = "/exa.api_server_pb.ApiServerService/GetCliModelConfigs"

# Client identity the Cascade API expects (Windsurf IDE build).
IDE_NAME = "windsurf"
IDE_VERSION = "3.2.23"
EXTENSION_NAME = "windsurf"
EXTENSION_VERSION = "1.48.2"
LOCALE = "en"

CONNECT_COMPRESSED_FLAG = 0x01
CONNECT_END_STREAM_FLAG = 0x02
MAX_CONNECT_FRAME_PAYLOAD = 16 * 1024 * 1024

DEFAULT_STOP_PATTERNS = ["<|user|>", "<|bot|>", "<|context_request|>", "<|endoftext|>", "<|end_of_turn|>"]

# ChatMessageSource
SRC_USER = 1
SRC_SYSTEM = 2
SRC_TOOL = 4
# ChatMessageRequestType
REQUEST_TYPE_CASCADE = 5
# ConversationalPlannerMode
PLANNER_MODE_DEFAULT = 1
# CacheControlType
CACHE_EPHEMERAL = 1
# StopReason
STOP_MAX_TOKENS = 3
STOP_FUNCTION_CALL = 10

DEFAULT_MAX_TOKENS = 64000
DEFAULT_TEMPERATURE = 0.4


class DevinAPIError(Exception):
    """Provider error with an HTTP-ish status for Hermes' retry classification."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.message = message


def _deterministic_uuid(seed: str) -> str:
    """Deterministic UUID: first 128 bits of SHA-256 as 8-4-4-4-12 hex."""
    h = hashlib.sha256(seed.encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ---------------------------------------------------------------------------
# Proto message builders (field numbers from the vendored exa protos)
# ---------------------------------------------------------------------------

def _metadata(api_key: str, user_jwt: str = "") -> bytes:
    out = pw.f_str(1, IDE_NAME)            # ide_name
    out += pw.f_str(7, IDE_VERSION)        # ide_version
    out += pw.f_str(12, EXTENSION_NAME)    # extension_name
    out += pw.f_str(2, EXTENSION_VERSION)  # extension_version
    out += pw.f_str(3, api_key)            # api_key
    out += pw.f_str(4, LOCALE)             # locale
    if user_jwt:
        out += pw.f_str(21, user_jwt)      # user_jwt
    return out


def _image_data(part: Dict[str, Any]) -> Optional[bytes]:
    """OpenAI image_url part → ImageData{base64_data, mime_type}. Data URLs only."""
    url = ""
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        url = image_url.get("url") or ""
    elif isinstance(image_url, str):
        url = image_url
    if not url.startswith("data:"):
        return None
    header, _, data = url.partition(",")
    mime = header[5:].split(";")[0] or "image/png"
    if not data:
        return None
    return pw.f_str(1, data) + pw.f_str(2, mime)


def _content_text_and_images(content: Any) -> Tuple[str, List[bytes]]:
    """Flatten OpenAI message content into (text, [ImageData])."""
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return ("" if content is None else str(content)), []
    text_parts: List[str] = []
    images: List[bytes] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("text", "input_text", "output_text"):
            text_parts.append(part.get("text") or "")
        elif ptype in ("image_url", "input_image"):
            img = _image_data(part)
            if img is not None:
                images.append(img)
    return "".join(text_parts), images


def _chat_tool_call(tc: Dict[str, Any]) -> bytes:
    fn = tc.get("function") or {}
    args = fn.get("arguments")
    if not isinstance(args, str):
        args = json.dumps(args or {})
    return (
        pw.f_str(1, tc.get("id") or "")
        + pw.f_str(2, fn.get("name") or "")
        + pw.f_str(3, args)
    )


def _chat_message_prompt(message_id: str, source: int, prompt: str, *,
                         tool_calls: Optional[List[bytes]] = None,
                         tool_call_id: str = "",
                         tool_result_is_error: bool = False,
                         images: Optional[List[bytes]] = None,
                         thinking: str = "",
                         signature: str = "") -> bytes:
    out = pw.f_str(1, message_id) + pw.f_varint(2, source) + pw.f_str(3, prompt)
    for tc in tool_calls or ():
        out += pw.f_msg(6, tc)
    if tool_call_id:
        out += pw.f_str(7, tool_call_id)
    if tool_result_is_error:
        out += pw.f_bool(9, True)
    for img in images or ():
        out += pw.f_msg(10, img)
    if thinking:
        out += pw.f_str(11, thinking)
    if signature:
        out += pw.f_str(12, signature)
    return out


def _tool_result_is_error(content: Any) -> bool:
    """Hermes tool results carry no error flag; sniff the JSON payload."""
    text = content if isinstance(content, str) else ""
    if not text:
        if isinstance(content, list):
            text = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    try:
        data = json.loads(text)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("is_error") is True or data.get("isError") is True:
        return True
    if data.get("status") == "error" or data.get("success") is False:
        return True
    return bool(data.get("error"))


def _completion_configuration(max_tokens: int, temperature: float, top_p: float,
                              stop: Optional[List[str]]) -> bytes:
    stops = list(DEFAULT_STOP_PATTERNS)
    if stop:
        stops.extend(stop)
    out = pw.f_varint(1, 1)                       # num_completions
    out += pw.f_varint(2, max_tokens)             # max_tokens
    out += pw.f_varint(3, 200)                    # max_newlines
    out += pw.f_fixed64(5, temperature)           # temperature
    out += pw.f_fixed64(6, temperature)           # first_temperature
    out += pw.f_varint(7, 50)                     # top_k
    out += pw.f_fixed64(8, top_p)                 # top_p
    out += pw.f_strs(9, stops)                    # stop_patterns
    out += pw.f_fixed64(11, 1.0)                  # fim_eot_prob_threshold
    return out


def _chat_tool_definition(tool: Dict[str, Any]) -> bytes:
    fn = tool.get("function") if tool.get("type") == "function" else tool
    fn = fn or {}
    params = fn.get("parameters")
    schema = json.dumps(params) if isinstance(params, (dict, list)) else (params or "")
    return (
        pw.f_str(1, fn.get("name") or "")
        + pw.f_str(2, fn.get("description") or "")
        + pw.f_str(3, schema)
        + pw.f_bool(12, bool(fn.get("strict")))
    )


def build_chat_request(*, api_key: str, user_jwt: str, model_uid: str,
                       messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]],
                       max_tokens: int, temperature: float, top_p: float,
                       stop: Optional[List[str]], cascade_id: str) -> bytes:
    """GetChatMessageRequest."""
    system_parts: List[str] = []
    prompts: List[bytes] = []
    for index, msg in enumerate(messages):
        role = msg.get("role") or ""
        if role == "system":
            text, _ = _content_text_and_images(msg.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role in ("user", "developer"):
            text, images = _content_text_and_images(msg.get("content"))
            prompts.append(_chat_message_prompt(
                _deterministic_uuid(f"{cascade_id}\x00{index}\x00{role}"),
                SRC_USER, text, images=images))
        elif role == "assistant":
            text, _ = _content_text_and_images(msg.get("content"))
            thinking = msg.get("reasoning_content") or msg.get("thinking") or ""
            tool_calls = [
                _chat_tool_call(tc) for tc in (msg.get("tool_calls") or [])
                if isinstance(tc, dict)
            ]
            if not text and not thinking and not tool_calls:
                continue
            seed = f"{cascade_id}\x00{index}\x00assistant"
            prompts.append(_chat_message_prompt(
                f"bot-{_deterministic_uuid(seed)}",
                SRC_SYSTEM, text, tool_calls=tool_calls, thinking=thinking))
        elif role == "tool":
            text, images = _content_text_and_images(msg.get("content"))
            tool_call_id = msg.get("tool_call_id") or ""
            prompts.append(_chat_message_prompt(
                _deterministic_uuid(f"{cascade_id}\x00{index}\x00tool\x00{tool_call_id}"),
                SRC_TOOL, text, tool_call_id=tool_call_id,
                tool_result_is_error=_tool_result_is_error(msg.get("content")),
                images=images))
        # unknown roles are dropped

    out = pw.f_msg(1, _metadata(api_key, user_jwt))
    out += pw.f_str(2, "\n\n".join(system_parts))
    for p in prompts:
        out += pw.f_msg(3, p)
    out += pw.f_str(21, model_uid)                                   # chat_model_uid
    out += pw.f_varint(7, REQUEST_TYPE_CASCADE)                      # request_type
    out += pw.f_msg(8, _completion_configuration(max_tokens, temperature, top_p, stop))
    for t in tools or ():
        out += pw.f_msg(10, _chat_tool_definition(t))
    out += pw.f_bool(11, True)                                       # disable_parallel_tool_calls
    out += pw.f_msg(12, pw.f_str(1, "auto"))                         # tool_choice{option_name:"auto"}
    out += pw.f_msg(13, pw.f_varint(1, CACHE_EPHEMERAL))             # system_prompt_cache_options
    out += pw.f_str(16, cascade_id)                                  # cascade_id
    out += pw.f_varint(20, PLANNER_MODE_DEFAULT)                     # planner_mode
    out += pw.f_str(22, str(uuid.uuid4()))                           # execution_id
    return out


def build_user_jwt_request(api_key: str) -> bytes:
    return pw.f_msg(1, _metadata(api_key))


def build_model_configs_request(api_key: str) -> bytes:
    return pw.f_msg(1, _metadata(api_key))


# ---------------------------------------------------------------------------
# Connect framing
# ---------------------------------------------------------------------------

def _connect_frame(message: bytes) -> bytes:
    gz = gzip.compress(message)
    return bytes([CONNECT_COMPRESSED_FLAG]) + len(gz).to_bytes(4, "big") + gz


def _iter_connect_frames(chunk_iter) -> Iterator[Tuple[int, bytes]]:
    """Yield (flag, payload) per Connect frame from a byte-chunk iterator."""
    pending = bytearray()
    for chunk in chunk_iter:
        if chunk:
            pending.extend(chunk)
        while len(pending) >= 5:
            flag = pending[0]
            length = int.from_bytes(pending[1:5], "big")
            if length > MAX_CONNECT_FRAME_PAYLOAD:
                raise DevinAPIError(
                    f"Devin Connect frame length {length} exceeds {MAX_CONNECT_FRAME_PAYLOAD}-byte cap")
            if len(pending) < 5 + length:
                break
            payload = bytes(pending[5:5 + length])
            del pending[:5 + length]
            yield flag, payload


def _trailer_error(payload: bytes) -> Optional[str]:
    try:
        parsed = json.loads(payload.decode("utf-8", errors="replace").strip() or "null")
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    err = parsed.get("error")
    if not isinstance(err, dict):
        return None
    code = err.get("code") if isinstance(err.get("code"), str) else ""
    message = err.get("message") if isinstance(err.get("message"), str) else ""
    if not code and not message:
        return None
    return f"Devin stream error{f' {code}' if code else ''}: {message}"


# ---------------------------------------------------------------------------
# Response decoding → OpenAI-shaped objects
# ---------------------------------------------------------------------------

def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _chunk(model: str, *, delta=None, finish_reason=None, usage=None, chunk_id=None):
    choice = _ns(index=0, delta=delta or _ns(), finish_reason=finish_reason, logprobs=None)
    return _ns(
        id=chunk_id or f"chatcmpl-devin-{uuid.uuid4().hex[:24]}",
        object="chat.completion.chunk",
        created=int(time.time()),
        model=model,
        choices=[choice] if (delta is not None or finish_reason is not None) else [],
        usage=usage,
        service_tier=None,
    )


def _tool_call_delta(index: int, tc_id: str, name: str, arguments_delta: str):
    return _ns(index=index, id=tc_id or None, type="function",
               function=_ns(name=name or None, arguments=arguments_delta or None))


def _usage(prompt: int, completion: int, cached: int, cache_write: int):
    total = prompt + completion + cached + cache_write
    details = _ns(cached_tokens=cached) if cached else None
    return _ns(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
               prompt_tokens_details=details, completion_tokens_details=None)


def _decode_response_message(raw: bytes) -> pw.Fields:
    try:
        return pw.decode(raw)
    except pw.ProtoError:
        return pw.decode(gzip.decompress(raw))


def _finish_reason(stop_reason: int, saw_tool_calls: bool) -> str:
    if saw_tool_calls or stop_reason == STOP_FUNCTION_CALL:
        return "tool_calls"
    if stop_reason == STOP_MAX_TOKENS:
        return "length"
    return "stop"


class DevinClient:
    """OpenAI-shaped client over the Devin Cascade Connect API."""

    # Hermes wraps foreign clients in its OpenAI-compat transport and an async
    # bridge unless these are set — our client already speaks the target shape.
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(self, api_key: str, base_url: str = DEVIN_API_URL, timeout: float = 600.0):
        self.api_key = api_key
        self.base_url = (base_url or DEVIN_API_URL).rstrip("/")
        self.timeout = timeout
        self._auth_cache: Optional[Tuple[str, str, float]] = None  # (jwt, base_url, expires_epoch)
        self.chat = _ChatNamespace(self)

    def close(self) -> None:
        pass

    # -- auth ---------------------------------------------------------------

    def _user_jwt(self) -> Tuple[str, str]:
        """(user_jwt, chat_base_url); cached until the JWT's own expiry."""
        now = time.time()
        if self._auth_cache and self._auth_cache[2] > now + 60:
            return self._auth_cache[0], self._auth_cache[1]

        import httpx
        body = build_user_jwt_request(normalize_session_token(self.api_key))
        try:
            resp = httpx.post(
                self.base_url + AUTH_PATH,
                content=body,
                headers={
                    "content-type": "application/proto",
                    "connect-protocol-version": "1",
                    "accept": "*/*",
                },
                timeout=30.0,
            )
        except httpx.TimeoutException as exc:
            raise DevinAPIError(f"Devin auth request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise DevinAPIError(f"Devin auth request failed: {exc}") from exc
        if resp.status_code != 200:
            raise DevinAPIError(
                f"Devin auth error {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code, body=resp.text[:500])
        try:
            fields = _decode_response_message(resp.content)
        except Exception:
            raise DevinAPIError("Devin auth error: malformed GetUserJwt response")
        user_jwt = pw.get_str(fields, 1)
        if not user_jwt:
            raise DevinAPIError("Devin auth error: GetUserJwt returned an empty user JWT")
        custom = pw.get_str(fields, 2).strip().rstrip("/")
        base = custom or self.base_url
        self._auth_cache = (user_jwt, base, _jwt_expiry_epoch(user_jwt))
        return user_jwt, base

    # -- chat ---------------------------------------------------------------

    def _create(self, *, model: str, messages: List[Dict[str, Any]], stream: bool = False,
                tools=None, max_tokens=None, max_completion_tokens=None,
                temperature=None, top_p=None, stop=None, extra_body=None,
                timeout=None, **_ignored):
        extra_body = extra_body if isinstance(extra_body, dict) else {}
        cascade_id = extra_body.get("devin_cascade_id") or str(uuid.uuid4())
        model_uid = model.split("/", 1)[1] if model.startswith("devin/") else model
        effective_max = int(max_tokens or max_completion_tokens or DEFAULT_MAX_TOKENS)
        effective_temp = DEFAULT_TEMPERATURE if temperature is None else float(temperature)
        effective_top_p = 1.0 if top_p is None else float(top_p)
        stop_list = [stop] if isinstance(stop, str) else (list(stop) if stop else None)
        req_timeout = timeout if timeout is not None else self.timeout

        def build(user_jwt: str) -> bytes:
            return build_chat_request(
                api_key=normalize_session_token(self.api_key), user_jwt=user_jwt,
                model_uid=model_uid, messages=messages, tools=tools,
                max_tokens=effective_max, temperature=effective_temp,
                top_p=effective_top_p, stop=stop_list, cascade_id=cascade_id)

        if stream:
            return _DevinStream(self, build, model_uid, req_timeout)
        return self._collect(build, model_uid, req_timeout)

    def _post_stream(self, build, timeout):
        """One streaming attempt; returns an httpx streaming response context."""
        import httpx
        user_jwt, chat_base = self._user_jwt()
        frame = _connect_frame(build(user_jwt))
        return httpx.stream(
            "POST", chat_base + CHAT_MESSAGE_PATH,
            content=frame,
            headers={
                "content-type": "application/connect+proto",
                "connect-protocol-version": "1",
                "connect-content-encoding": "gzip",
                "accept-encoding": "identity",
                "user-agent": "connect-go/1.18.1 (go1.26.3)",
                "connect-accept-encoding": "gzip",
            },
            timeout=timeout,
        )

    def _events(self, build, timeout) -> Iterator[Dict[str, Any]]:
        """Normalized event dicts from the Connect stream; one 401 retry."""
        import httpx
        last_exc: Optional[Exception] = None
        for attempt in (0, 1):
            try:
                with self._post_stream(build, timeout) as resp:
                    if resp.status_code != 200:
                        body = resp.read().decode(errors="replace")[:500]
                        raise DevinAPIError(
                            f"Devin API error {resp.status_code}: {body}",
                            status_code=resp.status_code, body=body)
                    yield from self._decode_stream(resp.iter_bytes())
                    return
            except DevinAPIError as exc:
                last_exc = exc
                if exc.status_code in (401, 403) and attempt == 0:
                    self._auth_cache = None  # JWT may have expired mid-session
                    continue
                raise
            except httpx.TimeoutException as exc:
                raise DevinAPIError(f"Devin request timed out: {exc}") from exc
            except httpx.HTTPError as exc:
                raise DevinAPIError(f"Devin request failed: {exc}") from exc
        if last_exc:
            raise last_exc

    def _decode_stream(self, byte_iter) -> Iterator[Dict[str, Any]]:
        for flag, payload in _iter_connect_frames(byte_iter):
            if flag & CONNECT_END_STREAM_FLAG:
                raw = gzip.decompress(payload) if flag & CONNECT_COMPRESSED_FLAG else payload
                error = _trailer_error(raw)
                if error:
                    yield {"type": "error", "message": error}
                yield {"type": "end"}
                continue
            raw = gzip.decompress(payload) if flag & CONNECT_COMPRESSED_FLAG else payload
            try:
                fields = pw.decode(raw)
            except pw.ProtoError:
                continue
            message_id = pw.get_str(fields, 1)
            delta_text = pw.get_str(fields, 3)
            delta_thinking = pw.get_str(fields, 9)
            delta_signature = pw.get_str(fields, 10)
            stop_reason = pw.get_int(fields, 5)
            tool_calls = []
            for tc in pw.get_msgs(fields, 6):
                tool_calls.append({
                    "id": pw.get_str(tc, 1),
                    "name": pw.get_str(tc, 2),
                    # Field 4 (invalid_json_str) carries the raw payload when the
                    # server couldn't parse it as JSON — better to surface the
                    # malformed args than to silently dispatch with {}.
                    "arguments_json": pw.get_str(tc, 3) or pw.get_str(tc, 4),
                })
            usage = None
            usage_fields = pw.get_msg(fields, 7)
            if usage_fields is not None:
                usage = {
                    "input": pw.get_int(usage_fields, 2),
                    "output": pw.get_int(usage_fields, 3),
                    "cache_write": pw.get_int(usage_fields, 4),
                    "cache_read": pw.get_int(usage_fields, 5),
                }
            yield {
                "type": "delta", "message_id": message_id,
                "text": delta_text, "thinking": delta_thinking,
                "signature": delta_signature, "stop_reason": stop_reason,
                "tool_calls": tool_calls, "usage": usage,
            }

    def _stream_chunks(self, build, model: str, timeout) -> Iterator[Any]:
        """Translate normalized events into ChatCompletionChunk-shaped deltas."""
        tool_order: Dict[str, int] = {}
        partial_json: Dict[str, str] = {}
        active_id: Optional[str] = None
        saw_tool_calls = False
        latest_stop = 0
        usage_ns = None
        response_id = None

        for event in self._events(build, timeout):
            etype = event["type"]
            if etype == "error":
                raise DevinAPIError(event["message"])
            if etype == "end":
                break
            if event["message_id"] and not response_id:
                response_id = event["message_id"]
            if event["thinking"]:
                yield _chunk(model, delta=_ns(content=None, reasoning_content=event["thinking"],
                                              role=None, tool_calls=None),
                             chunk_id=response_id)
            if event["text"]:
                yield _chunk(model, delta=_ns(content=event["text"], reasoning_content=None,
                                              role=None, tool_calls=None),
                             chunk_id=response_id)
            for tc in event["tool_calls"]:
                # Argument continuations arrive in id-less frames that extend the
                # active call (same convention as the reference client).
                tc_id = tc["id"] or active_id
                if not tc_id:
                    continue
                active_id = tc_id
                saw_tool_calls = True
                if tc_id not in tool_order:
                    tool_order[tc_id] = len(tool_order)
                    partial_json[tc_id] = ""
                index = tool_order[tc_id]
                # A payload that extends the previous one is cumulative —
                # emit only the new suffix; otherwise append.
                prev = partial_json[tc_id]
                incoming = tc["arguments_json"] or ""
                if incoming:
                    accumulated = incoming if incoming.startswith(prev) else prev + incoming
                    delta = accumulated[len(prev):]
                    partial_json[tc_id] = accumulated
                else:
                    delta = ""
                yield _chunk(model, delta=_ns(
                    content=None, reasoning_content=None, role=None,
                    tool_calls=[_tool_call_delta(index, tc_id, tc["name"], delta)]),
                    chunk_id=response_id)
            if event["stop_reason"]:
                latest_stop = event["stop_reason"]
            if event["usage"]:
                u = event["usage"]
                usage_ns = _usage(u["input"], u["output"], u["cache_read"], u["cache_write"])

        yield _chunk(model, finish_reason=_finish_reason(latest_stop, saw_tool_calls),
                     chunk_id=response_id)
        if usage_ns is not None:
            yield _chunk(model, usage=usage_ns, chunk_id=response_id)

    def _collect(self, build, model: str, timeout):
        """Non-streaming: consume the stream, return a ChatCompletion-shaped object."""
        text_parts: List[str] = []
        thinking_parts: List[str] = []
        active_id: Optional[str] = None
        tool_calls: Dict[str, Dict[str, str]] = {}
        latest_stop = 0
        usage_ns = None
        response_id = None

        for event in self._events(build, timeout):
            etype = event["type"]
            if etype == "error":
                raise DevinAPIError(event["message"])
            if etype == "end":
                break
            if event["message_id"] and not response_id:
                response_id = event["message_id"]
            if event["thinking"]:
                thinking_parts.append(event["thinking"])
            if event["text"]:
                text_parts.append(event["text"])
            for tc in event["tool_calls"]:
                tc_id = tc["id"] or active_id
                if not tc_id:
                    continue
                active_id = tc_id
                entry = tool_calls.setdefault(tc_id, {"id": tc_id, "name": "", "args": ""})
                if tc["name"]:
                    entry["name"] = tc["name"]
                incoming = tc["arguments_json"] or ""
                prev = entry["args"]
                entry["args"] = incoming if incoming.startswith(prev) else prev + incoming
            if event["stop_reason"]:
                latest_stop = event["stop_reason"]
            if event["usage"]:
                u = event["usage"]
                usage_ns = _usage(u["input"], u["output"], u["cache_read"], u["cache_write"])

        tc_list = [
            _ns(id=e["id"], type="function",
                function=_ns(name=e["name"], arguments=e["args"]))
            for e in tool_calls.values()
        ]
        message = _ns(
            role="assistant",
            content="".join(text_parts) or None,
            tool_calls=tc_list or None,
            reasoning_content="".join(thinking_parts) or None,
            refusal=None,
            function_call=None,
            audio=None,
        )
        return _ns(
            id=response_id or f"chatcmpl-devin-{uuid.uuid4().hex[:24]}",
            object="chat.completion",
            created=int(time.time()),
            model=model,
            choices=[_ns(index=0, message=message,
                         finish_reason=_finish_reason(latest_stop, bool(tc_list)),
                         logprobs=None)],
            usage=usage_ns,
            service_tier=None,
        )


class _DevinStream:
    """Iterator wrapper exposing ``response``/``close`` like an SDK stream."""

    def __init__(self, client: DevinClient, build, model: str, timeout):
        self._gen = client._stream_chunks(build, model, timeout)
        self.response = None         # no httpx response survives the generator boundary
        self.final_response = None   # read directly by the stream loop

    def __iter__(self):
        return self._gen

    def __next__(self):
        return next(self._gen)

    def close(self):
        self._gen.close()


class _CompletionsNamespace:
    def __init__(self, client: DevinClient):
        self._client = client

    def create(self, **kwargs):
        return self._client._create(**kwargs)


class _ChatNamespace:
    def __init__(self, client: DevinClient):
        self.completions = _CompletionsNamespace(client)


def _jwt_expiry_epoch(token: str) -> float:
    try:
        import base64
        payload = token.split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = decoded.get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
    except Exception:
        pass
    return time.time() + 3600  # unknown → re-auth hourly


# ---------------------------------------------------------------------------
# Model listing (GetCliModelConfigs) — used by the profile's fetch_models hook
# ---------------------------------------------------------------------------

def list_models(api_key: str, base_url: str = DEVIN_API_URL, timeout: float = 8.0) -> List[str]:
    """Return available chat model uids, or raise on failure."""
    import httpx
    body = build_model_configs_request(normalize_session_token(api_key))
    resp = httpx.post(
        (base_url or DEVIN_API_URL).rstrip("/") + MODEL_CONFIGS_PATH,
        content=body,
        headers={"content-type": "application/proto", "connect-protocol-version": "1",
                 "accept": "*/*"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise DevinAPIError(f"Devin model list error {resp.status_code}: {resp.text[:300]}",
                            status_code=resp.status_code)
    fields = _decode_response_message(resp.content)
    models: List[str] = []
    for cfg in pw.get_msgs(fields, 1):  # client_model_configs
        uid = pw.get_str(cfg, 22)  # ClientModelConfig.model_uid
        if uid and not pw.get_bool(cfg, 4):  # skip disabled
            models.append(uid)
    return models
