# 🔎 Search Gateway

Self-hosted open-web search for AI agents — one MCP endpoint that detaches Claude + Hermes
from paid search APIs. SearXNG (70+ engine indexes, $0 marginal, no quota) is the base; a small
router adds reranking, a budget-capped paid fallback, and page extraction. Normal queries cost **$0**.

- **Built on:** SearXNG (AGPL, run as an upstream container — not vendored) for discovery; the in-repo `gateway/` router is the layer this project owns. Phase 1 used [`ihor-sokoliuk/mcp-searxng`](https://github.com/ihor-sokoliuk/mcp-searxng) (MIT), since replaced by `gateway/app.py`.
- **Host:** any small box (built on an OCI Always Free arm64 VM, 4 OCPU / 24 GB, shared with other agents). Exposed **tailnet-only** via Tailscale serve (`:8443`) — no public surface.

```
SearXNG (70+ engines) ─┐
Gemini grounding ───────┤   (deep_search only)
Tavily / Exa (capped) ──┴─> sg-gateway router ─> Tailscale serve :8443 ─> Claude / Hermes
                                  │   tools: web_search · deep_search · web_read
                                  └── Valkey (cache + monthly budget counters)
```

## Tools (multi-source router — `gateway/app.py`)
- **`web_search`** — fast, free, everyday default (~1s): SearXNG + FlashRank rerank + domain-authority prior. SearXNG already covers Google/Bing/Brave/etc. **No Gemini grounding here** (it's ~40s — reserved for `deep_search`); Tavily then Exa fire only as a thin fallback when results come back sparse (< `MIN_RESULTS`, default 3). Normally $0.
- **`deep_search`** — agentic full fan-out: Gemini decomposes the task into sub-queries (`DEEP_SUBQUERIES`, default 3), then SearXNG + Gemini fan out across every sub-query and Tavily + Exa fire once on the main query → merge → dedupe → rerank. Bounded to one planning round (the iterative loop stays in the client). Spends quota within caps; use for hard queries.
- **`web_read`** — trafilatura extraction (free); optional Crawl4AI render if `CRAWL4AI_URL` is set.

Monthly caps (free-tier-safe, override via env): **Gemini 4,800** (`GEMINI_CAP`) · **Tavily 800** (`TAVILY_CAP`) · **Exa 800** (`EXA_CAP`). Each provider activates only when its key is present **and** its cap has room; budgets are tracked per month in Valkey and reported in every search response. All keys are optional — SearXNG alone is a working $0 gateway.

## Architecture notes
- **Two clients, one service.** The router runs HTTP transport (`MCP_HTTP_PORT=3000`), so Claude (remote, over the tailnet) and Hermes (local, `127.0.0.1:3000`) share one instance.
- **Tailnet-first.** Access is gated by Tailscale (TLS handled by `tailscale serve`), so no auth layer or public DNS is required. A public-domain alternative (host Caddy + basic auth) is available but not the default — see `clients/mcp-client-config.md`.
- **No on-box LLM.** Synthesis is the client's job; we don't run Ollama and compete for RAM.

## Deploy

```bash
# 0. Get the bundle onto the host (rsync from your Mac, or git clone):
#    rsync -av ./search-gateway/ your-host:~/search-gateway/
cd ~/search-gateway

# 1. SearXNG secret
sed -i "s|REPLACE_WITH_OPENSSL_RAND_HEX_32|$(openssl rand -hex 32)|" searxng/settings.yml

# 2. (optional) add provider keys for the paid fallback / deep_search
cp .env.example .env && $EDITOR .env      # all keys optional; SearXNG works with none

# 3. Bring up the stack
docker compose up -d && docker compose ps

# 4. Acceptance check (needs: jq)
chmod +x scripts/smoke_test.sh && ./scripts/smoke_test.sh   # expect RESULT: GREEN

# 5. Expose it tailnet-only (recommended — no public surface, TLS via Tailscale)
tailscale serve --bg --https=8443 127.0.0.1:3000
HOST=$(tailscale status --json | jq -r .Self.DNSName | sed 's/\.$//')
curl https://$HOST:8443/health    # -> ok
#    (Public-domain alternative via host Caddy + basic auth: see clients/mcp-client-config.md.)
```

## Wire the clients
See `clients/mcp-client-config.md`. Add `search-gateway` to Claude Code, Claude Desktop, and Hermes; make it the default web-search path. (Tip: keep any paid search connectors idle until you've confirmed parity.)

## Operations
- **Logs:** `docker compose logs -f gateway searxng`
- **Update:** `docker compose pull && docker compose up -d`
- **Rebuild after code changes:** `docker compose build gateway && docker compose up -d gateway`
- **Toggle a paid provider:** set/clear `GEMINI_API_KEY` / `TAVILY_API_KEY` / `EXA_API_KEY` in `.env`, then `docker compose up -d gateway`.
- **Health for monitoring:** point Uptime Kuma + a Healthchecks ping at `https://<tailnet-host>:8443/health`.
- **RAM watch:** Crawl4AI (optional) launches headless Chromium — cap concurrency so it doesn't starve co-tenants. The base stack idles ~0.5–1 GB.

## Troubleshooting
- **Many FAILs / Google CAPTCHAs:** expected from a datacenter IP. Lean on Brave/DDG/Bing/Startpage (already enabled); disable the worst offender in `searxng/settings.yml`. This is what the Tavily/Exa fallback is reserved for.
- **`format=json` 403:** ensure `search.formats` includes `json` and `server.limiter: false` in `searxng/settings.yml`; `docker compose restart searxng`.
- **Gemini grounding 429s:** grounding needs a billing-enabled Google project (free 5k/mo allocation, $0 under cap); a pure free-tier key 429s on grounding. Plain Gemini (deep_search planning) works regardless.

## Roadmap / status
- **Phase 1 — done.** SearXNG base, clients wired, GREEN smoke test.
- **Phase 2 — done.** Multi-source router live (`127.0.0.1:3000`, tailnet `:8443`); tools `web_search` / `deep_search` / `web_read`; domain-authority rerank prior; Hermes routed via the gateway MCP; Crawl4AI as an opt-in extractor (`CRAWL4AI_URL`).
- **Parked:** owned-corpus semantic engine (Qdrant) — belongs to a separate research-engine project, not this one.

## License
MIT (this repo's own code — see `LICENSE`). SearXNG is AGPL-3.0 and is run as an unmodified upstream container, not redistributed here.
