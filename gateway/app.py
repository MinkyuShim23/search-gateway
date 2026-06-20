"""
Search Gateway — multi-source router (the differentiated layer we own).

Tiers:
  web_search   — DEFAULT. SearXNG + Gemini (Google Search grounding) in parallel
                 -> rerank. Both are free (SearXNG unlimited, Gemini 5k/mo), so
                 this is the everyday path. Tavily/Exa are a thin safety-net
                 fallback only when the default comes back sparse.
  deep_search  — AGENTIC. Gemini decomposes the task into sub-queries; we fan
                 out SearXNG + Gemini across every sub-query and hit Tavily + Exa
                 once on the main query -> merge -> dedupe -> rerank. Bounded
                 (one planning round) so it can't run away; the real iterative
                 agent loop stays in the client (Claude/Hermes).
  web_read     — trafilatura extraction (free); optional Crawl4AI render hook.

Rules: free first; paid providers only with a key AND monthly budget; graceful
degradation everywhere; synthesis is the client's job.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from mcp.server.fastmcp import FastMCP

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8080").rstrip("/")
VALKEY_URL = os.getenv("VALKEY_URL", "redis://valkey:6379/1")
MCP_PORT = int(os.getenv("MCP_PORT", "3001"))
MIN_RESULTS = int(os.getenv("MIN_RESULTS", "3"))

EXA_API_KEY = os.getenv("EXA_API_KEY", "").strip()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip()
# Search grounding needs a billing-enabled project (free 5k/mo allocation, $0 under cap);
# a pure free-tier key 429s on grounding. Plain Gemini (deep_search planning) works regardless.
GEMINI_GROUNDING = os.getenv("GEMINI_GROUNDING", "1") == "1"

CAPS = {
    "gemini": int(os.getenv("GEMINI_CAP", "4800")),  # free 5,000/mo — used in the default path
    "tavily": int(os.getenv("TAVILY_CAP", "800")),  # free 1,000/mo — deep + thin fallback
    "exa": int(os.getenv("EXA_CAP", "800")),  # free 1,000/mo — deep + thin fallback
}
THIN_FALLBACK = [p.strip() for p in os.getenv("THIN_FALLBACK", "tavily,exa").split(",")]
DEEP_SUBQUERIES = int(os.getenv("DEEP_SUBQUERIES", "3"))
CRAWL4AI_URL = os.getenv("CRAWL4AI_URL", "").strip()
SEARCH_TTL = int(os.getenv("SEARCH_TTL", "3600"))
READ_TTL = int(os.getenv("READ_TTL", "86400"))

BOOST = (
    ".gov",
    ".edu",
    "nih.gov",
    "nejm.org",
    "nature.com",
    "sciencedirect",
    "jamanetwork",
    "thelancet",
    "cell.com",
    "springer",
    "wiley",
    "bmj.com",
    "clinicaltrials.gov",
    "arxiv.org",
    "acm.org",
    "ieee.org",
    "pubmed",
    "wikipedia.org",
    "modelcontextprotocol.io",
    "docs.",
    "developer.",
)
PENALTY = (
    "youtube.com",
    "youtu.be",
    "pinterest.",
    "facebook.com",
    "tiktok.com",
    "sketchfab.com",
    "quora.com",
    "instagram.com",
)

try:
    import redis

    _cache = redis.from_url(VALKEY_URL, decode_responses=True)
    _cache.ping()
except Exception:
    _cache = None

try:
    from flashrank import Ranker, RerankRequest

    _ranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2", cache_dir="/tmp/flashrank")
except Exception:
    _ranker = None

try:
    import trafilatura
except Exception:
    trafilatura = None

mcp = FastMCP("search-gateway", host="0.0.0.0", port=MCP_PORT)


# ---- cache + budget ------------------------------------------------------
def _cget(k):
    if not _cache:
        return None
    try:
        v = _cache.get(k)
        return json.loads(v) if v else None
    except Exception:
        return None


def _cset(k, v, ttl):
    if _cache:
        try:
            _cache.setex(k, ttl, json.dumps(v))
        except Exception:
            pass


def _month():
    return dt.date.today().strftime("%Y-%m")


def _budget_ok(p):
    cap = CAPS.get(p, 0)
    if cap <= 0:
        return False
    if not _cache:
        return True
    try:
        return int(_cache.get(f"sg:fb:{p}:{_month()}") or 0) < cap
    except Exception:
        return True


def _budget_spend(p):
    if _cache:
        try:
            k = f"sg:fb:{p}:{_month()}"
            _cache.incr(k)
            _cache.expire(k, 60 * 60 * 24 * 40)
        except Exception:
            pass


def _budget_report():
    out = {}
    for p, cap in CAPS.items():
        used = 0
        if _cache:
            try:
                used = int(_cache.get(f"sg:fb:{p}:{_month()}") or 0)
            except Exception:
                pass
        out[p] = {"used": used, "cap": cap}
    return out


# ---- providers -----------------------------------------------------------
def _searxng(query: str) -> list[dict]:
    r = httpx.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=20.0)
    r.raise_for_status()
    return [
        {
            "title": x.get("title", ""),
            "url": x.get("url", ""),
            "content": x.get("content", "") or "",
            "engine": x.get("engine", ""),
            "source": "searxng",
        }
        for x in r.json().get("results", [])
    ]


def _tavily(query: str, k: int) -> list[dict]:
    r = httpx.post(
        "https://api.tavily.com/search",
        json={"api_key": TAVILY_API_KEY, "query": query, "max_results": k},
        timeout=20.0,
    )
    r.raise_for_status()
    return [
        {
            "title": x.get("title", ""),
            "url": x.get("url", ""),
            "content": x.get("content", "") or "",
            "engine": "tavily",
            "source": "tavily",
        }
        for x in r.json().get("results", [])
    ]


def _exa(query: str, k: int) -> list[dict]:
    r = httpx.post(
        "https://api.exa.ai/search",
        headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"},
        json={"query": query, "numResults": k, "contents": {"text": {"maxCharacters": 600}}},
        timeout=20.0,
    )
    r.raise_for_status()
    return [
        {
            "title": x.get("title", ""),
            "url": x.get("url", ""),
            "content": (x.get("text") or "")[:600],
            "engine": "exa",
            "source": "exa",
        }
        for x in r.json().get("results", [])
    ]


def _gemini(query: str, k: int) -> list[dict]:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    r = httpx.post(
        url,
        json={"contents": [{"parts": [{"text": query}]}], "tools": [{"google_search": {}}]},
        timeout=45.0,
    )
    r.raise_for_status()
    cand = (r.json().get("candidates") or [{}])[0]
    answer = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    chunks = cand.get("groundingMetadata", {}).get("groundingChunks", []) or []
    out = []
    for c in chunks[:k]:
        w = c.get("web", {})
        if w.get("uri"):
            out.append(
                {
                    "title": w.get("title", ""),
                    "url": w["uri"],
                    "content": (answer[:300] if not out else w.get("title", "")),
                    "engine": "gemini",
                    "source": "gemini-grounding",
                }
            )
    return out


def _gemini_plan(query: str, n: int) -> list[str]:
    """Decompose a task into focused sub-queries (1 cheap Gemini call). Empty on failure."""
    if not (GEMINI_API_KEY and _budget_ok("gemini")):
        return []
    try:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        )
        prompt = (
            f"Decompose this into up to {n} focused, diverse web-search sub-queries that "
            f"together cover it. Return ONLY a JSON array of strings.\n\nTask: {query}"
        )
        r = httpx.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20.0)
        r.raise_for_status()
        _budget_spend("gemini")
        txt = "".join(
            p.get("text", "")
            for p in r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        )
        s, e = txt.find("["), txt.rfind("]")
        arr = json.loads(txt[s : e + 1]) if s >= 0 and e > s else []
        return [str(x) for x in arr][:n]
    except Exception:
        return []


def _run(name: str, query: str, k: int) -> list[dict]:
    """Unified provider call with budget gating + graceful failure."""
    try:
        if name == "searxng":
            return _searxng(query)
        if name == "gemini" and GEMINI_GROUNDING and GEMINI_API_KEY and _budget_ok("gemini"):
            res = _gemini(query, k)
            _budget_spend("gemini")
            return res
        if name == "tavily" and TAVILY_API_KEY and _budget_ok("tavily"):
            res = _tavily(query, k)
            _budget_spend("tavily")
            return res
        if name == "exa" and EXA_API_KEY and _budget_ok("exa"):
            res = _exa(query, k)
            _budget_spend("exa")
            return res
    except Exception:
        return []
    return []


# ---- ranking -------------------------------------------------------------
def _domain_prior(url: str) -> float:
    u = (url or "").lower()
    if any(b in u for b in BOOST):
        return 0.04
    if any(p in u for p in PENALTY):
        return -0.06
    return 0.0


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = (it.get("url") or "").split("#")[0].rstrip("/").lower()
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _rerank(query: str, items: list[dict], limit: int) -> list[dict]:
    items = _dedupe(items)
    if not items:
        return []
    if _ranker:
        try:
            passages = [
                {"id": i, "text": f"{it['title']} {it['content']}"[:1000]}
                for i, it in enumerate(items)
            ]
            ranked = _ranker.rerank(RerankRequest(query=query, passages=passages))
            scored = []
            for p in ranked:
                it = dict(items[p["id"]])
                it["score"] = round(float(p["score"]) + _domain_prior(it["url"]), 4)
                scored.append(it)
            scored.sort(key=lambda x: x["score"], reverse=True)
            return scored[:limit]
        except Exception:
            pass
    for it in items:
        it["score"] = round(0.5 + _domain_prior(it["url"]), 4)
    items.sort(key=lambda x: x["score"], reverse=True)
    return items[:limit]


def _parallel(jobs, max_workers=6):
    """jobs: list of (provider_name, query, k). Returns (items, sources_used set)."""
    items, sources = [], set()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_run, p, q, k): p for (p, q, k) in jobs}
        for f in as_completed(futs):
            got = f.result()
            if got:
                items += got
                sources.add(futs[f])
    return items, sources


# ---- tools ---------------------------------------------------------------
@mcp.tool()
def web_search(query: str, limit: int = 8) -> str:
    """Default web search (fast, free, everyday): SearXNG + rerank. SearXNG already
    queries Google/Bing/Brave/etc. Tavily/Exa fire only as a thin fallback when results
    are sparse. (Gemini grounding is ~40s, so it lives in deep_search, not here.)
    Returns JSON {query,count,reranked,sources_used,budgets,results:[...]}."""
    ckey = f"sg:search:{limit}:{query}"
    if c := _cget(ckey):
        return json.dumps(c, ensure_ascii=False)
    # default stays fast (~1s): SearXNG + rerank. Gemini grounding (~40s, multi-search
    # + synthesis) lives in deep_search, not here. SearXNG already queries Google.
    items, used = _parallel([("searxng", query, max(limit, 5))], max_workers=1)
    if len(_dedupe(items)) < MIN_RESULTS:  # thin safety net
        for prov in THIN_FALLBACK:
            got = _run(prov, query, max(limit, 5))
            if got:
                items += got
                used.add(prov)
            if len(_dedupe(items)) >= max(limit, MIN_RESULTS):
                break
    ranked = _rerank(query, items, limit)
    payload = {
        "query": query,
        "count": len(ranked),
        "reranked": _ranker is not None,
        "sources_used": sorted(used),
        "budgets": _budget_report(),
        "results": ranked,
    }
    _cset(ckey, payload, SEARCH_TTL)
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
def deep_search(query: str, limit: int = 10) -> str:
    """Agentic full search: Gemini decomposes the task into sub-queries, then we
    fan out SearXNG + Gemini across every sub-query and hit Tavily + Exa once on
    the main query -> merge -> dedupe -> rerank. Spends quota (within caps); use
    for hard/important questions. Returns the same JSON shape + 'subqueries'."""
    ckey = f"sg:deep:{limit}:{query}"
    if c := _cget(ckey):
        return json.dumps(c, ensure_ascii=False)
    plan = _gemini_plan(query, DEEP_SUBQUERIES)
    jobs = [
        ("searxng", query, 6),
        ("gemini", query, 6),
        ("tavily", query, 6),
        ("exa", query, 6),
    ]  # main query: all 4
    for sq in plan:  # sub-queries: free sources only
        jobs.append(("searxng", sq, 5))
        jobs.append(("gemini", sq, 5))
    items, sources = _parallel(jobs, max_workers=6)
    ranked = _rerank(query, items, limit)
    payload = {
        "query": query,
        "count": len(ranked),
        "reranked": _ranker is not None,
        "subqueries": plan,
        "sources_used": sorted(sources),
        "budgets": _budget_report(),
        "results": ranked,
    }
    _cset(ckey, payload, SEARCH_TTL)
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
def web_read(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return clean main-text. Free trafilatura extraction; falls
    back to a Crawl4AI render service for JS-heavy pages if CRAWL4AI_URL is set."""
    ckey = f"sg:read:{max_chars}:{url}"
    if c := _cget(ckey):
        return c
    text = ""
    try:
        if trafilatura is not None:
            dl = trafilatura.fetch_url(url)
            text = trafilatura.extract(dl, include_links=False, include_comments=False) or ""
    except Exception:
        text = ""
    if (not text or len(text) < 200) and CRAWL4AI_URL:
        try:
            r = httpx.post(f"{CRAWL4AI_URL.rstrip('/')}/crawl", json={"urls": [url]}, timeout=60.0)
            if r.status_code == 200:
                results = (r.json() or {}).get("results") or []
                if results:
                    md = results[0].get("markdown") or {}
                    if isinstance(md, dict):
                        text = md.get("fit_markdown") or md.get("raw_markdown") or text
                    elif isinstance(md, str):
                        text = md or text
        except Exception:
            pass
    text = (text or f"(no extractable content from {url})")[:max_chars]
    _cset(ckey, text, READ_TTL)
    return text


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
