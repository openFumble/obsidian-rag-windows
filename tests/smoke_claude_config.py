"""Start the server exactly as Claude Desktop would and talk MCP to it.

Reads the obsidian-rag entry from a claude_desktop_config.json, launches
command + args + env from it, lists the tools and runs a search.

    python tests/smoke_claude_config.py <claude_desktop_config.json> [server-name] [query]
"""

import asyncio
import json
import os
import sys


async def main(cfg_path: str, name: str, query: str) -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with open(cfg_path, encoding="utf-8") as f:
        entry = json.load(f)["mcpServers"][name]
    print("command:", entry["command"], *entry["args"])
    params = StdioServerParameters(command=entry["command"], args=entry["args"],
                                   env={**os.environ, **entry.get("env", {})})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            print("tools:", ", ".join(tools))
            res = json.loads((await s.call_tool("search", {"query": query})).content[0].text)
            hits = [x["path"] for x in res["results"]]
            print("search:", query, "->", hits)
            status = json.loads((await s.call_tool("index_status", {})).content[0].text)
            print("index:", status["notes"], "notes,", status["chunks"], "chunks, watcher:", status["watcher"])
            ok = len(tools) >= 8 and bool(hits) and status["notes"] > 0
            print("SMOKE TEST", "PASSED" if ok else "FAILED")
            return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "obsidian-rag",
                              sys.argv[3] if len(sys.argv) > 3 else "VLAN")))
