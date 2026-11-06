import unittest

from proxy import MessageAdapter, ProxyError, safe_error_text, upstream_error_details, classify_failure


class DiagnosticTests(unittest.TestCase):
    def test_same_exception_is_classified_by_processing_phase(self):
        for error in (ValueError(), TypeError(), AttributeError(), KeyError()):
            self.assertEqual(classify_failure(error, "request_translate").status, 400)
            self.assertEqual(classify_failure(error, "upstream_stream").status, 502)
            self.assertEqual(classify_failure(error, "credentials").status, 500)

    def test_missing_final_tool_arguments_use_complete_buffer(self):
        adapter = MessageAdapter("test")
        item = {"type": "function_call", "id": "fc", "call_id": "c", "name": "Read", "arguments": None}
        adapter.feed({"type": "response.output_item.added", "item": dict(item, arguments="")})
        adapter.feed({"type": "response.function_call_arguments.delta", "delta": '{"path":"a"}'})
        adapter.feed({"type": "response.function_call_arguments.done", "arguments": None})
        adapter.feed({"type": "response.output_item.done", "item": item})
        adapter.feed({"type": "response.completed", "response": {"output": [item]}})
        self.assertEqual(adapter.blocks[0]["input"], {"path": "a"})
        self.assertTrue(adapter.completed)

    def test_error_fields_only_and_credentials_redacted(self):
        details = upstream_error_details({"error": {
            "code": "unsupported_parameter", "message": "Invalid Bearer abcdef1234567890\nnext line",
            "request": {"prompt": "MUST_NOT_APPEAR"}}, "input": "MUST_NOT_APPEAR"})
        self.assertIn("unsupported_parameter", details)
        self.assertNotIn("abcdef1234567890", details)
        self.assertNotIn("MUST_NOT_APPEAR", details)
        self.assertNotIn("\n", details)

    def test_jwt_and_local_token_masked(self):
        result = safe_error_text("eyJhbGciOiJub25lIn0.eyJzdWIiOiJhYmMifQ.signature local-abcdefghijklmnopqrstuv")
        self.assertNotIn("eyJ", result)
        self.assertNotIn("abcdefghijklmnopqrstuv", result)

    def test_stream_failure_retains_actual_error(self):
        adapter = MessageAdapter("test")
        with self.assertRaisesRegex(ProxyError, "usage_limit_reached"):
            adapter.feed({"type": "response.failed", "response": {
                "error": {"code": "usage_limit_reached", "message": "Quota exhausted"}}})
        self.assertFalse(adapter.completed)


if __name__ == "__main__":
    unittest.main()
