# Wiring the gateway into Claude + Hermes

**Live endpoint (Phase 1, deployed 2026-06-03):** tailnet-only via Tailscale serve.

- MCP URL: `https://YOUR-TAILNET-HOST:8443/mcp`
- Health:  `https://YOUR-TAILNET-HOST:8443/health`
- Tools:   `searxng_web_search`, `web_url_read`
- Auth:    none needed — access is gated by the tailnet (TLS via Tailscale). Keep it tailnet-only; do NOT expose with Funnel.

## Claude Code (done via CLI)
```bash
claude mcp add --transport http search-gateway https://YOUR-TAILNET-HOST:8443/mcp --scope user
claude mcp list      # verify search-gateway: connected
```

## Claude Desktop
Settings → Connectors → Add custom connector → Remote (HTTP):
- URL: `https://YOUR-TAILNET-HOST:8443/mcp`
- No header needed (tailnet-gated). Your Mac must be on the tailnet.

## Hermes (on free-arm-vm)
Hermes shares the box — point it at the container directly, no tailnet hop:
```json
{ "search-gateway": { "type": "http", "url": "http://127.0.0.1:3000/mcp" } }
```

## Cutover
1. Add `search-gateway` to Claude (above) and make it the default web-search path.
2. Keep Exa/Tavily connected-but-idle until a few days of parity.
3. Then demote Exa/Tavily to opt-in fallback (Phase 2 router) and update the AI-Area "Search" ledger line to "detached — gateway live".

## Public-domain alternative (not used; tailnet is simpler)
If you ever want it reachable off-tailnet, front `127.0.0.1:3000` with your host Caddy + basic auth using `caddy/Caddyfile`, behind a real hostname.
