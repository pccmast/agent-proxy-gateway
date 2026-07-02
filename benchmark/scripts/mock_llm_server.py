"""Mock LLM upstream server for gateway benchmarking.

Returns fixed responses to isolate gateway performance from network variance.
Simulates both non-streaming (JSON) and streaming (SSE) LLM APIs.

Usage:
    python benchmark/scripts/mock_llm_server.py

The server listens on 127.0.0.1:18081 and mimics OpenAI-compatible endpoints:
    POST /v1/chat/completions  → JSON or SSE response
"""

import argparse
import json
import random
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockLLMHandler(BaseHTTPRequestHandler):
    """Handler for mock LLM requests."""

    # Configurable delay range (seconds)
    MIN_DELAY = 0.001  # 1ms
    MAX_DELAY = 0.003  # 3ms

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default access logs during benchmark."""
        pass

    def do_POST(self) -> None:
        """Handle POST requests to /v1/chat/completions."""
        path = self.path
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Simulate minimal upstream processing delay
        time.sleep(random.uniform(self.MIN_DELAY, self.MAX_DELAY))

        if "/v1/chat/completions" in path:
            try:
                req = json.loads(body)
                is_stream = req.get("stream", False)
            except (json.JSONDecodeError, UnicodeDecodeError):
                is_stream = False

            if is_stream:
                self._send_sse_response()
            else:
                self._send_json_response()
        else:
            self._send_error(404, "Unknown endpoint")

    def _send_json_response(self) -> None:
        """Send a standard non-streaming chat completion response."""
        response = {
            "id": f"mock-{random.randint(10000, 99999)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "mock-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "This is a mock response for benchmarking purposes.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 15,
                "completion_tokens": 10,
                "total_tokens": 25,
            },
        }
        self._send_json(200, response)

    def _send_sse_response(self) -> None:
        """Send a streaming SSE response with multiple chunks."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # Simulate 5 content chunks + 1 finish chunk + [DONE]
        chunks = [
            {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " world"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "!"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " This"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " is"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " a"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " mock"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " response"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "."}, "finish_reason": "stop"}]},
        ]

        for chunk in chunks:
            line = f"data: {json.dumps(chunk)}\n\n"
            self.wfile.write(line.encode())
            self.wfile.flush()
            time.sleep(0.005)  # 5ms between chunks

        self.wfile.write(b"data: [DONE]\n\n")

    def _send_json(self, status: int, data: dict) -> None:
        """Helper to send a JSON response."""
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        """Helper to send an error response."""
        self._send_json(status, {"error": message})


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock LLM upstream server for benchmarking")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=18081, help="Port to bind (default: 18081)")
    parser.add_argument("--min-delay", type=float, default=1.0, help="Min upstream delay in ms (default: 1)")
    parser.add_argument("--max-delay", type=float, default=3.0, help="Max upstream delay in ms (default: 3)")
    args = parser.parse_args()

    MockLLMHandler.MIN_DELAY = args.min_delay / 1000.0
    MockLLMHandler.MAX_DELAY = args.max_delay / 1000.0

    server = HTTPServer((args.host, args.port), MockLLMHandler)
    print(f"Mock LLM server running on http://{args.host}:{args.port}")
    print(f"Simulated upstream delay: {args.min_delay}-{args.max_delay}ms")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
