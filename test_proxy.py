import io
import json
import unittest
import http.client
import threading
import urllib.error
from pathlib import Path
from unittest.mock import patch

import proxy

from proxy import MessageAdapter, ProxyError, translate_request, upstream_events


class TranslationTests(unittest.TestCase):
    def test_tool_roundtrip_preserves_call_ids_and_order(self):
        request = translate_request({"messages": [
            {"role": "assistant", "content": [
                {"type": "text", "text": "Reading"},
                {"type": "tool_use", "id": "call_abc", "name": "Read", "input": {"path": "a.txt"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_abc", "content": "hello"},
                {"type": "text", "text": "Summarize it"}]}],
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": "Read"}}, "gpt-test")
        items = request["input"]
        self.assertEqual([i.get("type", i.get("role")) for i in items],
                         ["assistant", "function_call", "function_call_output", "user"])
        self.assertEqual(items[1]["call_id"], items[2]["call_id"])
        self.assertEqual(json.loads(items[1]["arguments"]), {"path": "a.txt"})
        self.assertEqual(request["tool_choice"], {"type": "function", "name": "Read"})

    def test_text_image_and_thinking(self):
        request = translate_request({"system": [{"type": "text", "text": "System"}], "messages": [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "private", "signature": "abc"}]},
            {"role": "user", "content": [{"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": "ABC"}}]}]}, "gpt-test")
        self.assertEqual(request["instructions"], "System")
        self.assertEqual(request["input"][0]["content"][0]["image_url"], "data:image/png;base64,ABC")
        self.assertNotIn("private", json.dumps(request))

    def test_unsupported_blocks_are_not_silently_dropped(self):
        with self.assertRaises(ProxyError):
            translate_request({"messages": [{"role": "user", "content": [{"type": "document"}]}]}, "gpt-test")

    def test_mid_conversation_system_message_folds_into_instructions(self):
        # Claude Desktop sends reminders as role "system" inside messages, which
        # the Messages schema forbids; they must not be dropped.
        request = translate_request({"system": "Base", "messages": [
            {"role": "user", "content": "hello"},
            {"role": "system", "content": [{"type": "text", "text": "Reminder"}]}]}, "gpt-test")
        self.assertEqual(request["instructions"], "Base\n\nReminder")
        self.assertEqual([item["role"] for item in request["input"]], ["user"])


class StreamTests(unittest.TestCase):
    def test_sse_parser_handles_comments_and_multiline_data(self):
        data = b': heartbeat\n\ndata: {"type":\ndata: "proxy.ping"}\n\ndata: [DONE]\n\n'
        self.assertEqual(list(upstream_events(io.BytesIO(data))),
                         [{"type": "proxy.ping"}, {"type": "proxy.ping"}])

    def test_text_and_parallel_tools_are_balanced_and_not_duplicated(self):
        adapter = MessageAdapter("alias")
        message = {"id": "m1", "type": "message", "content": [{"type": "output_text", "text": "Hi"}]}
        tool1 = {"id": "fc1", "type": "function_call", "call_id": "call_1", "name": "Read", "arguments": '{"path":"a"}'}
        tool2 = {"id": "fc2", "type": "function_call", "call_id": "call_2", "name": "Read", "arguments": '{"path":"b"}'}
        events = [adapter.start()]
        for event in [
            {"type": "response.output_text.delta", "output_index": 0, "delta": "Hi"},
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.output_item.done", "output_index": 1, "item": tool1},
            {"type": "response.output_item.done", "output_index": 2, "item": tool2},
            {"type": "response.completed", "response": {"status": "completed", "output": [message, tool1, tool2],
                "usage": {"input_tokens": 23, "output_tokens": 7}}}]:
            events += adapter.feed(event)
        self.assertEqual(len(adapter.blocks), 3)
        self.assertEqual(adapter.reason, "tool_use")
        self.assertEqual(adapter.usage["output_tokens"], 7)
        starts = [e["index"] for e in events if e["type"] == "content_block_start"]
        stops = [e["index"] for e in events if e["type"] == "content_block_stop"]
        self.assertEqual(starts, [0, 1, 2])
        self.assertEqual(stops, starts)
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_null_terminal_output_keeps_done_items(self):
        adapter = MessageAdapter("test")
        adapter.feed({"type": "response.output_item.done", "output_index": 0, "item": {
            "id": "m1", "type": "message", "content": [{"type": "output_text", "text": "Answer"}]}})
        adapter.feed({"type": "response.completed", "response": {"output": None}})
        self.assertTrue(adapter.completed)
        self.assertEqual(adapter.blocks, [{"type": "text", "text": "Answer"}])

    def test_failed_and_invalid_tools_are_errors(self):
        adapter = MessageAdapter("test")
        with self.assertRaises(ProxyError):
            adapter.feed({"type": "response.failed"})
        with self.assertRaises(ProxyError):
            adapter.feed({"type": "response.output_item.done", "item": {
                "type": "function_call", "call_id": "c", "name": "X", "arguments": "{"}})
        self.assertFalse(adapter.completed)


class ModelOptionsTests(unittest.TestCase):
    def test_models_and_effort(self):
        config = {"model": "gpt-6.1-sol", "models": ["gpt-6.1-sol", "gpt-6.1-terra"], "reasoning_effort": "low"}
        for name, expected in [("sonnet", "gpt-6.1-sol"), ("opus", "gpt-6.1-terra"),
                               ("gpt-6.1-terra", "gpt-6.1-terra"), ("claude-sonnet-4-5", "gpt-6.1-sol")]:
            self.assertEqual(proxy.resolve_options({"model": name, "output_config": {"effort": "high"}}, config), (expected, "high"))
        self.assertEqual(proxy.resolve_options({"output_config": {"effort": "max"}}, config), ("gpt-6.1-sol", "xhigh"))
        for body in [{"model": "unknown"}, {"output_config": {"effort": "invalid"}}]:
            with self.assertRaises(ProxyError):
                proxy.resolve_options(body, config)

    def test_catalogue_ids_route_by_family(self):
        # Claude Desktop only knows these ids through its signed model catalogue;
        # it sends them (sometimes with a "[1m]" suffix) once the picker uses them.
        config = {"model": "gpt-6.1-sol", "models": ["gpt-6.1-sol", "gpt-6.1-terra"], "reasoning_effort": "low"}
        for name, expected in [("claude-opus-5-5", "gpt-6.1-terra"),
                               ("claude-sonnet-5-5", "gpt-6.1-sol"),
                               ("claude-haiku-5-5", "gpt-6.1-sol"),
                               ("claude-fable-5-1", "gpt-6.1-terra"),
                               ("claude-opus-5-5[1m]", "gpt-6.1-terra")]:
            self.assertEqual(proxy.resolve_options({"model": name}, config)[0], expected)


class ValidationTests(unittest.TestCase):
    def test_invalid_requests(self):
        base = {"messages": [{"role": "user", "content": "hi"}]}
        invalid = [None, [], {}, {"messages": []}, {"messages": [None]},
                   {"messages": [{"role": "tool", "content": "hi"}]},
                   {"messages": [{"role": "user", "content": 1}]}]
        for key, values in {
            "stream": [1, "true", None], "max_tokens": [True, 0, -1, 1.5, "2"],
            "tools": [None, {}, [None], [{"name": "X", "input_schema": []}],
                      [{"name": "X", "input_schema": {}}] * 2],
            "tool_choice": [None, "auto", {}, {"type": "bad"},
                            {"type": "any"}, {"type": "tool", "name": "missing"},
                            {"type": "auto", "disable_parallel_tool_use": 1}],
            "output_config": [None, []], "thinking": [False], "metadata": ["x"],
            "stop_sequences": ["stop", [1], ["stop"]], "system": [1],
        }.items():
            invalid.extend(dict(base, **{key: value}) for value in values)
        for block in [{"type": "text", "text": 3}, {"type": "tool_use", "id": "c", "name": "X", "input": []},
                      {"type": "tool_result", "tool_use_id": ""},
                      {"type": "tool_result", "tool_use_id": "c", "is_error": 1}, None]:
            invalid.append({"messages": [{"role": "user", "content": [block]}]})
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ProxyError):
                translate_request(body, "test")

    def test_explicitly_unsupported_options(self):
        for name, value in {"mcp_servers": [], "container": {},
                            "service_tier": "auto", "inference_geo": "us", "fallbacks": []}.items():
            with self.subTest(name=name), self.assertRaises(ProxyError):
                translate_request({"messages": [{"role": "user", "content": "hi"}], name: value}, "test")

    def test_claude_thinking_retention_policy(self):
        base = {"messages": [{"role": "user", "content": "hi"}]}
        for keep in ("all", {"type": "thinking_turns", "value": 1}):
            result = translate_request(dict(base, context_management={"edits": [
                {"type": "clear_thinking_20251015", "keep": keep}]}), "test")
            self.assertNotIn("context_management", result)
        with self.assertRaises(ProxyError):
            translate_request(dict(base, context_management={"edits": [{"type": "clear_tool_uses_20250919"}]}), "test")

    def test_client_sampling_defaults_accepted_but_not_forwarded(self):
        base = {"messages": [{"role": "user", "content": "hi"}]}
        request = translate_request(dict(base, temperature=0.5, top_p=1, top_k=40), "test")
        for name in ("temperature", "top_p", "top_k"):
            self.assertNotIn(name, request)
        for name, value in [("temperature", True), ("temperature", float("nan")),
                            ("top_p", 2), ("top_k", 0)]:
            with self.subTest(name=name, value=value), self.assertRaises(ProxyError):
                translate_request(dict(base, **{name: value}), "test")

    def test_tool_choices_and_types(self):
        base = {"messages": [{"role": "user", "content": "hi"}],
                "tools": [{"name": "Read", "input_schema": {}}]}
        for choice, expected in [("auto", "auto"), ("any", "required"), ("none", "none"),
                                 ("tool", {"type": "function", "name": "Read"})]:
            with self.subTest(choice=choice):
                result = translate_request(dict(base, tool_choice={"type": choice, "name": "Read",
                                           "disable_parallel_tool_use": True}), "test")
                self.assertEqual(result["tool_choice"], expected)
                self.assertFalse(result["parallel_tool_calls"])
        for kind in ["web_search", "computer_20250124"]:
            with self.subTest(kind=kind), self.assertRaises(ProxyError):
                translate_request(dict(base, tools=[dict(base["tools"][0], type=kind)]), "test")


class AdapterRegressionTests(unittest.TestCase):
    def test_two_text_content_indices(self):
        adapter = MessageAdapter("test")
        events = []
        for index, text in enumerate(["first", "second"]):
            events += adapter.feed({"type": "response.output_text.delta", "output_index": 0,
                                    "content_index": index, "delta": text})
        events += adapter.feed({"type": "response.completed", "response": {"output": [{
            "id": "m", "type": "message", "content": [
                {"type": "output_text", "text": "first"}, {"type": "output_text", "text": "second"}]}]}})
        self.assertEqual(adapter.blocks, [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}])
        self.assertEqual([e["index"] for e in events if e["type"] == "content_block_stop"], [0, 1])

    def test_terminal_text_suffix_and_duplicate_completed(self):
        adapter = MessageAdapter("test")
        adapter.feed({"type": "response.output_text.delta", "delta": "Hel"})
        terminal = {"type": "response.completed", "response": {"output": [{"type": "message", "id": "m",
                    "content": [{"type": "output_text", "text": "Hello"}]}]}}
        events = adapter.feed(terminal)
        self.assertEqual([e["delta"]["text"] for e in events if e["type"] == "content_block_delta"], ["lo"])
        self.assertEqual(adapter.blocks[0]["text"], "Hello")
        self.assertEqual(adapter.feed(terminal), [])
        self.assertEqual(adapter.feed({"type": "response.output_text.delta", "delta": "extra"}), [])

    def test_tool_arguments_buffer_until_completed(self):
        adapter = MessageAdapter("test")
        item = {"type": "function_call", "id": "fc", "call_id": "c", "name": "Read", "arguments": ""}
        for event in [
            {"type": "response.output_item.added", "item": item},
            {"type": "response.function_call_arguments.delta", "delta": '{"path":'},
            {"type": "response.function_call_arguments.delta", "delta": '"a"}'},
            {"type": "response.function_call_arguments.done", "arguments": '{"path":"a"}'},
            {"type": "response.output_item.done", "item": dict(item, arguments='{"path":"a"}')}]:
            self.assertEqual(adapter.feed(event), [])
        events = adapter.feed({"type": "response.completed", "response": {"output": [dict(item, arguments='{"path":"a"}')]}})
        self.assertEqual([e["type"] for e in events], ["content_block_start", "content_block_delta",
                         "content_block_stop", "message_delta", "message_stop"])
        self.assertEqual(adapter.blocks[0]["input"], {"path": "a"})
        self.assertEqual(adapter.reason, "tool_use")

    def test_tool_done_without_arguments_retains_buffer(self):
        adapter = MessageAdapter("test")
        tool = {"type": "function_call", "id": "fc", "call_id": "c", "name": "Read"}
        for event in [
            {"type": "response.output_item.added", "item": tool},
            {"type": "response.function_call_arguments.delta", "delta": '{"path":"a"}'},
            {"type": "response.output_item.done", "item": tool},
        ]:
            self.assertEqual(adapter.feed(event), [])
        events = adapter.feed({"type": "response.completed", "response": {"output": [tool]}})
        self.assertEqual(adapter.blocks[0]["input"], {"path": "a"})
        self.assertEqual(events[1]["delta"]["partial_json"], '{"path":"a"}')
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_tool_then_text_events_remain_balanced(self):
        adapter = MessageAdapter("test")
        tool = {"type": "function_call", "id": "fc", "call_id": "c", "name": "Read", "arguments": "{}"}
        message = {"type": "message", "id": "m", "content": [{"type": "output_text", "text": "Done"}]}
        self.assertEqual(adapter.feed({"type": "response.output_item.done", "output_index": 0, "item": tool}), [])
        events = adapter.feed({"type": "response.output_text.delta", "output_index": 1,
                               "content_index": 0, "delta": "Done"})
        events += adapter.feed({"type": "response.completed", "response": {"output": [tool, message]}})
        active = None
        for event in events:
            if event["type"] == "content_block_start":
                self.assertIsNone(active)
                active = event["index"]
            elif event["type"] == "content_block_delta":
                self.assertEqual(event["index"], active)
            elif event["type"] == "content_block_stop":
                self.assertEqual(event["index"], active)
                active = None
        self.assertIsNone(active)
        self.assertEqual(adapter.blocks, [{"type": "tool_use", "id": "c", "name": "Read", "input": {}},
                                          {"type": "text", "text": "Done"}])
        self.assertEqual(sum(e["type"] == "content_block_start" for e in events), 2)
        self.assertEqual(sum(e["type"] == "message_stop" for e in events), 1)

    def test_failure_and_truncation_never_succeed(self):
        for terminal in [{"type": "error"}, {"type": "response.failed"},
                         {"type": "response.completed", "response": {"status": "failed"}},
                         {"type": "response.completed", "response": {"status": "cancelled"}},
                         {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "content_filter"}}}]:
            with self.subTest(terminal=terminal):
                adapter = MessageAdapter("test")
                adapter.feed({"type": "response.output_text.delta", "delta": "partial"})
                with self.assertRaises(ProxyError):
                    adapter.feed(terminal)
                self.assertFalse(adapter.completed)
        adapter = MessageAdapter("test")
        adapter.feed({"type": "response.output_item.added", "item": {"type": "function_call"}})
        with self.assertRaises(ProxyError):
            adapter.feed({"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}})
        self.assertFalse(adapter.completed)

    def test_usage_and_token_limit_reason(self):
        for kind in ["response.completed", "response.incomplete"]:
            adapter = MessageAdapter("test")
            adapter.feed({"type": "response.output_text.delta", "delta": "answer"})
            events = adapter.feed({"type": kind, "response": {"usage": {"input_tokens": 20,
                "output_tokens": 7, "input_tokens_details": {"cached_tokens": 12}},
                "incomplete_details": {"reason": "max_output_tokens"}}})
            self.assertEqual(adapter.usage, {"input_tokens": 8, "output_tokens": 7,
                             "cache_read_input_tokens": 12, "cache_creation_input_tokens": 0})
            self.assertEqual(events[-2]["usage"], adapter.usage)
            self.assertEqual(adapter.reason, "max_tokens" if kind.endswith("incomplete") else "end_turn")
        for usage in [{"input_tokens": 1, "input_tokens_details": {"cached_tokens": 2}},
                      {"input_tokens": -1}, {"output_tokens": True}, {"output_tokens": 1.5}]:
            adapter = MessageAdapter("test")
            adapter.feed({"type": "response.output_text.delta", "delta": "x"})
            with self.subTest(usage=usage), self.assertRaises(ProxyError):
                adapter.feed({"type": "response.completed", "response": {"usage": usage}})
            self.assertFalse(adapter.completed)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        credentials_patch = patch("proxy.read_credentials", return_value={"Authorization": "Bearer offline-upstream"})
        urlopen_patch = patch("proxy.urllib.request.urlopen")
        self.credentials = credentials_patch.start()
        self.addCleanup(credentials_patch.stop)
        self.urlopen = urlopen_patch.start()
        self.addCleanup(urlopen_patch.stop)
        self.server = proxy.ProxyServer(("127.0.0.1", 0), {"local_token": "offline-local", "model": "test-model",
            "reasoning_effort": "low", "upstream_timeout_seconds": 2}, Path("never-read-offline-auth.json"))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def request(self, body=None, path="/v1/messages", authorized=True, method="POST", extra_headers=None):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["x-api-key"] = "offline-local"
        headers.update(extra_headers or {})
        raw = body if isinstance(body, bytes) else json.dumps(body or {"messages": [{"role": "user", "content": "hi"}]})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            connection.close()

    def upstream(self, events):
        self.urlopen.return_value = io.BytesIO(b"".join(
            ("data: " + json.dumps(event) + "\n\n").encode() for event in events))

    @staticmethod
    def completed():
        return {"type": "response.completed", "response": {"status": "completed", "output": [
            {"id": "m", "type": "message", "content": [{"type": "output_text", "text": "Hello"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 4}}}}

    def test_auth_not_found_and_json_errors(self):
        ids = set()
        for kwargs, status, kind in [({"authorized": False}, 401, "authentication_error"),
                ({"path": "/missing"}, 404, "not_found_error"),
                ({"method": "GET", "path": "/missing"}, 404, "not_found_error"),
                ({"body": b"{"}, 400, "invalid_request_error"),
                ({"body": b"[]"}, 400, "invalid_request_error")]:
            with self.subTest(kwargs=kwargs):
                actual, headers, raw = self.request(**kwargs)
                result = json.loads(raw)
                self.assertEqual(actual, status)
                self.assertEqual(result["error"]["type"], kind)
                self.assertEqual(result["request_id"], headers["request-id"])
                self.assertTrue(headers["request-id"].startswith("req_"))
                ids.add(headers["request-id"])
        self.assertEqual(len(ids), 5)
        self.credentials.assert_not_called()
        self.urlopen.assert_not_called()

    def test_stream_and_nonstream(self):
        for stream in [False, True]:
            with self.subTest(stream=stream):
                self.upstream([self.completed()])
                status, headers, raw = self.request({"model": "sonnet", "messages": [{"role": "user", "content": "hi"}],
                                                     "stream": stream}, extra_headers={"Authorization": "Bearer offline-local"})
                self.assertEqual(status, 200)
                self.assertEqual(headers["x-upstream-model"], "test-model")
                if stream:
                    events = list(upstream_events(io.BytesIO(raw.encode())))
                    self.assertEqual(events[0]["type"], "message_start")
                    self.assertEqual(events[-1]["type"], "message_stop")
                    self.assertEqual(events[-2]["usage"]["input_tokens"], 6)
                else:
                    message = json.loads(raw)
                    self.assertEqual(message["model"], "sonnet")
                    self.assertEqual(message["content"], [{"type": "text", "text": "Hello"}])
                    self.assertEqual(message["usage"]["cache_read_input_tokens"], 4)
        self.assertEqual(self.credentials.call_count, 2)

    def test_disconnected_and_failed_streams_have_no_success(self):
        for stream in [False, True]:
            for tail in [[], [{"type": "response.failed"}]]:
                with self.subTest(stream=stream, tail=tail):
                    self.upstream([{"type": "response.output_text.delta", "delta": "partial"}] + tail)
                    status, _, raw = self.request({"messages": [{"role": "user", "content": "hi"}], "stream": stream})
                    if stream:
                        self.assertEqual(status, 200)
                        events = list(upstream_events(io.BytesIO(raw.encode())))
                        self.assertEqual(events[-1]["type"], "error")
                        self.assertNotIn("message_stop", [e["type"] for e in events])
                    else:
                        self.assertEqual(status, 502)
                        self.assertEqual(json.loads(raw)["error"]["type"], "api_error")

    def test_upstream_error_mapping_and_retry_after(self):
        for code, expected, kind in [(429, 429, "rate_limit_error"), (401, 503, "authentication_error"),
                                     (403, 503, "authentication_error"), (503, 529, "overloaded_error"),
                                     (500, 502, "api_error")]:
            with self.subTest(code=code):
                self.urlopen.side_effect = urllib.error.HTTPError("https://offline.invalid", code, "offline",
                    {"Retry-After": "17"}, io.BytesIO(b"upstream-body-must-not-leak"))
                status, headers, raw = self.request()
                self.assertEqual(status, expected)
                self.assertEqual(json.loads(raw)["error"]["type"], kind)
                self.assertEqual(headers["Retry-After"], "17")
                self.assertNotIn("upstream-body-must-not-leak", raw)
        self.urlopen.side_effect = urllib.error.URLError("offline")
        self.assertEqual(self.request()[0], 504)

    def test_malformed_upstream_frames_are_backend_errors_not_client_errors(self):
        for stream in (False, True):
            for wire in (b'data: {"type":\n\n', b'data: null\n\n', b'data: \xff\n\n'):
                with self.subTest(stream=stream, wire=wire):
                    self.urlopen.return_value = io.BytesIO(wire)
                    with self.assertLogs("proxy", level="WARNING") as logs:
                        status, _, raw = self.request({"stream": stream, "messages": [{"role": "user", "content": "hi"}]})
                    self.assertIn("phase=upstream_stream", " ".join(logs.output))
                    if stream:
                        self.assertEqual(status, 200)
                        events = list(upstream_events(io.BytesIO(raw.encode())))
                        self.assertEqual(events[-1]["type"], "error")
                        self.assertEqual(events[-1]["error"]["type"], "api_error")
                        self.assertNotIn("message_stop", [e["type"] for e in events])
                    else:
                        self.assertEqual(status, 502)
                        self.assertEqual(json.loads(raw)["error"]["type"], "api_error")

    def test_adapter_type_errors_are_diagnosed_without_exposing_body(self):
        self.upstream([self.completed()])
        with patch("proxy.MessageAdapter.feed", side_effect=TypeError("PRIVATE_CONTENT")):
            with self.assertLogs("proxy", level="WARNING") as logs:
                status, _, raw = self.request()
        self.assertEqual(status, 502)
        self.assertIn("exception=TypeError", " ".join(logs.output))
        self.assertNotIn("PRIVATE_CONTENT", raw + " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
