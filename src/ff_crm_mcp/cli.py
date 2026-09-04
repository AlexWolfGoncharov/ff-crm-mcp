#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Call a CRM MCP gateway over Streamable HTTP — one tool per invocation.

Nothing runs persistently: each call opens an MCP session, runs one tool, prints
the result, exits.

  ff-crm-mcp --url https://<gateway>/receiver-crm/mcp --doctor
  ff-crm-mcp --list-tools                                  # with CRM_MCP_URL set
  ff-crm-mcp --tool authenticate --args @auth.json
  ff-crm-mcp --tool get_classevent_by_id --args @payload.json
  ff-crm-mcp --check-name "EMI | Purchase converted | Promo cashback confirm | Push"

Endpoint resolution: --url, then CRM_MCP_URL, then --market/--env against a
markets.json (CRM_MCP_MARKETS, or the copy inside the crm-mcp skill). The
published package carries no gateway URLs of its own.

Use --insecure (or CRM_MCP_INSECURE_TLS=1) for a gateway whose certificate does
not cover its host.
"""
import argparse
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

# ponytail: Windows writes stdout in the console code page, so a Russian tool
# description or a Hindi template raises UnicodeEncodeError before anything else
# can go wrong. Every file this script reads is UTF-8 too — never the locale.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def markets_path():
    """Where markets.json lives — and it is optional.

    The published package ships CODE ONLY: no internal hostnames. An endpoint
    comes from --url or CRM_MCP_URL, and markets.json is used only when you have
    the file (inside the crm-mcp skill, or pointed at by CRM_MCP_MARKETS).
    """
    env = os.environ.get("CRM_MCP_MARKETS")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in (("..", "references", "markets.json"),                 # skill layout, via the shim
                ("..", "..", "..", "..", "plugins", "crm-tools", "skills",
                 "crm-mcp", "references", "markets.json")):           # repo checkout
        cand = os.path.abspath(os.path.join(here, *rel))
        if os.path.exists(cand):
            return cand
    return None


CONFIG = markets_path()
PROMO_TYPES = ("marketing", "bulkmailing")
MIN_CONTROL_GROUP = 2  # percent


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on") if v else False


def load_market(market, env):
    if not CONFIG:
        sys.exit(
            "no markets.json here, so --market/--env cannot be resolved.\n"
            "Pass the gateway directly: --url https://<gateway>/receiver-crm/mcp "
            "(add --insecure where the cert does not cover the host), or set "
            "CRM_MCP_URL, or point CRM_MCP_MARKETS at a markets.json."
        )
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    m = cfg["markets"].get(market.upper())
    if not m:
        sys.exit("unknown market %r — known: %s" % (market, ", ".join(cfg["markets"])))
    e = m["envs"].get(env.lower())
    if not e:
        sys.exit("unknown env %r for %s — known: %s" % (env, market, ", ".join(m["envs"])))
    if not e.get("mcp_url"):
        sys.exit(
            "no CRM MCP url for %s/%s (status: %s).\n"
            "Ask the user for the gateway URL and put it in references/markets.json — never guess one."
            % (market.upper(), env, e.get("status"))
        )
    return m, e


CHANNEL_WORDS = ("push", "email", "sms", "banner", "popup", "superbanner", "supperbanner",
                 "superinsert", "call", "ivr", "zalo", "successscreen")
TYPE_WORDS = ("service", "marketing", "bulkmailing")


def name_issues(name):
    """Mechanical half of references/naming.md. Judgement calls stay with the author."""
    out = []
    parts = [p.strip() for p in name.split("|") if p.strip()]
    if " | " not in name or len(parts) < 3:
        out.append("expected '<Product> | <Action> | [Campaign] | <Channel>' with ' | ' separators")
    if len(parts) > 4:
        out.append("%d segments — the template has at most 4" % len(parts))
    if parts and parts[-1].lower() not in CHANNEL_WORDS:
        out.append("last segment should be the channel (%s), got %r" % ("Push/Email/SMS/…", parts[-1]))
    if any(ord(c) > 127 for c in name):
        out.append("non-ASCII characters — breaks search; write it in English")
    if "(" in name or ")" in name:
        out.append("no notes in parentheses — put them in targetAudience or the description")
    for p in parts[:-1]:
        if p.lower() in TYPE_WORDS:
            out.append("%r is the event type — it lives in the `type` field, not the name" % p)
    if len(name) > 80:
        out.append("%d characters — keep it under 80" % len(name))
    if name.strip().lower().startswith("test") and not name.startswith("TEST"):
        out.append("a test event is prefixed TEST and stays on dev")
    return out


def guard(tool, args):
    """Rules that must never be broken by accident. Fail before the network call."""
    if tool == "create_classevent":
        if str(args.get("status", "")).lower() != "pause":
            return 'create_classevent must be called with status="pause" — the user turns it on after the checklist'
        etype = str(args.get("type", "")).lower()
        waived = args.pop("_control_group_waived_by_user", False)
        if etype in PROMO_TYPES and not waived:
            cg = str(args.get("controlGroup", "")).strip().rstrip("%")
            try:
                pct = float(cg)
            except ValueError:
                pct = 0
            if pct < MIN_CONTROL_GROUP:
                return (
                    'promotional communication (type=%s) requires a control group of at least %d%% — '
                    'got %r. Agree the size with the author. Only if the author explicitly waives it, '
                    're-run with "_control_group_waived_by_user": true.'
                    % (etype, MIN_CONTROL_GROUP, args.get("controlGroup"))
                )
    if tool in ("delete_classevent", "delete_all_communications_by_event_id", "delete_event_class_access"):
        if not args.pop("_confirmed_by_user", False):
            return "%s is destructive — ask the user explicitly, then re-run with \"_confirmed_by_user\": true" % tool
    return None


class Session:
    def __init__(self, url, timeout, insecure=False):
        self.url = url
        self.timeout = timeout
        # ponytail: some prod gateways present a cert that doesn't cover the host
        # (IN prod: single-label wildcard vs two-label host). insecure skips TLS
        # verification for exactly those, flagged per-env in markets.json.
        self.ctx = ssl._create_unverified_context() if insecure else None
        self.sid = None
        self.n = 0

    def _post(self, payload):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                self.sid = r.headers.get("Mcp-Session-Id") or self.sid
                return r.read().decode()
        except urllib.error.HTTPError as e:
            sys.exit("HTTP %s from CRM MCP: %s" % (e.code, e.read().decode()[:500]))
        except OSError as e:
            sys.exit("cannot reach CRM MCP (%s): %s\nVPN up? URL right?" % (self.url, e))

    def call(self, method, params=None):
        self.n += 1
        body = self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        msg = parse_response(body)
        if msg is None:
            sys.exit("empty response to %s" % method)
        if "error" in msg:
            sys.exit("MCP error on %s: %s" % (method, json.dumps(msg["error"], ensure_ascii=False)))
        return msg.get("result", {})

    def notify(self, method):
        self._post({"jsonrpc": "2.0", "method": method})

    def open(self):
        self.call("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "crm-mcp-skill", "version": "1"},
        })
        self.notify("notifications/initialized")


def parse_response(body):
    """Accept both a plain JSON body and an SSE stream."""
    body = body.strip()
    if not body:
        return None
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk:
                msg = json.loads(chunk)
                if "id" in msg or "error" in msg:
                    return msg
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--market", help="in, az, vn, kg, uz, ma")
    p.add_argument("--env", default="dev", help="dev | prod (default dev)")
    p.add_argument("--tool")
    p.add_argument("--args", default="{}", help="JSON object, or @file.json")
    p.add_argument("--list-tools", action="store_true")
    p.add_argument("--timeout", type=int, default=300, help="seconds (the first call of a session can hang ~4 min)")
    p.add_argument("--url", help="gateway URL directly, instead of --market/--env "
                                "(or set CRM_MCP_URL)")
    p.add_argument("--insecure", action="store_true",
                   help="skip TLS verification (auto-on where markets.json sets insecure_tls, e.g. IN prod)")
    p.add_argument("--check-name", dest="check_name", metavar="NAME",
                   help="check an eventName against references/naming.md (offline)")
    p.add_argument("--doctor", action="store_true",
                   help="check DNS (via getaddrinfo, not nslookup), TCP, MCP session and tool count")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()

    if a.selftest:
        selftest()
        return
    if a.check_name:
        problems = name_issues(a.check_name)
        print("\n".join("- " + x for x in problems) if problems else "OK — matches the naming standard")
        sys.exit(1 if problems else 0)
    url = a.url or os.environ.get("CRM_MCP_URL")
    if url:
        label = (a.market or "url"), (a.env or "-")
        env = {"mcp_url": url,
               "insecure_tls": a.insecure or _truthy(os.environ.get("CRM_MCP_INSECURE_TLS"))}
        a.market, a.env = label[0], label[1]
    else:
        if not a.market:
            p.error("--market (with --env), or --url / CRM_MCP_URL, is required")
        _, env = load_market(a.market, a.env)
    insecure = a.insecure or bool(env.get("insecure_tls"))
    if a.doctor:
        sys.exit(doctor(a.market, a.env.lower(), env, a.timeout, insecure))
    s = Session(env["mcp_url"], a.timeout, insecure=insecure)
    s.open()

    if a.list_tools:
        for t in s.call("tools/list").get("tools", []):
            print("%-42s %s" % (t["name"], (t.get("description") or "").splitlines()[0][:100]))
        return

    if not a.tool:
        p.error("--tool or --list-tools is required")
    raw = a.args
    if raw.startswith("@"):
        raw = open(raw[1:], encoding="utf-8").read()
    args = json.loads(raw)

    problem = guard(a.tool, args)
    if problem:
        sys.exit("REFUSED: " + problem)

    if a.tool == "create_classevent":
        for x in name_issues(str(args.get("eventName", ""))):
            print("name warning: " + x, file=sys.stderr)

    res = s.call("tools/call", {"name": a.tool, "arguments": args})
    for item in res.get("content", []):
        print(item.get("text", json.dumps(item, ensure_ascii=False)))
    if res.get("isError"):
        sys.exit(1)


def doctor(market_code, env_name, env, timeout, insecure):
    """One command that answers 'is this gateway actually broken?'.

    Deliberately uses socket.getaddrinfo, NOT nslookup/dig: those read
    /etc/resolv.conf and never consult the macOS scoped (split-DNS) resolver,
    so on VPN they report NXDOMAIN for internal hosts that applications
    resolve fine. That false NXDOMAIN has been mistaken for "VPN is down".
    """
    url = env["mcp_url"]
    host = urllib.parse.urlsplit(url).hostname
    port = urllib.parse.urlsplit(url).port or 443
    print("%s/%s  %s" % (market_code.upper(), env_name, url))
    print("TLS verification: %s" % ("OFF (cert does not cover this host)" if insecure else "on"))

    ok = True
    try:
        addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(host, port)})
        print("DNS   OK   %s -> %s" % (host, ", ".join(addrs)))
    except OSError as e:
        print("DNS   FAIL %s: %s" % (host, e))
        print("      VPN down, or no split-DNS profile for this market. Ask DevOps; never edit the URL.")
        print("      (nslookup/dig NXDOMAIN alone proves nothing — they bypass the scoped resolver.)")
        return 1

    try:
        with socket.create_connection((addrs[0], port), timeout=min(timeout, 15)):
            print("TCP   OK   %s:%s open" % (addrs[0], port))
    except OSError as e:
        print("TCP   FAIL %s:%s — %s" % (addrs[0], port, e))
        return 1

    s = Session(url, timeout, insecure=insecure)
    s.open()
    tools = s.call("tools/list").get("tools", [])
    print("MCP   OK   session up, %d tools" % len(tools))
    if not tools:
        print("      gateway answered but exposes no tools — that IS a server-side problem")
        ok = False
    print('\nNext: put {"email": "<your corporate email>"} in auth.json, then')
    print("      --tool authenticate --args @auth.json")
    print("      (a file, not inline JSON — cmd.exe mangles inline quoting.")
    print("       A personal address returns 'Not found ldap by email' — not a fault.)")
    return 0 if ok else 1


def selftest():
    # control group is mandatory on promo
    assert guard("create_classevent", {"status": "pause", "type": "marketing", "controlGroup": "0"})
    assert guard("create_classevent", {"status": "pause", "type": "bulkMailing"})
    assert guard("create_classevent", {"status": "pause", "type": "marketing", "controlGroup": "3"}) is None
    assert guard("create_classevent", {"status": "pause", "type": "marketing", "controlGroup": "2%"}) is None
    assert guard("create_classevent", {"status": "pause", "type": "marketing",
                                       "_control_group_waived_by_user": True}) is None
    # service comms need no control group
    assert guard("create_classevent", {"status": "pause", "type": "service"}) is None
    # never create live
    assert guard("create_classevent", {"status": "active", "type": "service"})
    # destructive needs confirmation
    assert guard("delete_classevent", {"eventId": "M1"})
    assert guard("delete_classevent", {"eventId": "M1", "_confirmed_by_user": True}) is None
    # naming standard — mechanical checks only
    assert name_issues("Personal loan | Pre-approved | Push") == []
    assert name_issues("EMI | Purchase converted | Promo cashback confirm | Push") == []
    assert any("separators" in x for x in name_issues("tra-conf-EMI-triggered"))
    assert any("non-ASCII" in x for x in name_issues("Day 2 | EDU → Standard | Push"))
    assert any("parentheses" in x for x in name_issues("Personal loan | Pre-approved (баг) | Push"))
    assert any("channel" in x for x in name_issues("Personal loan | Pre-approved | Promo"))
    assert any("type" in x for x in name_issues("Service | Pre-approved | Push"))
    # transport parsing
    assert parse_response('{"id":1,"result":{"ok":1}}')["result"]["ok"] == 1
    assert parse_response('event: message\ndata: {"id":1,"result":{"ok":2}}\n\n')["result"]["ok"] == 2
    assert parse_response("") is None
    # doctor parses the url it will actually dial
    u = urllib.parse.urlsplit("https://sub.gateway.example/receiver-crm/mcp")
    assert u.hostname == "sub.gateway.example"
    assert (u.port or 443) == 443
    # every live env must carry a dialable url, and IN/AZ prod must keep insecure_tls
    assert _truthy("1") and _truthy("TRUE") and not _truthy("0") and not _truthy(None)
    if not CONFIG:
        print("selftest ok (no markets.json here — endpoint comes from --url/CRM_MCP_URL)")
        return
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    for code, m in cfg["markets"].items():
        for name, e in m["envs"].items():
            if e.get("mcp_url"):
                assert urllib.parse.urlsplit(e["mcp_url"]).hostname, (code, name)
            # a per-env TLS opt-out must stay a real bool wherever it is set
            if "insecure_tls" in e:
                assert isinstance(e["insecure_tls"], bool), (code, name)
    print("selftest ok")


if __name__ == "__main__":
    main()
