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
    if not config.ANTHROPIC_API_KEY and not config.OPENAI_API_KEY:
        warnings.append(
            "No model key (ANTHROPIC_API_KEY or OPENAI_API_KEY). TOWER will run "
            "in degraded mode: ingestion, change detection, usage analytics, "
            "health scoring and ranking all still work, but documents will not "
            "be read.")

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

    # A model key that exists but does not work is a WARNING, never a failure.
    # Billing state must not be able to stop the sentinel from noticing that a
    # document was withdrawn. Reading degrades; watching does not.
    from .brain import Brain
    b = Brain()
    print(f"reading provider: {b.provider}")
    if b.enabled:
        try:
            ok = b._tool_call(
                system="You reply with structured output.",
                user="Return the word ok.",
                schema={"type": "object", "properties": {"ok": {"type": "string"}},
                        "required": ["ok"]},
                tool_name="reply", model=config.MODEL_EXTRACT,
                max_tokens=32, retries=1)
            if ok is None:
                warnings.append(
                    f"{b.provider} key is present but the call failed "
                    f"({b.last_error}). Running in degraded mode: change detection "
                    f"and scoring stay live, document reading does not.")
            else:
                print(f"model reachable via {b.provider}")
        except Exception as e:
            warnings.append(f"{b.provider} key present but unusable: {e}")

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
