# 🔎 Search Gateway

Self-hosted open-web search for Claude + Hermes, replacing the Exa/Tavily dependency.
One HTTP MCP endpoint backed by SearXNG (borrows 70+ engine indexes, $0 marginal, no quota).

- **Project:** AI Area → 🔎 Self-Hosted Search Gateway
- **ADR / decision:** `Engineering & AI Systems/ADR-Self-Hosted-Search-Gateway_2026-06-03.md`
- **Host:** `free-arm-vm` (OCI Always Free, 4 OCPU / 24 GB, arm64) — shares the box with Hermes + host Caddy.
- **Adopted base:** [`ihor-sokoliuk/mcp-searxng`](https://github.com/ihor-sokoliuk/mcp-searxng) (MIT).

```
SearXNG (discovery) ──> mcp-searxng (search + url_read, HTTP /mcp) ──> host Caddy (TLS+auth) ──> Claude / Hermes
        │                                                                         
        └── Valkey (cache)                  Phase 2 adds: reranker · Crawl4AI extract · Exa/Tavily fallback w/ cost cap
```

## Architecture notes
- **Two clients, one service.** The MCP runs in HTTP transport (`MCP_HTTP_PORT=3000`), so both Claude (remote, via Caddy) and Hermes (local, `127.0.0.1:3000`) use the same instance.
- **No second Caddy.** `free-arm-vm` already has a host Caddy — we expose the MCP on localhost and add a reverse-proxy block to the existing Caddy. The bundled `caddy` service stays commented out.
- **No on-box LLM.** Synthesis is the client's job; we don't run Ollama and compete with Hermes for RAM.

## Deploy (run on free-arm-vm)

```bash
# 0. Get the bundle onto the VM (rsync from your Mac, or git):
#    rsync -av ~/Projects/personal/search-gateway/ free-arm-vm:~/search-gateway/
cd ~/search-gateway

# 1. SearXNG secret
sed -i "s|REPLACE_WITH_OPENSSL_RAND_HEX_32|$(openssl rand -hex 32)|" searxng/settings.yml

# 2. Bring up the stack
docker compose up -d
docker compose ps

# 3. Acceptance check (needs: jq)
chmod +x scripts/smoke_test.sh && ./scripts/smoke_test.sh
#    Expect RESULT: GREEN. If FAILs cluster on one engine, see Troubleshooting.

# 4. Front it with your existing host Caddy
#    - point an A record (e.g. search.yourdomain) at YOUR-VM-IP
#    - hash a password:
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'YOUR_STRONG_PASS'
#    - paste hash + hostname into caddy/Caddyfile, append that block to your host Caddyfile, then:
#      caddy reload   (or: systemctl reload caddy)
curl -u gateway:YOUR_STRONG_PASS https://search.yourdomain/health   # -> ok
```

## Wire the clients
See `clients/mcp-client-config.md`. Add `search-gateway` to Claude Code, Claude Desktop, and Hermes; make it the default web-search path; keep Exa/Tavily connected-but-idle until parity is confirmed.

## Operations
- **Logs:** `docker compose logs -f searxng mcp-searxng`
- **Update:** `docker compose pull && docker compose up -d`
- **Health for monitoring:** add `https://search.yourdomain/health` to Uptime Kuma and a Healthchecks ping (consistent with the Server Management stack).
- **RAM watch:** Crawl4AI (Phase 2) launches headless Chromium — cap concurrency so it doesn't starve Hermes. Phase-1 stack idles ~0.5–1 GB.

## Troubleshooting
- **Many FAILs / Google CAPTCHAs:** expected from a datacenter IP. Lean on Brave/DDG/Bing/Startpage (already enabled); disable the worst offender in `searxng/settings.yml`. This is exactly what the Phase-2 Exa/Tavily fallback is reserved for.
- **`format=json` 403:** ensure `search.formats` includes `json` and `server.limiter: false` in settings.yml; `docker compose restart searxng`.
- **Client can't auth:** verify `GATEWAY_BASIC_B64 = base64("user:pass")` matches the password you hashed into the Caddyfile.

## Tools (multi-source router — `gateway/app.py`)
- **`web_search`** — cost-conscious default: SearXNG (free) + FlashRank rerank + domain-authority prior; paid/grounded fallback **Gemini → Tavily → Exa** only when results are thin, each behind a monthly free-tier cap. Normally $0.
- **`deep_search`** — deliberate full fan-out: SearXNG + Gemini + Tavily + Exa **in parallel** → merge → dedupe → rerank. Spends quota (within caps); use for hard queries.
- **`web_read`** — trafilatura extraction (free); optional Crawl4AI render if `CRAWL4AI_URL` set.

Caps (monthly, free-tier-safe): Gemini 4,500 · Tavily 800 · Exa 800. Each provider needs its key in `.env` (Exa set; add `TAVILY_API_KEY`, `GEMINI_API_KEY`). Budgets tracked in Valkey, reported in every search response.

## Roadmap / status
- **Phase 1 — done.** SearXNG + adopted mcp-searxng, clients wired, GREEN smoke test.
- **Phase 2 — done.** Multi-source router `sg-gateway` (`127.0.0.1:3001`) live; tailnet `:8443` points to it; tools `web_search`/`deep_search`/`web_read`. Polish done: domain-authority rerank prior; Hermes routed via gateway MCP; redundant `sg-mcp` stopped; Crawl4AI as opt-in (`CRAWL4AI_URL`).
  - *Pending your keys:* add `TAVILY_API_KEY` + `GEMINI_API_KEY` to `~/search-gateway/.env` then `docker compose up -d gateway` to light up Tavily + Gemini.
- **Fork B — parked (recorded, dropped for now):** owned-corpus semantic engine (Qdrant) — belongs to Agentic Engineering · research engine, not this project.

## Phase 2 ops
- Enable/disable paid fallback: set/clear `EXA_API_KEY` (or `TAVILY_API_KEY`) in `~/search-gateway/.env`, then `docker compose up -d gateway`. Budget cap = `MONTHLY_FALLBACK_BUDGET` (default 500), tracked per month in Valkey db1.
- Rebuild after code changes: `docker compose build gateway && docker compose up -d gateway`.
