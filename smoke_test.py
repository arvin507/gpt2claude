"""Live checks; consumes subscription quota. Pass --claude to test the installed CLI too."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent


def find_claude():
    """PATH first, then the installer's per-user location. Handles Windows and POSIX names."""
    found = shutil.which("claude")
    if found:
        return found
    for name in ("claude.exe", "claude"):
        candidate = Path.home() / ".local/bin" / name
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("Claude Code CLI not found; install it or pass an explicit path.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", action="store_true")
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text())
    base = f"http://127.0.0.1:{config['port']}"

    def call(body, stream=False):
        req = urllib.request.Request(base + "/v1/messages", data=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + config["local_token"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as response:
            if not stream:
                return json.load(response)
            events = []
            for raw in response:
                if raw.startswith(b"data:"):
                    events.append(json.loads(raw[5:]))
            return events

    body = {"model": "claude-sonnet-4-5", "max_tokens": 100,
            "messages": [{"role": "user", "content": "What is 17 * 23? Return only the number."}]}
    response = call(body)
    assert response["stop_reason"] == "end_turn", response
    assert "391" in "".join(b.get("text", "") for b in response["content"]), response
    print("PASS non-streaming Messages: 391")

    tools = [{"name": "get_test_value", "description": "Get the test value from the local test harness.",
              "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}}]
    messages = [{"role": "user", "content": "Call get_test_value with key=sample, then report the exact value it returns."}]
    response = call({**body, "messages": messages, "tools": tools, "tool_choice": {"type": "tool", "name": "get_test_value"}})
    assert response["stop_reason"] == "tool_use", response
    tool = next(b for b in response["content"] if b["type"] == "tool_use")
    assert tool["name"] == "get_test_value" and tool["input"]["key"] == "sample", response
    print("PASS tool call -> Anthropic tool_use with valid arguments")
    messages += [{"role": "assistant", "content": response["content"]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool["id"], "content": "ORCHID-7391"}]}]
    events = call({**body, "stream": True, "messages": messages, "tools": tools,
                   "tool_choice": {"type": "none"}}, stream=True)
    assert not any(e["type"] == "error" for e in events), events
    assert events[0]["type"] == "message_start" and events[-1]["type"] == "message_stop", events
    answer = "".join(e.get("delta", {}).get("text", "") for e in events)
    assert "ORCHID-7391" in answer, answer
    print("PASS tool_result -> GPT -> Anthropic SSE: ORCHID-7391")

    if args.claude:
        fixture = ROOT / "test-fixture.txt"
        fixture.write_text("The verification code is MAPLE-4826.\n", encoding="utf-8")
        (ROOT / "empty-mcp.json").write_text('{"mcpServers":{}}\n')
        prompt = f"Use the Read tool to read {fixture}. What is the verification code? Return only the code."
        command = [find_claude(), "--settings", str(ROOT / "claude-settings.json"),
                   "--setting-sources=", "--strict-mcp-config", "--mcp-config", str(ROOT / "empty-mcp.json"),
                   "--tools", "Read", "--allowedTools", "Read", "--permission-mode", "dontAsk",
                   "--no-session-persistence", "--output-format", "json", "-p", prompt]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, encoding="utf-8", timeout=240)
        if result.returncode:
            raise RuntimeError(f"Claude Code exit {result.returncode}: {result.stdout[-2000:]} {result.stderr[-1000:]}")
        payload = json.loads(result.stdout)
        assert not payload.get("is_error"), payload
        assert "MAPLE-4826" in payload.get("result", ""), payload
        print("PASS real Claude Code CLI + Read tool + GPT answer: MAPLE-4826")
        (ROOT / "claude-smoke-result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
