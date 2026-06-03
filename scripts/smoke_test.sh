#!/usr/bin/env bash
# Phase-1 acceptance check: hit the SearXNG JSON API with a representative query
# set and report result-count + latency per query. Run ON free-arm-vm after deploy.
#
#   ./scripts/smoke_test.sh                       # default http://127.0.0.1:8080
#   ./scripts/smoke_test.sh http://127.0.0.1:8080
#
# Requires: curl, jq.  PASS = every query returns >=3 results.
set -euo pipefail
BASE="${1:-http://127.0.0.1:8080}"

queries=(
  # general
  "best noise cancelling headphones 2026"
  "how does RAFT consensus work"
  "OCI always free arm ampere a1 limits"
  "site reliability engineering error budget"
  "what is a reranker cross-encoder"
  # medical
  "neoadjuvant immunotherapy head and neck squamous cell carcinoma trial"
  "GLP-1 agonists cardiovascular outcomes 2025"
  "sentinel lymph node biopsy oral cavity cancer guidelines"
  "ctDNA minimal residual disease surveillance"
  "CDS non-device clinical decision support FDA guidance"
  # ai / eng
  "model context protocol streamable http transport"
  "SearXNG datacenter IP google captcha mitigation"
  "qdrant hybrid search bm25 dense"
  "bge-reranker-v2-m3 latency cpu"
  "crawl4ai vs trafilatura extraction"
  # finance / misc
  "treasury yield curve inversion 2026"
  "factor investing momentum vs value"
  "Caddy reverse proxy basic auth bcrypt"
  "tailscale funnel vs reverse proxy"
  "docker compose arm64 multi-arch images"
)

pass=0; fail=0; total_ms=0
printf "%-6s %-7s %-9s %s\n" "RES" "MS" "STATUS" "QUERY"
for q in "${queries[@]}"; do
  t0=$(date +%s%3N)
  resp=$(curl -s --max-time 20 -G "$BASE/search" \
            --data-urlencode "q=$q" --data-urlencode "format=json" || echo '{}')
  t1=$(date +%s%3N); ms=$((t1 - t0)); total_ms=$((total_ms + ms))
  n=$(echo "$resp" | jq '(.results // []) | length' 2>/dev/null || echo 0)
  if [ "${n:-0}" -ge 3 ]; then status="PASS"; pass=$((pass+1)); else status="FAIL"; fail=$((fail+1)); fi
  printf "%-6s %-7s %-9s %s\n" "$n" "$ms" "$status" "$q"
done
echo "------------------------------------------------------------"
echo "PASS=$pass FAIL=$fail  avg=$((total_ms / ${#queries[@]}))ms over ${#queries[@]} queries"
[ "$fail" -eq 0 ] && echo "RESULT: GREEN — gateway is serving." || echo "RESULT: check engine tuning (see README troubleshooting)."
