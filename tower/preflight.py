"""
Preflight. Fail loudly, early, and specifically.

A system that quietly produces plausible output when its inputs are missing
is worse than one that crashes. Every check here is a thing that, if wrong,
would make the dashboard look fine while being untrue.
"""

from __future__ import annotations

import sys

from . import config
from .mcp_client import MCPClient

REQUIRED_TOOLS = {
    "list_accounts", "get_account", "list_account_documents",
    "get_account_document", "get_account_usage",
}
OPTIONAL_TOOLS = {"search_documents", "se_list_dataset_files",
                  "se_get_dataset_file", "se_search_dataset"}


def main() -> int:
    problems, warnings = [], []

    if not config.MCP_TOKEN:
        problems.append("MCP_TOKEN is empty. The Book of Business is unreachable.")
    if not config.ANTHROPIC_API_KEY:
        warnings.append(
            "ANTHROPIC_API_KEY is empty. TOWER will run in degraded mode: "
            "ingestion, change detection, usage analytics, health scoring and "
            "ranking all still work, but documents will not be read.")

    tools: list[str] = []
    if config.MCP_TOKEN:
        try:
            c = MCPClient(config.MCP_ENDPOINT, config.MCP_TOKEN)
            info = c.initialize()
            print(f"MCP server: {info.get('serverInfo', {}).get('name', '?')} "
                  f"v{info.get('serverInfo', {}).get('version', '?')}")
            tools = [t["name"] for t in c.list_tools()]
            print(f"tools exposed: {len(tools)} -> {', '.join(sorted(tools))}")
            missing = REQUIRED_TOOLS - set(tools)
            if missing:
                problems.append(f"MCP server is missing required tools: {sorted(missing)}")
            absent_opt = OPTIONAL_TOOLS - set(tools)
            if absent_opt:
                warnings.append(f"optional tools not exposed: {sorted(absent_opt)}")

            accounts = c.call("list_accounts")
            n = len(accounts) if isinstance(accounts, list) else -1
            print(f"accounts visible: {n}")
            if n <= 0:
                problems.append("list_accounts returned nothing.")
            elif n < 14:
                warnings.append(
                    f"list_accounts returned {n} accounts. The brief describes 14. "
                    "If this is a deliberate source change the sentinel will "
                    "tombstone the missing ones on the next cycle.")
            c.close()
        except Exception as e:
            problems.append(f"cannot reach the MCP endpoint: {e}")

    if config.ANTHROPIC_API_KEY:
        try:
            from anthropic import Anthropic
            Anthropic(api_key=config.ANTHROPIC_API_KEY).messages.create(
                model=config.MODEL_EXTRACT, max_tokens=8,
                messages=[{"role": "user", "content": "reply with the single word ok"}])
            print(f"model reachable: {config.MODEL_EXTRACT}")
        except Exception as e:
            problems.append(f"Anthropic API key present but unusable: {e}")

    for w in warnings:
        print(f"WARN  {w}")
    for p in problems:
        print(f"FAIL  {p}")

    if problems:
        print("\npreflight FAILED")
        return 1
    print("\npreflight OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
