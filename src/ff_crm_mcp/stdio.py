#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Register the CRM MCP gateway as a stdio MCP server — no Node, no PyPI deps.

Claude Desktop and Claude Code speak stdio to a local process; the CRM gateways
speak Streamable HTTP. This bridges the two: newline-delimited JSON-RPC on
stdin/stdout, one HTTP POST per message, session id threaded through.

  "crm-az-prod": {
    "command": "uv",
    "args": ["run", "<skill>/scripts/crm_stdio.py", "--market", "az", "--env", "prod"]
  }

Why this and not `npx mcp-remote` or `uvx mcp-proxy`:
  - no Node on the machine is enough to kill mcp-remote;
  - mcp-proxy needs `--with mcp==1.9.x` pinned (2.x dropped request_ctx) AND
    still cannot reach in/prod or az/prod, whose certs do not cover their host:
    verified 04.09.2026, httpx raises CERTIFICATE_VERIFY_FAILED / Hostname
    mismatch with no way to opt out.
  This reads the url and the insecure_tls flag from ../references/markets.json,
  so those two gateways just work — and stays in sync with crm_mcp.py.

Everything on stdout is protocol. Diagnostics go to stderr.

NOTE: a connector enforces none of the three hard rules in SKILL.md (create on
pause, control group on promo, delete confirmation). Prefer crm_mcp.py where a
shell is available; use this when only a connector can work.
"""
import argparse
import json
import os
import sys

from .cli import Session, load_market, parse_response, _truthy  # one canonical copy


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--market", help="in, az, vn, kg, uz, ma (or use --url / CRM_MCP_URL)")
    p.add_argument("--env", default="dev", help="dev | prod (default dev)")
    p.add_argument("--timeout", type=int, default=300, help="seconds per call")
    p.add_argument("--url", help="gateway URL directly (or set CRM_MCP_URL)")
    p.add_argument("--insecure", action="store_true",
                   help="skip TLS verification (auto-on where markets.json sets insecure_tls)")
    a = p.parse_args()

    url = a.url or os.environ.get("CRM_MCP_URL")
    if url:
        env = {"mcp_url": url,
               "insecure_tls": a.insecure or _truthy(os.environ.get("CRM_MCP_INSECURE_TLS"))}
        a.market = a.market or "url"
    else:
        if not a.market:
            p.error("--market (with --env), or --url / CRM_MCP_URL, is required")
        _, env = load_market(a.market, a.env)
    insecure = a.insecure or bool(env.get("insecure_tls"))
    s = Session(env["mcp_url"], a.timeout, insecure=insecure)
    who = "%s/%s" % (a.market.lower(), a.env.lower()) if not url else "CRM_MCP_URL"
    print("%s -> %s%s" % (who, env["mcp_url"],
                          "  (TLS verification off)" if insecure else ""),
          file=sys.stderr, flush=True)

    # ponytail: one request at a time, in the order the client sent them. The CRM
    # gateway never initiates a message, so there is nothing to pump the other way;
    # if a client ever pipelines concurrent requests, thread this loop.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as e:
            print("dropped a line that is not JSON: %s" % e, file=sys.stderr, flush=True)
            continue

        mid = msg.get("id")
        try:
            body = s._post(msg)
        except SystemExit as e:  # Session._post exits on a transport error
            if mid is None:
                continue
            emit({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32001, "message": str(e)}})
            continue

        if mid is None:
            continue  # a notification: the gateway answers 202 with no body
        reply = parse_response(body)
        if reply is None:
            emit({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32002, "message": "empty response from the CRM gateway"}})
        else:
            # The gateway echoes the id as a STRING even when the client sent a
            # number ({"id": 1} comes back as {"id": "1"} — verified IN/prod,
            # 04.09.2026). JSON-RPC requires it back unchanged, and a strict
            # client will not match the reply to its request. Restore ours.
            if reply.get("id") != mid:
                reply["id"] = mid
            emit(reply)


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
