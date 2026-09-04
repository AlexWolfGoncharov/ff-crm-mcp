# ff-crm-mcp

Two commands for a Fintech Farm CRM MCP gateway:

- `ff-crm-mcp` — one-shot CLI: open an MCP session, run one tool, exit.
- `ff-crm-mcp-stdio` — stdio↔Streamable-HTTP bridge, so the gateway can be
  registered as an MCP connector.

Stdlib only. No Node, no PyPI dependencies, Python 3.9+.

## The package ships code only

No gateway URLs, no hostnames, no market data. The endpoint always comes from
you:

```bash
export CRM_MCP_URL="https://<gateway>/receiver-crm/mcp"
uvx ff-crm-mcp --list-tools
uvx ff-crm-mcp --doctor
```

Some gateways present a certificate that does not cover their host. For those,
and only those, add `CRM_MCP_INSECURE_TLS=1` (or `--insecure`).

If you have a `markets.json` (it lives in the `crm-mcp` skill), point at it and
use `--market/--env` instead:

```bash
export CRM_MCP_MARKETS=/path/to/crm-mcp/references/markets.json
uvx ff-crm-mcp --market in --env prod --doctor
```

Resolution order: `--url` → `CRM_MCP_URL` → `--market/--env` via markets.json.

## As a connector

`claude_desktop_config.json` — one entry per market and env, and the name is how
an agent knows which env it is writing to:

```json
"crm-az-prod": {
  "command": "uvx",
  "args": ["ff-crm-mcp-stdio"],
  "env": {
    "CRM_MCP_URL": "https://<az-prod-gateway>/receiver-crm/mcp",
    "CRM_MCP_INSECURE_TLS": "1"
  }
}
```

Restart the client afterwards; MCP servers are read at startup. The bridge
prints the resolved URL to stderr on start, so the client log shows whether it
reached the network.

Every gateway is internal: VPN required. `nslookup` and `dig` bypass the macOS
scoped resolver and will report NXDOMAIN for a host that resolves fine — use
`ff-crm-mcp --doctor`, which checks DNS through `getaddrinfo`, TCP 443, and a
real `initialize` + `tools/list`.

## Guard rails

`ff-crm-mcp` refuses to create a communication that is not on `pause`, refuses a
promotional one without a control group ≥2%, and refuses a destructive call
without an explicit confirmation flag. **A connector enforces none of this** —
the bridge is transport only. Prefer the CLI where a shell is available.

## Install

```bash
# no git needed — straight from a release asset
uvx --from https://github.com/AlexWolfGoncharov/ff-crm-mcp/releases/download/v0.1.0/ff_crm_mcp-0.1.0-py3-none-any.whl ff-crm-mcp --doctor

# or from source, if git is available
uvx --from git+https://github.com/AlexWolfGoncharov/ff-crm-mcp ff-crm-mcp-stdio
```

In a connector entry, the whole `--from …` form goes in `args` ahead of the
command name.
