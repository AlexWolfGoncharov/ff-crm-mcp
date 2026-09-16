# ff-crm-mcp

Two commands for a CRM MCP gateway:

- `ff-crm-mcp` — one-shot CLI: open an MCP session, run one tool, exit.
- `ff-crm-mcp-stdio` — stdio↔Streamable-HTTP bridge, so the gateway can be
  registered as an MCP connector.

Stdlib only. No Node, no PyPI dependencies, Python 3.9+.

## Install

`--python 3.12` matters: on a machine with no Python at all, `uvx` otherwise
fails with `Failed to spawn: python`.

```bash
# no git, no package index — straight from a release asset
uvx --python 3.12 --from https://github.com/AlexWolfGoncharov/ff-crm-mcp/releases/download/v0.1.0/ff_crm_mcp-0.1.0-py3-none-any.whl ff-crm-mcp --doctor

# from source, where git is available
uvx --python 3.12 --from git+https://github.com/AlexWolfGoncharov/ff-crm-mcp ff-crm-mcp-stdio
```

## The package ships code only

No gateway URLs, no hostnames, no market data. The endpoint always comes from
you:

```bash
export CRM_MCP_URL="https://<gateway>/receiver-crm/mcp"
ff-crm-mcp --list-tools
ff-crm-mcp --doctor
```

Some gateways present a certificate that does not cover their host. For those,
and only those, add `CRM_MCP_INSECURE_TLS=1` (or `--insecure`).

If you have a `markets.json`, point at it and use `--market/--env` instead:

```bash
export CRM_MCP_MARKETS=/path/to/markets.json
ff-crm-mcp --market in --env prod --doctor
```

Resolution order: `--url` → `CRM_MCP_URL` → `--market/--env` via markets.json.

## As a connector

One entry per market and env — the name is how you (and an agent) know which
environment a call is writing to:

```json
"crm-az-prod": {
  "command": "uvx",
  "args": ["--python", "3.12", "--from", "https://github.com/AlexWolfGoncharov/ff-crm-mcp/releases/download/v0.1.0/ff_crm_mcp-0.1.0-py3-none-any.whl", "ff-crm-mcp-stdio"],
  "env": {
    "CRM_MCP_URL": "https://<gateway>/receiver-crm/mcp",
    "CRM_MCP_INSECURE_TLS": "1"
  }
}
```

**Where `uvx` exists, `uvx fastmcp-remote <url>` is the simpler connector** — add
`--verify false` for a gateway whose certificate does not cover its host
(verified 16.09.2026: handshake, tool list and tool calls all fine). This
package's bridge is for machines with neither `uvx` nor `npx`; the CLI side
keeps its own value either way — `--doctor` and the write guards have no
equivalent in a generic bridge.

Restart the client afterwards; MCP servers are read at startup. The bridge
prints the resolved URL to stderr on start, so the client log shows whether it
reached the network.

**On Windows, never use `command: npx`** in a connector entry or an `.mcpb`
manifest: it cannot spawn `npx.cmd` without a shell and dies with ENOENT. `uvx`
has no such problem.

## Diagnostics

`ff-crm-mcp --doctor` resolves the host through `getaddrinfo`, opens TCP 443,
then runs a real `initialize` + `tools/list` and reports the tool count. Use it
before blaming a gateway.

If the gateway is on a private network, note that `nslookup` and `dig` read
`/etc/resolv.conf` and bypass the macOS scoped resolver — they report NXDOMAIN
for hosts that applications resolve fine. `--doctor` uses `getaddrinfo`, which
is what a real client does.

## Guard rails

`ff-crm-mcp` refuses to create a communication that is not paused, refuses a
promotional one without a control group, and refuses a destructive call without
an explicit confirmation flag.

**A connector enforces none of that** — the bridge is transport only. Prefer the
CLI wherever a shell is available.

## Quirks it handles

- **A certificate that does not cover the gateway host.** `--insecure` /
  `CRM_MCP_INSECURE_TLS=1` for exactly those endpoints.
- **A gateway that echoes a numeric JSON-RPC id back as a string** (`{"id": 1}`
  → `{"id": "1"}`). A strict client never matches that reply to its request; the
  bridge restores the original id and its type.
