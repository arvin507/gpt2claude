"""Local Anthropic Messages -> Codex subscription Responses adapter (stdlib only)."""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import re
from pathlib import Path
import secrets
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
UPSTREAM = "https://chatgpt.com/backend-api/codex/responses"
LOG = logging.getLogger("proxy")
MAX_BODY = 32 * 1024 * 1024


class ProxyError(Exception):
    def __init__(self, message, status=400, kind="invalid_request_error"):
        super().__init__(message)
        self.status, self.kind = status, kind


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def safe_error_text(value):
    text = str(value)
    text = re.sub(r"(?i)Bearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED_JWT]", text)
    text = re.sub(r"\b(?:sk-|local-)[A-Za-z0-9_-]{12,}", "[REDACTED_KEY]", text)
    return " ".join(text.split())[:1600]


def upstream_error_details(payload):
    """Select error fields only; do not dump request bodies or full responses."""
    if isinstance(payload, dict):
        error = payload.get("error") or payload.get("detail") or payload
        if isinstance(error, dict):
            parts = [f"{key}={safe_error_text(error[key])}" for key in ("type", "code", "param", "message")
                     if isinstance(error.get(key), (str, int))]
            return "; ".join(parts) or "No structured error details returned."
        if isinstance(error, str):
            return safe_error_text(error)
    return "Non-JSON error response (body omitted)."


def require(condition, message):
    if not condition:
        raise ProxyError(message)


def classify_failure(error, phase):
    if isinstance(error, ProxyError):
        return error
    if isinstance(error, (urllib.error.URLError, TimeoutError, socket.timeout)):
        return ProxyError("Could not reach Codex backend or upstream timed out.", 504, "api_error")
    if phase == "upstream_stream":
        return ProxyError(f"Invalid or interrupted Codex response ({type(error).__name__}); retry the request.",
                          502, "api_error")
    if phase in {"request_decode", "request_translate"} and isinstance(error, (ValueError, KeyError, TypeError, AttributeError)):
        if isinstance(error, json.JSONDecodeError):
            return ProxyError(f"Invalid request JSON: {error.msg} at line {error.lineno}, column {error.colno}.")
        return ProxyError("Invalid Messages request structure.")
    return ProxyError("Internal proxy error.", 500, "api_error")


def validate_request(body):
    require(isinstance(body, dict), "Request must be a JSON object.")
    require(isinstance(body.get("messages"), list) and bool(body["messages"]), "messages must be a nonempty array.")
    for name in ("temperature", "top_p", "top_k"):
        if name in body:
            value = body[name]
            if name == "top_k":
                require(type(value) is int and value > 0, "top_k must be a positive integer.")
            else:
                require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1,
                        f"{name} must be a finite number between 0 and 1.")
            # Claude clients include these defaults. Codex's reasoning models do
            # not accept them; accept valid client input but do not forward it.
    if "context_management" in body:
        management = body["context_management"]
        require(isinstance(management, dict), "context_management must be an object.")
        edits = management.get("edits", [])
        require(isinstance(edits, list), "context_management.edits must be an array.")
        for edit in edits:
            require(isinstance(edit, dict) and edit.get("type") == "clear_thinking_20251015",
                    "Only clear_thinking context management is supported; tool clearing and server compaction are unavailable.")
            keep = edit.get("keep", "all")
            require(keep == "all" or (isinstance(keep, dict) and keep.get("type") == "thinking_turns"
                                     and type(keep.get("value")) is int and keep["value"] > 0),
                    "Invalid clear_thinking keep policy.")
        # Anthropic thinking/signatures are never sent upstream, so its thinking
        # retention policy has no effect on the GPT transcript.
    for name in ("mcp_servers", "container", "service_tier", "inference_geo", "fallbacks"):
        if name in body:
            raise ProxyError(f"{name} is unsupported by this Codex adapter.")
    if "model" in body:
        require(isinstance(body["model"], str) and bool(body["model"]), "model must be a nonempty string.")
    if "stream" in body:
        require(isinstance(body["stream"], bool), "stream must be a boolean.")
    if "max_tokens" in body:
        require(type(body["max_tokens"]) is int and body["max_tokens"] > 0, "max_tokens must be a positive integer.")
    for name in ("output_config", "thinking", "metadata"):
        if name in body:
            require(isinstance(body[name], dict), f"{name} must be an object.")
    if "stop_sequences" in body:
        require(isinstance(body["stop_sequences"], list) and all(isinstance(s, str) for s in body["stop_sequences"]), "stop_sequences must be an array of strings.")
    tools = body.get("tools", [])
    require(isinstance(tools, list), "tools must be an array.")
    names = set()
    for tool in tools:
        require(isinstance(tool, dict), "Each tool must be an object.")
        name = tool.get("name")
        require(isinstance(name, str) and bool(name) and name not in names, "Tool names must be nonempty and unique.")
        names.add(name)
        require(isinstance(tool.get("input_schema"), dict), "Tool input_schema must be an object.")
    choice = body.get("tool_choice", {"type": "auto"})
    require(isinstance(choice, dict), "tool_choice must be an object.")
    require(choice.get("type") in ("auto", "any", "none", "tool"), "Unsupported tool_choice type.")
    if "disable_parallel_tool_use" in choice:
        require(isinstance(choice["disable_parallel_tool_use"], bool), "disable_parallel_tool_use must be boolean.")
    if choice["type"] == "tool":
        require(isinstance(choice.get("name"), str) and choice["name"] in names, "tool_choice must name a declared tool.")
    if choice["type"] == "any":
        require(bool(tools), "tool_choice any requires tools.")
    for message in body["messages"]:
        require(isinstance(message, dict), "Each message must be an object.")
        content = message.get("content")
        require(isinstance(content, (str, list)), "message content must be text or blocks.")
        if isinstance(content, list):
            for block in content:
                require(isinstance(block, dict), "Each content block must be an object.")
                kind = block.get("type")
                if kind == "text":
                    require(isinstance(block.get("text"), str), "text must be a string.")
                elif kind == "tool_use":
                    require(all(isinstance(block.get(k), str) and block[k] for k in ("id", "name")), "tool_use requires id and name.")
                    require(isinstance(block.get("input"), dict), "tool_use input must be an object.")
                elif kind == "tool_result":
                    require(isinstance(block.get("tool_use_id"), str) and bool(block["tool_use_id"]), "tool_result requires tool_use_id.")
                    if "is_error" in block:
                        require(isinstance(block["is_error"], bool), "is_error must be boolean.")


def image_part(block):
    source = block.get("source", {})
    require(isinstance(source, dict), "image source must be an object.")
    if source.get("type") == "base64":
        require(source.get("media_type") in {"image/png", "image/jpeg", "image/gif", "image/webp"}, "Unsupported image media_type.")
        require(isinstance(source.get("data"), str) and bool(source["data"]), "image data must be a nonempty string.")
        url = f"data:{source['media_type']};base64,{source['data']}"
    elif source.get("type") == "url":
        url = source.get("url")
        require(isinstance(url, str) and urllib.parse.urlsplit(url).scheme in {"http", "https"}, "image URL must use HTTP or HTTPS.")
    else:
        raise ProxyError("Only base64 and URL image sources are supported.")
    return {"type": "input_image", "image_url": url}


def result_output(content):
    if isinstance(content, str):
        return content
    require(isinstance(content, list), "tool_result content must be text or blocks.")
    parts = []
    for block in content:
        require(isinstance(block, dict), "tool_result blocks must be objects.")
        if block.get("type") == "text":
            require(isinstance(block.get("text"), str), "tool_result text must be a string.")
            parts.append({"type": "input_text", "text": block["text"]})
        elif block.get("type") == "image":
            parts.append(image_part(block))
        else:
            raise ProxyError(f"Unsupported tool result block: {block.get('type')}")
    return parts or ""


def translate_request(body, model, effort="low"):
    """Preserve message/tool ordering; never forward Anthropic credentials."""
    validate_request(body)
    system = body.get("system", "")
    if isinstance(system, list):
        if any(not isinstance(b, dict) or b.get("type") != "text" or not isinstance(b.get("text"), str) for b in system):
            raise ProxyError("Only text system blocks are supported.")
        system = "\n\n".join(b["text"] for b in system)
    if not isinstance(system, str):
        raise ProxyError("system must be text or text blocks.")
    items = []
    for message in body["messages"]:
        role = message.get("role")
        if role not in ("user", "assistant"):
            raise ProxyError("Messages must have user or assistant roles.")
        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            raise ProxyError("message content must be text or blocks.")
        pending = []

        def flush():
            if pending:
                items.append({"role": role, "content": list(pending)})
                pending.clear()

        for block in content:
            kind = block.get("type")
            if kind == "text":
                if block.get("text"):
                    pending.append({"type": "output_text" if role == "assistant" else "input_text", "text": block["text"]})
            elif kind == "image" and role == "user":
                pending.append(image_part(block))
            elif kind == "tool_use" and role == "assistant":
                flush()
                items.append({"type": "function_call", "call_id": block["id"],
                              "name": block["name"], "arguments": compact(block.get("input", {}))})
            elif kind == "tool_result" and role == "user":
                flush()
                output = result_output(block.get("content", ""))
                if block.get("is_error"):
                    if isinstance(output, str):
                        output = "Tool execution failed: " + output
                    else:
                        output.insert(0, {"type": "input_text", "text": "Tool execution failed:"})
                items.append({"type": "function_call_output", "call_id": block["tool_use_id"], "output": output})
            elif kind in ("thinking", "redacted_thinking") and role == "assistant":
                # Anthropic thinking signatures cannot be replayed to a different model.
                continue
            else:
                raise ProxyError(f"Unsupported {role} content block: {kind}")
        flush()
    request = {"model": model, "instructions": system or "You are a helpful assistant.",
               "input": items, "stream": True, "store": False,
               "reasoning": {"effort": effort}}
    tools = []
    for tool in body.get("tools") or []:
        if tool.get("type") not in {None, "custom"}:
            raise ProxyError(f"Server-side tool type is unsupported: {tool.get('type')}. Use client tools.")
        tools.append({"type": "function", "name": tool["name"],
                      "description": tool.get("description", ""),
                      "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                      "strict": False})
    if tools:
        request["tools"] = tools
        choice = body.get("tool_choice") or {"type": "auto"}
        kind = choice.get("type")
        if kind == "tool":
            request["tool_choice"] = {"type": "function", "name": choice["name"]}
        elif kind in {"auto", "any", "none"}:
            request["tool_choice"] = {"auto": "auto", "any": "required", "none": "none"}[kind]
        else:
            raise ProxyError(f"Unsupported tool_choice: {kind}")
        request["parallel_tool_calls"] = not choice.get("disable_parallel_tool_use", False)
    if body.get("stop_sequences"):
        raise ProxyError("stop_sequences is not supported by this Codex backend.")
    output_format = (body.get("output_config") or {}).get("format") or body.get("output_format")
    if output_format:
        require(isinstance(output_format, dict), "output format must be an object.")
        require(isinstance(output_format.get("schema"), dict), "output format schema must be an object.")
        if output_format.get("type") != "json_schema":
            raise ProxyError("Only json_schema output format is supported.")
        request["text"] = {"format": {"type": "json_schema", "name": "response",
                                       "schema": output_format["schema"], "strict": True}}
    return request


def resolve_options(body, config):
    requested = body.get("model") or config["model"]
    models = config.get("models", [config["model"]])
    aliases = {"sonnet": models[0], "haiku": models[0], "opus": models[-1],
               "claude-sonnet-4-5": models[0], "claude-opus-4-5": models[-1]}
    model = aliases.get(requested, requested)
    if model not in models:
        raise ProxyError("Unknown model. Select a model listed by /v1/models.")
    effort = (body.get("output_config") or {}).get("effort", config["reasoning_effort"])
    mapping = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
    require(isinstance(effort, str) and effort in mapping, "Unsupported effort. Use low, medium, high, xhigh or max.")
    return model, mapping[effort]


def read_credentials(auth_file):
    try:
        auth = json.loads(auth_file.read_text(encoding="utf-8"))
        if auth.get("auth_mode") != "chatgpt":
            raise ValueError("not ChatGPT auth")
        token = auth["tokens"]["access_token"]
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=" * (-len(token.split(".")[1]) % 4)))
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        raise ProxyError("Cannot read Codex ChatGPT credentials. Sign in with Codex first.", 503, "authentication_error") from None
    if claims.get("exp", 0) < time.time() + 10:
        raise ProxyError("Codex access token expired. Refresh your login in Codex, then retry; the proxy rereads auth.json automatically.", 503, "authentication_error")
    account = claims.get("https://api.openai.com/auth", {})
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "text/event-stream", "User-Agent": "ClaudeCodexLocalProxy/1.0",
               "originator": "claude-codex-local-proxy"}
    account_id = auth["tokens"].get("account_id") or account.get("chatgpt_account_id")
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    residency = account.get("chatgpt_data_residency") or account.get("chatgpt_compute_residency")
    if residency:
        headers["x-openai-internal-codex-residency"] = residency
    return headers


def upstream_events(response):
    """Parse SSE data fields, including multi-line events and heartbeat comments."""
    def parse(value):
        try:
            event = json.loads(value)
        except json.JSONDecodeError as error:
            raise ProxyError(f"Invalid upstream SSE JSON: {error.msg} at line {error.lineno}, column {error.colno}.",
                             502, "api_error") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ProxyError("Invalid upstream SSE event: expected an object with a type.", 502, "api_error")
        return event

    data = []
    for raw in response:
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            raise ProxyError("Upstream SSE contained invalid UTF-8.", 502, "api_error") from None
        if not line:
            if data:
                value = "\n".join(data)
                data.clear()
                if value != "[DONE]":
                    yield parse(value)
            else:
                yield {"type": "proxy.ping"}
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data and "\n".join(data) != "[DONE]":
        yield parse("\n".join(data))


class MessageAdapter:
    """Incremental Responses -> Anthropic event conversion and final JSON assembly."""
    def __init__(self, model):
        self.id = "msg_" + uuid.uuid4().hex
        self.model = model
        self.blocks = []
        self.active = None
        self.seen = set()
        self.text_by_output = {}
        self.text_received = {}
        self.tool_items = {}
        self.pending_tool_events = []
        self.completed = False
        self.reason = "end_turn"
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def message(self):
        return {"id": self.id, "type": "message", "role": "assistant", "model": self.model,
                "content": self.blocks, "stop_reason": self.reason, "stop_sequence": None, "usage": self.usage}

    def start(self):
        msg = self.message()
        msg.update(content=[], stop_reason=None)
        return {"type": "message_start", "message": msg}

    def close_block(self):
        if self.active is not None:
            index, self.active = self.active, None
            return [{"type": "content_block_stop", "index": index}]
        return []

    def text_delta(self, key, text):
        if not text:
            return []
        events = []
        if key not in self.text_by_output:
            events += self.close_block()
            self.active = len(self.blocks)
            self.text_by_output[key] = self.active
            self.blocks.append({"type": "text", "text": ""})
            events.append({"type": "content_block_start", "index": self.active,
                           "content_block": {"type": "text", "text": ""}})
        index = self.text_by_output[key]
        if self.active != index:
            events += self.close_block()
            self.active = len(self.blocks)
            self.blocks.append({"type": "text", "text": ""})
            self.text_by_output[key] = self.active
            index = self.active
            events.append({"type": "content_block_start", "index": index,
                           "content_block": {"type": "text", "text": ""}})
        self.blocks[index]["text"] += text
        self.text_received[key] = self.text_received.get(key, "") + text
        events.append({"type": "content_block_delta", "index": index,
                       "delta": {"type": "text_delta", "text": text}})
        return events

    def item_done(self, item, key):
        if not isinstance(item, dict):
            raise ProxyError("Invalid upstream output item: expected an object.", 502, "api_error")
        identity = item.get("id") or key
        if identity in self.seen:
            return []
        self.seen.add(identity)
        events = []
        if item.get("type") == "message":
            for content_index, part in enumerate(item.get("content") or []):
                text_key = (key, content_index)
                text = part.get("text", part.get("refusal", ""))
                if text_key not in self.text_by_output:
                    events += self.text_delta(text_key, text)
                else:
                    current = self.text_received[text_key]
                    if not text.startswith(current):
                        raise ProxyError("Upstream text conflicts with streamed content.", 502, "api_error")
                    events += self.text_delta(text_key, text[len(current):])
                if self.active == self.text_by_output.get(text_key):
                    events += self.close_block()
        elif item.get("type") == "function_call":
            events += self.close_block()
            buffered = self.tool_items.get(key, {})
            arguments = item.get("arguments")
            if arguments is None:
                arguments = buffered.get("arguments", "")
            if not isinstance(arguments, str) or not arguments:
                raise ProxyError("Upstream returned missing tool arguments.", 502, "api_error")
            partial = buffered.get("arguments", "")
            if partial and not arguments.startswith(partial):
                raise ProxyError("Upstream tool arguments conflict with deltas.", 502, "api_error")
            item = {**buffered, **item}
            if not all(isinstance(item.get(k), str) and item[k] for k in ("call_id", "name")):
                raise ProxyError("Upstream returned invalid tool identity.", 502, "api_error")
            try:
                value = json.loads(arguments)
                if not isinstance(value, dict):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ProxyError("Upstream returned invalid tool arguments.", 502, "api_error") from None
            index = len(self.blocks)
            block = {"type": "tool_use", "id": item["call_id"], "name": item["name"], "input": value}
            self.blocks.append(block)
            self.tool_items[key] = {**item, "arguments": arguments}
            events += [
                {"type": "content_block_start", "index": index, "content_block": {**block, "input": {}}},
                {"type": "content_block_delta", "index": index,
                 "delta": {"type": "input_json_delta", "partial_json": arguments}},
                {"type": "content_block_stop", "index": index},
            ]
        return events

    def feed(self, event):
        if self.completed:
            return []
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ProxyError("Invalid upstream event: expected an object with a type.", 502, "api_error")
        kind = event.get("type")
        key = event.get("output_index", 0)
        if kind == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                self.tool_items[key] = dict(item)
            return []
        if kind == "response.function_call_arguments.delta":
            item = self.tool_items.setdefault(key, {})
            item["arguments"] = item.get("arguments", "") + event.get("delta", "")
            return []
        if kind == "response.function_call_arguments.done":
            item = self.tool_items.setdefault(key, {})
            arguments = event.get("arguments")
            if arguments is None:
                arguments = item.get("arguments", "")
            if not isinstance(arguments, str):
                raise ProxyError("Upstream returned non-string tool arguments.", 502, "api_error")
            if not arguments.startswith(item.get("arguments", "")):
                raise ProxyError("Upstream tool arguments conflict with deltas.", 502, "api_error")
            item["arguments"] = arguments
            return []
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            converted = self.text_delta((key, event.get("content_index", 0)), event.get("delta", ""))
            if self.pending_tool_events:
                self.pending_tool_events += converted
                return []
            return converted
        if kind == "response.output_item.done":
            converted = self.item_done(event["item"], key)
            if self.pending_tool_events or event["item"].get("type") == "function_call":
                self.pending_tool_events += converted
                return []
            return converted
        if kind in {"error", "response.failed"}:
            details = upstream_error_details(event.get("response") or event)
            raise ProxyError(f"Codex stream failed: {details}", 502, "api_error")
        if kind in {"response.completed", "response.incomplete"}:
            response = event.get("response") or {}
            if response.get("status") in {"failed", "cancelled"}:
                raise ProxyError("Codex response did not complete successfully.", 502, "api_error")
            unfinished = [index for index, item in self.tool_items.items()
                          if (item.get("id") or index) not in self.seen]
            output = response.get("output") or []
            final_ids = {item.get("id") or index for index, item in enumerate(output)}
            if any((self.tool_items[index].get("id") or index) not in final_ids for index in unfinished):
                raise ProxyError("Upstream completed with unfinished tool calls.", 502, "api_error")
            events = []
            for index, item in enumerate(output):
                events += self.item_done(item, index)
            events += self.close_block()
            usage = response.get("usage") or {}
            total = usage.get("input_tokens", 0)
            cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
            if any(type(n) is not int or n < 0 for n in (total, cached, usage.get("output_tokens", 0))) or cached > total:
                raise ProxyError("Upstream returned invalid usage.", 502, "api_error")
            self.usage = {"input_tokens": total - cached, "output_tokens": usage.get("output_tokens", 0),
                          "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}
            if kind == "response.incomplete" or response.get("status") == "incomplete":
                if (response.get("incomplete_details") or {}).get("reason") != "max_output_tokens":
                    raise ProxyError("Codex response incomplete for a non-token-limit reason.", 502, "api_error")
                if any(b["type"] == "tool_use" for b in self.blocks) or self.tool_items:
                    raise ProxyError("Codex response truncated during tool generation.", 502, "api_error")
                self.reason = "max_tokens"
            elif any(b["type"] == "tool_use" for b in self.blocks):
                self.reason = "tool_use"
            if not self.blocks:
                raise ProxyError("Codex returned no text or tool calls.", 502, "api_error")
            self.completed = True
            events = self.pending_tool_events + events
            self.pending_tool_events = []
            events += [{"type": "message_delta", "delta": {"stop_reason": self.reason, "stop_sequence": None},
                        "usage": self.usage}, {"type": "message_stop"}]
            return events
        if kind == "proxy.ping":
            return [{"type": "ping"}]
        return []


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, config, auth_file):
        self.config, self.auth_file = config, auth_file
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClaudeCodexProxy/1.0"

    def handle_one_request(self):
        self.request_id = "req_" + uuid.uuid4().hex
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            LOG.info("connection closed by client request_id=%s", self.request_id)
            self.close_connection = True

    def end_headers(self):
        self.send_header("request-id", self.request_id)
        self.send_header("x-proxy-compatibility", "codex-subscription; token-count=approximate; tool-input=buffered; sampling=ignored")
        super().end_headers()

    def log_message(self, *_):
        pass  # Never log prompts, headers, tokens, or full query strings.

    def json_response(self, status, body, extra_headers=None):
        raw = compact(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def error_response(self, error):
        self.json_response(error.status, {"type": "error", "error": {"type": error.kind, "message": str(error)},
                                          "request_id": self.request_id}, getattr(error, "headers", None))

    def authorized(self):
        value = self.headers.get("Authorization", "")
        token = value[7:] if value.startswith("Bearer ") else self.headers.get("x-api-key", "")
        return hmac.compare_digest(token.encode(), self.server.config["local_token"].encode())

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self.json_response(200, {"status": "ok", "service": "claude-codex-proxy",
                                     "upstream_model": self.server.config["model"]})
        elif path == "/v1/models" and self.authorized():
            model = self.server.config["model"]
            # Desktop discovery filters out non-Claude IDs. This is a routing alias,
            # clearly labelled with the real upstream, not a claim to serve Claude.
            alias = "claude-sonnet-4-5"
            models = [
                {"id": alias, "type": "model", "display_name": f"GPT subscription — {model}",
                 "description": f"Local compatibility alias. Actual upstream: {model} (OpenAI subscription).",
                 "created_at": "2026-01-01T00:00:00Z",
                 "anthropic_family_tier": "sonnet", "is_family_default": True},
                {"id": model, "type": "model", "display_name": model,
                 "created_at": "2026-01-01T00:00:00Z"},
            ]
            configured = self.server.config.get("models", [model])
            models += [{"id": name, "type": "model", "display_name": name,
                        "created_at": "2026-01-01T00:00:00Z"} for name in configured if name != model]
            if len(configured) > 1:
                models.insert(1, {"id": "claude-opus-4-5", "type": "model",
                                  "display_name": f"GPT subscription — {configured[-1]}",
                                  "description": f"Compatibility alias. Actual upstream: {configured[-1]}",
                                  "created_at": "2026-01-01T00:00:00Z"})
            # Claude Desktop builds these into its model picker; omit them and the
            # effort selector never renders (max_effort_level -> maxEffort) and the
            # window budget falls back to a built-in default.
            effort_level = self.server.config.get("max_effort_level", "xhigh")
            context = int(self.server.config["context_window"])
            for entry in models:
                entry["max_effort_level"] = effort_level
                entry["max_input_tokens"] = context
            self.json_response(200, {"data": models, "has_more": False,
                                     "first_id": alias, "last_id": models[-1]["id"]})
        else:
            self.error_response(ProxyError("Not found." if self.authorized() else "Invalid local proxy token.",
                                           404 if self.authorized() else 401,
                                           "not_found_error" if self.authorized() else "authentication_error"))

    def emit(self, event):
        self.wfile.write(f"event: {event['type']}\ndata: {compact(event)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def do_POST(self):
        streaming_started = False
        started = time.monotonic()
        phase = "request_decode"
        path = urllib.parse.urlsplit(self.path).path
        try:
            self.connection.settimeout(30)
            if not self.authorized():
                raise ProxyError("Invalid local proxy token.", 401, "authentication_error")
            if path not in {"/v1/messages", "/v1/messages/count_tokens"}:
                raise ProxyError("Not found.", 404, "not_found_error")
            if self.headers.get("Transfer-Encoding"):
                raise ProxyError("Chunked request bodies are unsupported.")
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= MAX_BODY:
                raise ProxyError("Request must be between 1 byte and 32 MiB.", 413, "request_too_large")
            body = json.loads(self.rfile.read(size))
            phase = "request_translate"
            config = self.server.config
            validate_request(body)
            model, effort = resolve_options(body, config)
            request_body = translate_request(body, model, effort)
            ignored = [name for name in ("temperature", "top_p", "top_k") if name in body]
            if ignored:
                LOG.info("compatibility request_id=%s sampling_parameters_ignored=%s", self.request_id, ",".join(ignored))
            if path.endswith("/count_tokens"):
                # Different tokenizer: explicitly mark the local planning estimate.
                count = max(1, math.ceil(len(compact(request_body).encode("utf-8")) / 3))
                self.json_response(200, {"input_tokens": count}, {"x-token-count-approximate": "true"})
                return
            phase = "credentials"
            headers = read_credentials(self.server.auth_file)
            request = urllib.request.Request(UPSTREAM, data=compact(request_body).encode("utf-8"), headers=headers, method="POST")
            try:
                phase = "upstream_connect"
                upstream = urllib.request.urlopen(request, timeout=config["upstream_timeout_seconds"])
            except urllib.error.HTTPError as error:
                retry_after = error.headers.get("Retry-After", "")
                upstream_id = safe_error_text(error.headers.get("x-request-id", "unknown"))
                try:
                    details = upstream_error_details(json.loads(error.read(65536)))
                except (ValueError, OSError):
                    details = "Error body unavailable or non-JSON (body omitted)."
                finally:
                    error.close()
                LOG.warning("upstream HTTP failure request_id=%s upstream_request_id=%s upstream_status=%d details=%s",
                            self.request_id, upstream_id, error.code, details)
                kind = "rate_limit_error" if error.code == 429 else "api_error"
                status = 429 if error.code == 429 else 502
                if error.code in {401, 403}:
                    status, kind = 503, "authentication_error"
                elif error.code in {502, 503, 504, 529}:
                    status, kind = 529, "overloaded_error"
                hint = " Refresh the ChatGPT login in Codex." if error.code == 401 else ""
                failure = ProxyError(f"Codex backend HTTP {error.code}: {details}.{hint}", status, kind)
                if retry_after.isascii() and retry_after.isdecimal() and len(retry_after) <= 8:
                    failure.headers = {"Retry-After": retry_after}
                raise failure from None
            with upstream:
                phase = "upstream_stream"
                adapter = MessageAdapter(body.get("model") or config["model"])
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.send_header("X-Accel-Buffering", "no")
                    self.send_header("x-upstream-model", model)
                    self.end_headers()
                    self.close_connection = True
                    streaming_started = True
                    self.emit(adapter.start())
                try:
                    for event in upstream_events(upstream):
                        if not isinstance(event, dict):
                            raise ProxyError("Codex returned an invalid stream event.", 502, "api_error")
                        for converted in adapter.feed(event):
                            if streaming_started:
                                self.emit(converted)
                        if adapter.completed:
                            break
                except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
                    raise classify_failure(error, phase) from error
                if not adapter.completed:
                    raise ProxyError("Upstream stream disconnected before completion; retry the request.", 502, "api_error")
                if not streaming_started:
                    self.json_response(200, adapter.message(), {"x-upstream-model": model})
                LOG.info("message completed request_id=%s model=%s input=%d output=%d stop=%s seconds=%.1f",
                         self.request_id, model, adapter.usage["input_tokens"], adapter.usage["output_tokens"], adapter.reason,
                         time.monotonic() - started)
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("client disconnected request_id=%s seconds=%.1f", self.request_id, time.monotonic() - started)
        except Exception as error:
            original_error = error.__cause__ or error
            frames = traceback.extract_tb(original_error.__traceback__)
            location = " > ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames[-5:])
            exception_type = type(original_error).__name__
            error = classify_failure(error, phase)
            LOG.warning("request failed request_id=%s phase=%s exception=%s location=%s status=%d kind=%s message=%s seconds=%.1f",
                        self.request_id, phase, exception_type, location, error.status, error.kind,
                        safe_error_text(error), time.monotonic() - started)
            try:
                if streaming_started:
                    self.emit({"type": "error", "error": {"type": error.kind, "message": str(error)},
                               "request_id": self.request_id})
                else:
                    self.error_response(error)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True


def initialize(config_path):
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    else:
        config = {"port": 8787, "model": "gpt-6.1-sol", "reasoning_effort": "low",
                  "upstream_timeout_seconds": 300, "local_token": "local-" + secrets.token_urlsafe(24)}
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    if not config.get("local_token") or not config.get("model"):
        raise ValueError("config must contain model and local_token")
    models = config.get("models", ["gpt-6.1-sol", "gpt-6.1-terra"])
    if not isinstance(models, list) or not models or any(not isinstance(m, str) or not m for m in models) or config["model"] not in models:
        raise ValueError("models must list supported IDs and contain the default model")
    context = config.get("context_window", 400000)
    if type(context) is not int or context <= 0:
        raise ValueError("context_window must be a positive integer")
    settings = {"effortLevel": config["reasoning_effort"], "env": {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{config['port']}",
        "ANTHROPIC_AUTH_TOKEN": config["local_token"], "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": config["model"],
        "ANTHROPIC_DEFAULT_OPUS_MODEL": models[-1],
        "ANTHROPIC_DEFAULT_SONNET_MODEL": models[0],
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": models[0],
        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": models[-1],
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": models[0],
        "ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES": "effort,xhigh_effort,max_effort",
        "ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES": "effort,xhigh_effort,max_effort",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(context),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ENABLE_TOOL_SEARCH": "false",
    }}
    (config_path.parent / "claude-settings.json").write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--init", action="store_true", help="Generate local config and Claude settings, then exit")
    args = parser.parse_args()
    config = initialize(args.config)
    if args.init:
        print("Created config.json and claude-settings.json (no subscription token copied).")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), RotatingFileHandler(
                            args.config.parent / "diagnostics.log", maxBytes=2 * 1024 * 1024,
                            backupCount=3, encoding="utf-8")])
    auth_file = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    server = ProxyServer(("127.0.0.1", int(config["port"])), config, auth_file)
    LOG.info("listening http://127.0.0.1:%d upstream_model=%s", config["port"], config["model"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
