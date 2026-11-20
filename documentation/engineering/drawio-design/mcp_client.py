"""Small Streamable HTTP client for the user-requested draw.io MCP."""

import json
import urllib.error
import urllib.request

URL = "https://mcp.draw.io/mcp"


class DrawioMCP:
    def __init__(self):
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "Mozilla/5.0",
        }
        self.counter = 0
        self.initialize = self.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "Codex-diagrams", "version": "1.0"},
        })

    def request(self, method, params):
        self.counter += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.counter,
                           "method": method, "params": params}).encode()
        request = urllib.request.Request(URL, data=body, headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=55) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.headers["Mcp-Session-Id"] = session
                raw = response.read().decode()
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"MCP HTTP {error.code}: {error.read().decode()[:800]}") from error
        if raw.lstrip().startswith("data:") or raw.lstrip().startswith("event:"):
            events = [json.loads(line[5:].strip()) for line in raw.splitlines()
                      if line.startswith("data:")]
            result = next(event for event in events if event.get("id") == self.counter)
        else:
            result = json.loads(raw)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["result"]

    def call(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(result)
        return result


if __name__ == "__main__":
    from pathlib import Path
    client = DrawioMCP()
    output = Path(__file__).parent
    result = client.call("get_drawio_reference", {"format": "xml"})
    (output / "mcp-xml-reference.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    for block in result.get("content", []):
        if block.get("type") == "text":
            print(block["text"].encode("ascii", "backslashreplace").decode())
