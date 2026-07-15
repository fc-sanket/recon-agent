#!/usr/bin/env python3
"""
Recon Agent
Textual TUI — chat while the scan runs.

Usage:
  python3 auto_recon.py                          # launch TUI
  python3 auto_recon.py "do recon on iocl.com"  # start immediately
  python3 auto_recon.py --resume                 # resume last session
"""
from __future__ import annotations
import asyncio, json, os, subprocess, sys, datetime, textwrap
from pathlib import Path

# ── .env loader ───────────────────────────────────────────────────────────────
def _load_env():
    for p in [Path(__file__).parent / ".env", Path.home() / ".env"]:
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            break
_load_env()

# ── deps ──────────────────────────────────────────────────────────────────────
try:
    import anthropic
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "anthropic>=0.116.0"])
    import anthropic

try:
    from textual.app import App, ComposeResult
    from textual.widgets import Header, Footer, RichLog, Input, Label
    from textual.containers import Horizontal
    from textual import work
    from textual.reactive import reactive
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "textual>=0.80.0"])
    from textual.app import App, ComposeResult
    from textual.widgets import Header, Footer, RichLog, Input, Label
    from textual.containers import Horizontal
    from textual import work
    from textual.reactive import reactive

KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not KEY:
    sys.exit("\n✗  ANTHROPIC_API_KEY not set — add it to .env\n")

MODEL        = os.environ.get("RECON_MODEL", "claude-sonnet-4-6")
RECON_ROOT   = Path(__file__).parent          # /home/kali/recon_agent
SESSION_DIR  = RECON_ROOT / "sessions"
RESULTS_DIR  = RECON_ROOT / "results"
SESSION_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

# ── pricing ───────────────────────────────────────────────────────────────────
_COST_IN  = 3.0  / 1_000_000   # claude-sonnet-4-6
_COST_OUT = 15.0 / 1_000_000
_TOK_IN   = 0
_TOK_OUT  = 0
_WHOXY_REV_START: int | None = None   # reverse balance at scan start

def _add_tok(i, o):
    global _TOK_IN, _TOK_OUT
    _TOK_IN += i; _TOK_OUT += o

def _cost() -> str:
    c = _TOK_IN * _COST_IN + _TOK_OUT * _COST_OUT
    return f"  {_TOK_IN:,} in / {_TOK_OUT:,} out  ≈ ${c:.3f}"

def _fetch_whoxy_reverse() -> int | None:
    """Fetch current WhoXY reverse_whois_balance. Returns None on error."""
    whoxy = os.environ.get("WHOXY_API_KEY", "")
    if not whoxy:
        return None
    try:
        import subprocess as _sp
        r = _sp.run(["curl", "-sk", "--max-time", "5",
                     f"https://api.whoxy.com/?key={whoxy}&account=balance"],
                    capture_output=True, text=True)
        return int(json.loads(r.stdout).get("reverse_whois_balance", 0))
    except Exception:
        return None

def _whoxy_usage_str() -> str:
    """Return a human-readable WhoXY credit usage string for end-of-scan summary."""
    if _WHOXY_REV_START is None:
        return ""
    current = _fetch_whoxy_reverse()
    if current is None:
        return ""
    used = _WHOXY_REV_START - current
    cost = used * 0.01
    return f"  WhoXY: {used:+d} reverse credits used  (${cost:.2f})  remaining={current:,}"

# ── system prompt ─────────────────────────────────────────────────────────────
SYSTEM = """You are Recon Agent — a senior OSINT analyst running on Kali Linux.

━━━ GOAL ━━━
Discover every root domain owned by the target company. Root domains only (never subdomains).
Final deliverable: a report categorising every candidate as INCLUDED / EXCLUDED / DANGLING.

━━━ TOOLS — when to use each ━━━
  bash        — run shell commands; execute Python scripts you wrote to disk
  write_file  — write batch .py scripts before running (required for all multi-domain ops)
  read_file   — read pool files, checklist, methodology.md, results
  web_fetch   — fetch a URL you already know (API endpoints, specific pages)
  web_search  — find things you don't have a URL for: subsidiary lists, annual reports,
                press releases, company pages, government filings. Use early and often.

━━━ SETUP — run this first, every session ━━━
```bash
export ORG_NAME="<full legal company name>"
export PRIMARY_DOMAIN="<primary domain>"
export PRIMARY_SLD="<sld only, e.g. hcltech>"
export ENGAGEMENT_DIR="/home/kali/recon_agent/results/recon_${PRIMARY_DOMAIN//./_}"
export POOL="$ENGAGEMENT_DIR/pool"
export SCRIPTS="$ENGAGEMENT_DIR/scripts"
export PROV="$ENGAGEMENT_DIR/provenance.jsonl"
mkdir -p "$POOL" "$SCRIPTS" "$ENGAGEMENT_DIR/downloads" "$ENGAGEMENT_DIR/reports"
pip3 install -q requests tldextract dnspython mmh3 2>/dev/null
export PATH="$PATH:/home/kali/go/bin:/usr/local/go/bin"
apt-get install -y whois jq poppler-utils -qq 2>/dev/null

# Provenance helper — use instead of bare echo >> pool
prov() {
    # Usage: prov DOMAIN STEP SOURCE VIA SIGNAL [CONFIDENCE]
    local d="$1" step="$2" src="$3" via="$4" sig="$5" conf="${6:-medium}"
    echo "$d" >> "$POOL/${step}.txt"
    printf '{"domain":"%s","step":"%s","source":"%s","via":"%s","signal":"%s","confidence":"%s","ts":"%s"}\n' \
        "$d" "$step" "$src" "$via" "$sig" "$conf" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$PROV"
}
export -f prov

# Seed the primary domain with provenance
prov "$PRIMARY_DOMAIN" "step00_primary" "seed" "user_input" "primary_domain" "high"

# If resuming: list what's already been found
ls "$POOL"/ 2>/dev/null && wc -l "$PROV" 2>/dev/null && echo "--- pool contents above ---"
```

Python batch scripts must paste this snippet at the top to record provenance:
```python
import json, datetime, os, pathlib
_pool = pathlib.Path(os.environ['POOL'])
_prov = pathlib.Path(os.environ['ENGAGEMENT_DIR']) / 'provenance.jsonl'
def log_prov(domain, step, source, via, signal, confidence="medium"):
    (_pool / f"{step}.txt").open("a").write(domain + "\n")
    _prov.open("a").write(json.dumps({
        "domain": domain, "step": step, "source": source,
        "via": via, "signal": signal, "confidence": confidence,
        "ts": datetime.datetime.utcnow().isoformat() + "Z"
    }) + "\n")
```

━━━ CRITICAL RULES ━━━

0. MINIMIZE TURNS — every round-trip re-sends the full conversation history and costs money.
   TARGET: complete an entire phase in ≤5 turns. Never use more than 1 turn per checklist item.

   MULTI-TOOL RULE: when 2+ checklist items are independent (no data dependency between them),
   issue ALL their tool calls in a SINGLE response as parallel tool invocations.
   Example — if Phase 2 has 5 unchecked items that don't depend on each other:
     Turn N:  bash(favicon_hash_script) + bash(dns_pivot_script) + bash(gau_script)
              + web_search(annual_report) + web_fetch(crtsh_url)     ← 5 tools, 1 turn
     Turn N+1: bash(asn_script) + bash(http_headers_script)          ← remaining 2, 1 turn

   NEVER do:
     Turn N:   bash(favicon_hash_script)
     Turn N+1: bash(dns_pivot_script)       ← WASTEFUL — 2 turns for 2 independent operations
     Turn N+2: bash(gau_script)             ← WASTEFUL

   The only exception: when step B REQUIRES the output of step A (data dependency).
   If step B doesn't need A's results to proceed, they must run in the same turn.

   PHASE COMPLETION TARGET:
     Phase 1 (7 items) → ≤3 turns
     Phase 2 (7 items) → ≤3 turns
     Phase 3 (4 items) → ≤2 turns
     Phase 4 (7 items) → ≤3 turns
     Phase 5 (5 items) → ≤2 turns
     Phase 6 (5 items) → ≤2 turns
   Total: ≤15 turns for a full scan. Exceed this only with explicit justification.

1. BATCH EVERYTHING — the bash tool rejects single-domain whois/dig/curl/wget.
   Always write a Python script to disk that loops over ALL domains, then run it once.
     WRONG: whois example.com          → REJECTED
     WRONG: curl -sk https://x.com     → REJECTED
     RIGHT: write_file $SCRIPTS/verify.py  (loops 30 domains)  →  bash: python3 .../verify.py

2. ROOT DOMAINS ONLY — strip subdomains. api.example.com → example.com

3. STAKE CHECK — for every subsidiary/JV:
   Check BSE/NSE → annual report → MCA → Wikipedia for ownership %.
   >50% stake = INCLUDE + recurse. ≤50% (including exactly 50%) = EXCLUDE.
   Log: echo "domain|company|stake%" >> $ENGAGEMENT_DIR/stake_register.txt

4. VERIFY OWNERSHIP before including — DNS resolving is not enough.
   Need at least one of: WHOIS registrant match / SSL cert org match / HTTP branding.
   If unsure: use web_fetch to read the page title + copyright footer. Never guess.

5. WHOIS REGISTRAR TRAP — "Endurance International Group India Pvt Ltd" = BigRock/HostGator
   registrar, NOT the domain owner. Check HTTP content before concluding ownership.

6. BRAND VARIANTS ≠ owned — popular brand names are used by unrelated companies worldwide.
   Always WHOIS a brand variant hit. Registrant ≠ target → EXCLUDE.

7. RECURSE — after confirming any >50% subsidiary domain: run crt.sh + WhoXY on it immediately.

8. DEDUP — before any verification step, sort -u all pool files into a single candidate list.
   Never run WHOIS/DNS on the same domain twice.

9. crt.sh PAGINATION — crt.sh caps results. Always query BOTH:
   - Wildcard: https://crt.sh/?q=%.{domain}&output=json
   - Org name: https://crt.sh/?o={encoded_org}&output=json
   Parse all pages (loop until empty page).

10. WHOXY CREDIT CONSERVATION — reverse WHOIS = 1 credit/page ($0.01 each; budget 10k credits):

    BALANCE PRE-FLIGHT (run ONCE before any reverse query):
      REV=$(curl -sk "https://api.whoxy.com/?key=$WHOXY_API_KEY&account=balance" | \
            python3 -c "import json,sys; print(json.load(sys.stdin).get('reverse_whois_balance',0))")
      echo "WhoXY reverse balance: $REV"
      If REV < 50 → log in COVERAGE GAPS, skip ALL reverse steps, continue to Phase 5.

    SCAN BUDGET CAP — max 30 reverse credits per scan (= $0.30), regardless of how many
    qualifying emails/names are found. Track credits_used as you go. When credits_used >= 30,
    stop all further reverse queries and log "WHOXY SCAN BUDGET EXHAUSTED" in COVERAGE GAPS.
    This prevents account blowout even if many qualifying emails are discovered.

    COLLECT ALL SEEDS FIRST — before issuing any reverse query, run live WHOIS on ALL
    confirmed domains in one batch script. Collect every registrant email + company name.
    Only then filter and deduplicate. Never split into two rounds (primary first, subsidiaries later).

    WHICH emails to reverse-query (FILTER STRICTLY before issuing any query):
      ✓ DO: emails ending @{primary_sld}.*   e.g. reg@iocl.co.in, admin@indianoil.com
      ✓ DO: emails ending @{confirmed_>50%_subsidiary_sld}.*
      ✗ SKIP privacy proxies: domainsbyproxy.com, whoisguard.com, privacyprotect.org,
              contactprivacy.com, withheldforprivacy.com, nameprivacy.com, domainprivacygroup.com
      ✗ SKIP webmail / generic: gmail.com, yahoo.com, hotmail.com, outlook.com, protonmail.com
      ✗ SKIP registrar support addresses: *@godaddy.com, *@bigrock.in, *@namecheap.com, etc.
      ✗ SKIP domain brokers / parked: hugedomains.com, sedo.com, afternic.com, dan.com, parkingcrew.net

    WHICH company names to reverse-query:
      ✓ DO: exact PRIMARY company name (once only)
      ✓ DO: confirmed >50% subsidiaries with stake verified via BSE/annual report — max 10 names
      ✗ SKIP: unconfirmed Wikipedia entries, JV partners, associate companies, minority stakes
      ✗ SKIP domain broker / proxy companies: "HugeDomains", "Domains By Proxy", "GoDaddy",
              "NameBright", "Sedo", "Afternic" — these own millions of unrelated domains and will
              exhaust the entire credit budget in one query.

    PRIORITY ORDER (when scan budget is limited):
      1. Primary SLD emails first (highest signal)
      2. Primary company name
      3. Subsidiary SLD emails
      4. Subsidiary company names
      Stop as soon as credits_used >= 30.

    PAGE CAP: max 3 pages per query (= 3 credits max).
      If page 1 returns 0 results → stop immediately (0 credits wasted on empty pages).
      Never paginate beyond page 3 without an explicit reason in your reasoning.

    DEDUP before querying: normalize names (strip "Ltd" "Limited" "Inc" "Corp" "Co." suffixes),
    collect all emails + company names into sets FIRST, then query once per unique normalized value.

11. TIMEOUT recovery — if a bash script times out, split it into smaller batches.
    Don't retry the same large script. Break it: domains[:25], domains[25:50], etc.

12. PARTIAL REPORTS — after every phase write a partial report to $ENGAGEMENT_DIR/reports/
    partial_<phase>.md. If the scan is interrupted, results are not lost.

13. SELF-INSTALL missing tools before using them:
    pip3 install -q <pkg>  or  apt-get install -y <pkg> -qq

14. PROVENANCE — every domain written to pool MUST have a provenance entry in provenance.jsonl.
    In bash: use prov() instead of bare echo >> pool file.
    In Python scripts: paste log_prov() snippet from SETUP and call it instead of open().write().
    Fields: domain | step (filename) | source (tool/API) | via (parent domain/email/cert that led here)
            signal (what matched: cert_san / whois_email / redirect / dns_a / favicon_hash / etc.)
            confidence (high / medium / low)
    Example entries:
      prov "hclsoftware.com" "step01_crtsh" "crt.sh" "*.hcltech.com" "cert_san" "high"
      prov "lankaioc.com"    "step04_whoxy" "whoxy_reverse_email" "reg@iocl.co.in" "whois_email" "high"
      log_prov("actian.com", "step03_builtwith", "builtwith_inbound", "hcltech.com", "verified_redirect", "high")

━━━ REFERENCE (read if needed) ━━━
  /home/kali/recon_agent/methodology.md  — full API URLs, request formats, techniques

━━━ MANDATORY PHASE CHECKLIST ━━━
Write to $ENGAGEMENT_DIR/checklist.txt at start. Mark [DONE] immediately when each step finishes.
A step with 0 results is still [DONE]. Write the final report only after ALL items are [DONE].

  PHASE 1 — SEED COLLECTION
  [ ] crt.sh wildcard + org name search on PRIMARY domain (paginate both queries)
  [ ] Homepage + sitemap + brand/product pages crawled for outbound domain links
        Fetch ALL of these paths (curl each, extract outbound root domains):
          /  /sitemap.xml  /about  /group  /our-brands  /brands  /portfolio
          /subsidiaries  /group/companies  /business-units  /investors
        Follow internal links up to depth 2 on the same domain (BFS, max 30 pages)
        This finds product sub-brand domains (e.g. servolube.in) only listed on brand pages
  [ ] web_search: "<company> annual report subsidiaries site:bseindia.com OR site:nseindia.com"
  [ ] Annual report PDF: download, extract subsidiary/stake table
        Preferred: pdftotext report.pdf - (if installed)
        Fallback:  python3 -c "from pdfminer.high_level import extract_text; print(extract_text('report.pdf'))"
        pdfminer.six is pre-installed and always available.
  [ ] Wikipedia + Wikidata SPARQL: subsidiaries list + ownership %
  [ ] Brand variant DNS sweep: primary SLD across all TLDs + ccTLDs (.co.in .net.in .org.in etc)
  [ ] web_search: site:gov.in "<company name>" to find government-registered domains

  PHASE 2 — TECHNICAL PIVOTS
  [ ] Favicon hash pivot: fetch /favicon.ico → mmh3 hash → Shodan http.favicon.hash:{hash}
  [ ] GA/GTM tracker pivot: grep page source for UA-/GTM-/G- IDs → SpyOnWeb lookup per ID
  [ ] DNS record pivot: dig DMARC/SPF/MTA-STS/CAA on primary + subsidiaries
        DMARC rua: email domain, SPF include: chains, CAA domains, MTA-STS mx: fields
  [ ] HTTP header pivot: curl -skI each confirmed domain → parse CSP/CORS/Link headers
  [ ] GAU + Wayback CDX: historical URLs → extract root domains (lapsed/old brand domains)
        GAU binary: /home/kali/go/bin/gau (already in PATH via SETUP export)
        bash: /home/kali/go/bin/gau --subs {domain}  OR  curl "web.archive.org/cdx/search/cdx?url=*.{domain}..."
  [ ] CRT time-correlation: note primary cert not_before date → find same-org certs issued ±30 days
  [ ] ASN lookup + PTR sweep: bgpview.io/search → get prefixes → mapcidr | dnsx -ptr → root domains

  PHASE 3 — SUBSIDIARY RECURSION (repeat for every >50% subsidiary confirmed)
  [ ] crt.sh wildcard + org search on EACH subsidiary domain
  [ ] ccTLD sweep on EACH subsidiary SLD (.co.in .net.in .org.in .firm.in .gen.in .ind.in)
  [ ] BuiltWith inbound redirect sweep on EACH confirmed domain — MANDATORY, do not skip:
        for d in confirmed_domains:
          curl -sk "https://api.builtwith.com/redirect1/api.json?KEY=$BUILTWITH_API_KEY&LOOKUP={d}"
        ⚠ FIELD NAME: parse response["Inbound"][i]["Domain"]  (NOT Results[].Redirect[].domain_name)
        Strip subdomains with tldextract; filter noise CDNs/ad-networks
  [ ] Stake % confirmed for EVERY entity (BSE/NSE → annual report → MCA → Wikipedia)

  ⚠ PHASE GATE: ALL Phase 3 items must be [DONE] before moving to Phase 4.
     Do NOT proceed to Phase 4 while any Phase 3 item is unchecked.

  PHASE 4 — REGISTRANT PIVOT
  [ ] CREDIT CHECK: run balance pre-flight (Rule 10). If reverse_whois_balance < 50, skip all
        reverse steps below and go directly to Phase 5. Log in COVERAGE GAPS.
  [ ] Batch live WHOIS on ALL confirmed domains (primary + every confirmed subsidiary) in ONE
        script — loop every domain, curl WhoXY live WHOIS, extract registrant_contact.email_address
        + company_name. Do NOT run reverse queries yet. Just collect.
        Command per domain: curl -sk "https://api.whoxy.com/?key=$WHOXY_API_KEY&whois={domain}&format=json"
        Save collected seeds to $ENGAGEMENT_DIR/whoxy_seeds.txt (email|company|source_domain per line)
  [ ] Filter + deduplicate seeds: apply Rule 10 filters (skip privacy proxies, webmail, registrars).
        Normalize company names (strip Ltd/Inc/Corp). Build final query list. Log how many qualify.
        If qualifying queries × 3 > 30 credits → log which ones will be skipped (lowest priority first).
  [ ] Reverse WHOIS on qualifying seeds — follow priority order from Rule 10; track credits_used;
        stop when credits_used >= 30. Max 3 pages per query. Log each query + credits used.
  [ ] amass intel: /home/kali/go/bin/amass intel -d {domain} -whois -src && /home/kali/go/bin/amass intel -org "{ORG_NAME}" -src

  PHASE 5 — ADDITIONAL SOURCES
  [ ] Shodan: SSL cert org search + org: BGP attribution search for primary org name
  [ ] SecurityTrails: associated domains on primary domain
  [ ] IntelX: phonebook search — MANDATORY, do not skip. Run ALL three queries:
        # Query 1: by primary domain
        SEARCH_ID=$(curl -sk -X POST -H "Content-Type: application/json" \
          -d "{\"term\":\"{PRIMARY_DOMAIN}\",\"buckets\":[],\"lookuplevel\":0,\"maxresults\":200,\"timeout\":20,\"datefrom\":\"\",\"dateto\":\"\",\"sort\":4,\"media\":0,\"terminate\":[]}" \
          "https://2.intelx.io/phonebook/search?k=$INTELX_API_KEY" | python3 -c "import json,sys;print(json.load(sys.stdin).get('id',''))")
        sleep 2
        # Query 2: by org name (replace spaces with %20)
        RESULTS=$(curl -sk "https://2.intelx.io/phonebook/search/result?k=$INTELX_API_KEY&id=$SEARCH_ID&limit=200")
        echo "$RESULTS" | python3 -c "
        import json,sys,re,tldextract
        data=json.load(sys.stdin)
        seen=set()
        for s in data.get('selectors',[]):
            val=s.get('selectorvalue','').strip()
            if '@' in val: val=val.split('@')[-1]
            val=re.sub(r'[^a-zA-Z0-9._-]','',val).strip('.')
            ext=tldextract.extract(val)
            if ext.domain and ext.suffix:
                rd=f'{ext.domain}.{ext.suffix}'
                if rd not in seen: seen.add(rd); print(rd)
        " >> $POOL/step05_intelx.txt
        # Repeat search with org name: same POST pattern with term="{ORG_NAME}"
  [ ] VirusTotal: related domains on primary + subsidiary domains
  [ ] URLScan.io: search by domain, ASN, and page.title:"{ORG_NAME}"

  PHASE 6 — VERIFICATION
  [ ] Dedup all pool candidates → single sorted list
  [ ] DNS profile (A + NS + MX) on ALL candidates — one batch script
  [ ] WHOIS + RDAP on ALL candidates — one batch script (RDAP for privacy-masked results)
  [ ] HTTP content check on candidates where WHOIS is unclear
  [ ] Stake register complete: every INCLUDED domain has an entry

━━━ FINAL REPORT FORMAT ━━━
Write to $ENGAGEMENT_DIR/reports/final_report.md:

## INCLUDED — Confirmed owned (>50% stake or direct WHOIS match)
| Domain | Company | Stake | Evidence | Discovered via | NS |

## DANGLING — Registered by target but inactive/parked (takeover risk)
| Domain | Issue | Risk | Discovered via |

## EXCLUDED — Not owned or ≤50% stake
| Domain | Reason |

## COVERAGE GAPS — sources that returned 0 or failed
| Source | Status | Note |

For the "Discovered via" column, read provenance.jsonl:
  python3 -c "
import json, collections
prov = collections.defaultdict(list)
for line in open('$ENGAGEMENT_DIR/provenance.jsonl'):
    e = json.loads(line)
    prov[e['domain']].append(f\"{e['source']} via {e['via']} [{e['confidence']}]\")
for d,srcs in sorted(prov.items()): print(d, '|', ' / '.join(srcs))
  "
"""

# ── tools schema ──────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "bash",
        "description": "Run any shell command on Kali Linux. API keys are auto-exported.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "default": 120}
            },
            "required": ["command"]
        }
    },
    {
        "name": "web_fetch",
        "description": "Fetch a URL and return HTML/text content (up to 15,000 chars).",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}}
            },
            "required": ["url"]
        }
    },
    {
        "name": "read_file",
        "description": "Read a local file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":   {"type": "string"},
                "offset": {"type": "integer", "default": 0},
                "limit":  {"type": "integer", "default": 2000}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write content to a local file. Creates parent directories.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
                "append":  {"type": "boolean", "default": False}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "web_search",
        "description": "Search the web using DuckDuckGo. Returns titles, URLs and snippets. Use for finding subsidiary sites, press releases, company pages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string"},
                "max_results": {"type": "integer", "default": 10}
            },
            "required": ["query"]
        }
    },
]

# ── tool implementations ──────────────────────────────────────────────────────
_ENV_PREAMBLE = (
    "set -a; "
    "for _f in /home/kali/recon_agent/.env /home/kali/.env; do "
    "[ -f \"$_f\" ] && source \"$_f\"; done; "
    "set +a; "
    "export PATH=\"$PATH:/home/kali/go/bin:/usr/local/go/bin\"; "
)

import re as _re

# Matches a URL with no path or only "/" — bare domain probe, not an API call.
# e.g. https://cpcl.in   https://iocl.com/   BUT NOT https://crt.sh/?q=%.iocl.com
_BARE_URL_RE = _re.compile(r'https?://[^/\s]+/?$', _re.I)

def _is_single_domain_cmd(cmd: str) -> bool:
    """Return True if command is a bare single-domain check (should be batched)."""
    lines = [l.strip() for l in cmd.splitlines()
             if l.strip() and not l.strip().startswith('#')]
    lines = [l for l in lines if not l.startswith(('source ', 'export ', 'set '))]
    if len(lines) != 1:
        return False  # multi-line = loop/script
    line = lines[0]
    # whois <domain> — always single
    if _re.match(r'^whois\s+\S+\s*$', line, _re.I):
        return True
    # curl — only block if it hits a bare domain URL (no path/query = not an API call)
    if _re.match(r'^curl\b', line, _re.I) and '|' not in line and 'for ' not in line:
        urls = _re.findall(r'https?://\S+', line, _re.I)
        if len(urls) == 1 and _BARE_URL_RE.match(urls[0]):
            return True
    # wget — same logic
    if _re.match(r'^wget\b', line, _re.I) and '|' not in line and 'for ' not in line:
        urls = _re.findall(r'https?://\S+', line, _re.I)
        if len(urls) == 1 and _BARE_URL_RE.match(urls[0]):
            return True
    # dig / nslookup / host — single domain lookup
    if _re.match(r'^(?:dig|nslookup|host)\b', line, _re.I) and 'for ' not in line:
        if _re.search(r'\s\S+\.\S+\s*$', line):
            return True
    return False

def _tool_bash(args: dict) -> str:
    cmd = args.get("command", "").strip()
    if not cmd:
        return "[SKIPPED] Empty command"
    if _is_single_domain_cmd(cmd):
        return (
            "[BATCH VIOLATION] Single-domain command rejected.\n"
            "You must write a Python script that checks ALL pending domains in one loop.\n"
            "Steps:\n"
            "  1. write_file → /home/kali/recon_agent/results/recon_<domain>/scripts/verify_batch.py\n"
            "     (loop over all candidates: DNS + WHOIS + HTTP in one script)\n"
            "  2. bash → python3 /home/kali/recon_agent/results/recon_<domain>/scripts/verify_batch.py\n"
            "Never run whois/curl/dig on a single domain — always batch."
        )
    timeout = min(int(args.get("timeout", 120)), 600)
    try:
        r = subprocess.run(
            _ENV_PREAMBLE + cmd,
            shell=True, capture_output=True, text=True,
            timeout=timeout, executable="/bin/bash"
        )
        out = r.stdout
        if r.returncode != 0 and r.stderr:
            out += "\n[stderr] " + r.stderr[:3000]
        out = out.strip() or f"[Exit {r.returncode}] No output"
        return out[:15000] if len(out) > 15000 else out
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT after {timeout}s] Try splitting or use a larger timeout."
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"

def _tool_web_fetch(args: dict) -> str:
    url = args.get("url", "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return f"[ERROR] Invalid URL: {url!r}"
    headers = args.get("headers") or {}
    cmd = ["curl", "-skL", "--max-time", "30",
           "-A", "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36",
           "--retry", "2", "--retry-delay", "1"]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
        out = r.stdout
        return out[:15000] if len(out) > 15000 else (out or f"[EMPTY RESPONSE] {url}")
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT] {url}"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"

def _tool_read_file(args: dict) -> str:
    p = Path(args.get("path", "").strip())
    if not p.exists():
        return f"[NOT FOUND] {p}"
    offset = max(0, int(args.get("offset", 0)))
    limit  = max(1, int(args.get("limit", 2000)))
    try:
        lines = p.read_text(errors="replace").splitlines()
        chunk = lines[offset: offset + limit]
        result = "\n".join(chunk)
        rem = len(lines) - offset - limit
        if rem > 0:
            result += f"\n\n... ({rem} more lines, use offset={offset+limit})"
        return result or "[EMPTY FILE]"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"

def _tool_write_file(args: dict) -> str:
    p = Path(args.get("path", "").strip()).resolve()
    allowed = RESULTS_DIR.resolve()
    if not str(p).startswith(str(allowed) + "/"):
        return f"[SECURITY] Write rejected — path must be under {allowed}\nGot: {p}"
    content = args.get("content", "")
    append  = bool(args.get("append", False))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a" if append else "w", encoding="utf-8") as f:
            f.write(content)
        return f"{'Appended' if append else 'Written'} {len(content):,} chars to {p}"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"

def _tool_web_search(args: dict) -> str:
    query       = args.get("query", "").strip()
    max_results = int(args.get("max_results", 10))
    if not query:
        return "[ERROR] query is required"
    try:
        from ddgs import DDGS
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "ddgs"],
                       capture_output=True)
        try:
            from ddgs import DDGS
        except Exception as e:
            return f"[ERROR] Could not install ddgs: {e}"
    import time as _time
    for _attempt in range(3):
        try:
            with DDGS() as ddgs:
                hits = list(ddgs.text(query, max_results=max_results))
            if not hits:
                return "[NO RESULTS]"
            lines = []
            for h in hits:
                lines.append(f"TITLE: {h.get('title','')}")
                lines.append(f"URL:   {h.get('href','')}")
                lines.append(f"BODY:  {h.get('body','')[:300]}")
                lines.append("")
            return "\n".join(lines)
        except Exception as e:
            msg = str(e).lower()
            if "ratelimit" in msg or "429" in msg or "202" in msg:
                if _attempt < 2:
                    _time.sleep(15 * (_attempt + 1))
                    continue
            return f"[ERROR] {type(e).__name__}: {e}"
    return "[ERROR] web_search: rate-limited after 3 attempts"

DISPATCH = {
    "bash":       _tool_bash,
    "web_fetch":  _tool_web_fetch,
    "read_file":  _tool_read_file,
    "write_file": _tool_write_file,
    "web_search": _tool_web_search,
}

# ── session ────────────────────────────────────────────────────────────────────
def _save(sid, messages, meta):
    try:
        cost = _TOK_IN * _COST_IN + _TOK_OUT * _COST_OUT
        meta["tok_in"]  = _TOK_IN
        meta["tok_out"] = _TOK_OUT
        meta["cost_usd"] = round(cost, 4)
        (SESSION_DIR / f"{sid}.json").write_text(
            json.dumps({"meta": meta, "messages": messages}, indent=2))
    except Exception:
        pass

def _clean_messages(messages: list) -> list:
    """Remove dangling tool_use at the end (no matching tool_result) and
    orphaned tool_result at the start — both cause API 400 errors."""
    if not messages:
        return messages
    # Drop trailing assistant message if it ends with tool_use blocks
    # (session saved mid-turn before tool results came back)
    last = messages[-1]
    if last["role"] == "assistant":
        content = last.get("content", [])
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in content
        ):
            messages = messages[:-1]
    # Drop leading tool_result user messages (orphaned from a cut)
    while messages and _is_tool_result(messages[0]):
        messages = messages[1:]
    return messages

def _load(sid) -> tuple[list, dict]:
    p = SESSION_DIR / f"{sid}.json"
    if not p.exists():
        return [], {}
    try:
        d = json.loads(p.read_text())
        msgs = d.get("messages", [])
        return _clean_messages(msgs), d.get("meta", {})
    except Exception:
        return [], {}

def _sessions() -> list[dict]:
    out = []
    for p in sorted(SESSION_DIR.glob("*.json"), reverse=True)[:10]:
        try:
            d = json.loads(p.read_text())
            out.append({"id": p.stem, **d.get("meta", {})})
        except Exception:
            pass
    return out

# ── context compaction ────────────────────────────────────────────────────────
_COMPACT_AT   = 600_000   # ~150k actual tokens — compact only near the 200k limit
_COMPACT_KEEP = 20        # keep 40 messages after compaction

# ── tool result trimming ──────────────────────────────────────────────────────
_RESULT_KEEP_FULL  = 4     # keep last N tool-result messages at full size
_RESULT_MAX_CHARS  = 2000  # older results trimmed to this many chars

def _trim_old_results(messages: list) -> list:
    """Trim large tool results in old messages — agent already processed them,
    no need to re-send 50k bash output on every subsequent turn."""
    if len(messages) <= _RESULT_KEEP_FULL * 2:
        return messages
    cutoff = len(messages) - (_RESULT_KEEP_FULL * 2)
    for msg in messages[:cutoff]:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            result = block.get("content", "")
            if isinstance(result, str) and len(result) > _RESULT_MAX_CHARS:
                kept = result[:_RESULT_MAX_CHARS]
                dropped = len(result) - _RESULT_MAX_CHARS
                block["content"] = kept + f"\n[…{dropped:,} chars trimmed from history]"
    return messages

# ── prompt caching — system message wrapped for Anthropic cache_control ───────
_SYSTEM_CACHED = [{"type": "text", "text": SYSTEM,
                   "cache_control": {"type": "ephemeral"}}]

def _est(messages): return sum(len(str(m)) for m in messages) // 4

def _is_tool_result(msg: dict) -> bool:
    """True if this user message is a tool_result block (not plain text)."""
    c = msg.get("content", "")
    if isinstance(c, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
    return False

def _find_clean_cut(messages: list, target: int) -> int:
    """Find the nearest index >= target where we can safely cut.
    A clean cut is a user message that is plain text (not a tool_result),
    so the kept slice starts with a complete turn, not an orphaned result.
    """
    for i in range(target, len(messages)):
        m = messages[i]
        if m["role"] == "user" and not _is_tool_result(m):
            return i
    # fallback: keep everything from target even if not clean
    return target

async def _compact(messages: list, client: anthropic.AsyncAnthropic) -> list:
    if len(messages) <= _COMPACT_KEEP * 2:
        return messages

    # Find a clean cut point — never split a tool_use / tool_result pair
    raw_cut = max(0, len(messages) - (_COMPACT_KEEP * 2))
    cut = _find_clean_cut(messages, raw_cut)

    # If no clean cut found, just trim to last COMPACT_KEEP*2 plain messages
    if cut >= len(messages) - 2:
        return messages[-(  _COMPACT_KEEP * 2):]

    old  = messages[:cut]
    keep = messages[cut:]

    text = "\n\n".join(f"[{m['role']}]: {str(m['content'])[:600]}" for m in old)
    try:
        resp = await client.messages.create(
            model=MODEL, max_tokens=2048,
            messages=[{"role": "user", "content":
                "Summarise this recon session. List every domain found, every step completed, "
                "every registrant email/company discovered. Be thorough — this replaces the full history.\n\n"
                + text}])
        summary = resp.content[0].text
    except Exception:
        return keep  # on failure just drop old messages, keep is already clean

    return ([{"role": "user",      "content": f"[PRIOR SESSION SUMMARY]\n{summary}"},
             {"role": "assistant", "content": "Understood. Continuing from where I left off."}]
            + keep)

# ── checklist auto-updater ────────────────────────────────────────────────────
import glob as _glob

def _auto_checklist() -> int:
    """Scan engagement dirs, mark completed steps based on pool files. Returns # items marked."""
    marked = 0
    for cl_path in _glob.glob('/home/kali/recon_agent/results/recon_*/checklist.txt'):
        eng = os.path.dirname(cl_path)
        pool = set(os.listdir(os.path.join(eng, 'pool'))) if os.path.isdir(os.path.join(eng, 'pool')) else set()
        scr  = set(os.listdir(os.path.join(eng, 'scripts'))) if os.path.isdir(os.path.join(eng, 'scripts')) else set()
        all_f = pool | scr
        done = {
            'crt.sh':                   any('crt' in f for f in all_f),
            'Annual report':            any('annual' in f.lower() or 'report' in f.lower() for f in all_f),
            'Wikipedia':                any('wiki' in f for f in pool),
            'Brand variant':            any('brand' in f or 'variant' in f for f in all_f),
            'WhoXY WHOIS on PRIMARY':   any('whoxy' in f or 'step03' in f or 'step25' in f for f in pool),
            'Stake register':           os.path.exists(os.path.join(eng, 'stake_register.txt')),
            'SecurityTrails':           any('securitytrails' in f or 'step19' in f for f in all_f),
            'Shodan':                   any('shodan' in f for f in all_f),
            'DNS profile':              any('dns' in f for f in pool),
            'WHOIS on ALL':             any('whois' in f for f in pool),
            'HTTP content':             any('http' in f or 'step06' in f for f in pool),
            'IntelX':                   any('intelx' in f or 'intel' in f for f in all_f),
            'BuiltWith':                any('builtwith' in f or 'bw_' in f for f in all_f),
            'VirusTotal':               any('virustotal' in f or 'vt_' in f for f in all_f),
            'crt.sh wildcard on EACH':  any('subsidiary' in f for f in all_f),
            'Indian ccTLD sweep':       any('ccTLD' in f or 'cctld' in f or 'indian_tld' in f for f in all_f),
        }
        try:
            with open(cl_path) as _f:
                txt = _f.read()
            orig = txt
            for kw, is_done in done.items():
                if is_done:
                    txt = _re.sub(
                        rf'\[ \] ([^\n]*{_re.escape(kw)}[^\n]*)',
                        r'[DONE] \1', txt, flags=_re.IGNORECASE)
            if txt != orig:
                open(cl_path, 'w').write(txt)
                marked += txt.count('[DONE]') - orig.count('[DONE]')
        except Exception:
            pass
    return marked

def _count_domains() -> int:
    """Count unique confirmed domains across all active engagement pool files."""
    seen: set[str] = set()
    NOISE = {'archive.org','wikipedia.org','wikimedia.org','bseindia.com','nseindia.com',
             'sitemaps.org','wikidata.org','wikimediafoundation.org'}
    for pool_dir in _glob.glob('/home/kali/recon_agent/results/recon_*/pool'):
        for fname in os.listdir(pool_dir):
            if fname.endswith('.json'): continue
            try:
                with open(os.path.join(pool_dir, fname)) as _f:
                    for line in _f:
                        d = line.strip()
                        if '.' in d and ' ' not in d and d not in NOISE:
                            seen.add(d)
            except Exception:
                pass
    return len(seen)

_CONFIRMED_KW = _re.compile(r'(CONFIRMED|INCLUDED|OWNED|✓|★)', _re.I)

def _highlight_result(text: str, max_lines: int = 60) -> str:
    """Return Rich-markup-annotated preview of tool result."""
    if not text:
        return "[dim]  (no output)[/dim]"
    lines = text.splitlines()
    out = []
    for line in lines[:max_lines]:
        stripped = line.rstrip()
        if not stripped:
            continue
        escaped = stripped.replace('[', '\\[')
        if _CONFIRMED_KW.search(stripped):
            out.append(f"  [bold green]  {escaped}[/bold green]")
        elif stripped.startswith(('  +', '★', '✓', 'FOUND', 'CONFIRMED')):
            out.append(f"  [green]{escaped}[/green]")
        elif stripped.startswith(('  -', 'EXCLUDED', 'REJECTED', '[BATCH VIOLATION]')):
            out.append(f"  [red]{escaped}[/red]")
        elif stripped.startswith(('ERROR', 'TIMEOUT', 'WARN', '!')):
            out.append(f"  [yellow]{escaped}[/yellow]")
        elif stripped.startswith('#'):
            out.append(f"  [dim]{escaped}[/dim]")
        else:
            out.append(f"  [dim white]{escaped}[/dim white]")
    result = "\n".join(out)
    if len(lines) > max_lines:
        result += f"\n  [dim]  … {len(lines)-max_lines} more lines[/dim]"
    return result

def _check_api_keys() -> list:
    """Returns list of (key_name, icon, rich_detail) for display."""
    KEYS = [
        ("ANTHROPIC_API_KEY",      "Required"),
        ("WHOXY_API_KEY",          "High — reverse WHOIS"),
        ("BUILTWITH_API_KEY",      "Medium — redirect sweep"),
        ("SHODAN_API_KEY",         "Medium — SSL cert org"),
        ("INTELX_API_KEY",         "Medium — phonebook"),
        ("SECURITYTRAILS_API_KEY", "Medium — associated domains"),
        ("WHOISXMLAPI_KEY",        "Low — backup WHOIS"),
        ("OPENROUTER_API_KEY",     "LLM reasoning"),
    ]
    out = []
    for k, label in KEYS:
        val = os.environ.get(k, "")
        if val:
            out.append((k, "✓", f"[green]SET[/green]  ({label})"))
        else:
            out.append((k, "✗", f"[red]NOT SET[/red]  ({label})"))

    # WhoXY balance check (live API call, 3s timeout)
    whoxy = os.environ.get("WHOXY_API_KEY", "")
    if whoxy:
        try:
            import subprocess as _sp
            r = _sp.run(
                ["curl", "-sk", "--max-time", "5",
                 f"https://api.whoxy.com/?key={whoxy}&account=balance"],
                capture_output=True, text=True)
            data = json.loads(r.stdout)
            live = data.get("live_whois_balance", "?")
            rev  = data.get("reverse_whois_balance", "?")
            hist = data.get("whois_history_balance", "?")
            zero = (live == 0 or rev == 0)
            detail = (
                f"[yellow]⚠ ZERO BALANCE[/yellow]  live={live}  reverse={rev}  history={hist}" if zero
                else f"[green]SET  live={live}  reverse={rev}  history={hist}[/green]"
            )
            icon = "⚠" if zero else "✓"
            out = [(k, ic, d) if k != "WHOXY_API_KEY" else ("WHOXY_API_KEY", icon, detail)
                   for (k, ic, d) in out]
            # log balance snapshot and store start value for per-scan diff
            _log_whoxy_balance(live, rev, hist)
            global _WHOXY_REV_START
            if _WHOXY_REV_START is None and isinstance(rev, int):
                _WHOXY_REV_START = rev
        except Exception:
            pass
    return out


def _log_whoxy_balance(live, reverse, history):
    """Append a timestamped balance snapshot to whoxy_balance_log.jsonl."""
    import datetime as _dt
    log_path = RECON_ROOT / "whoxy_balance_log.jsonl"
    entry = {
        "ts":      _dt.datetime.utcnow().isoformat() + "Z",
        "live":    live,
        "reverse": reverse,
        "history": history,
    }
    try:
        with log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _current_step() -> str:
    """Return 'done/total · current step name' from the active checklist."""
    for cl_path in _glob.glob('/home/kali/recon_agent/results/recon_*/checklist.txt'):
        try:
            with open(cl_path) as _f:
                lines = _f.readlines()
            total   = sum(1 for l in lines if '[ ]' in l or '[DONE]' in l)
            done    = sum(1 for l in lines if '[DONE]' in l)
            pending = next((l.strip() for l in lines if l.strip().startswith('[ ]')), "")
            step    = pending.replace('[ ]', '').strip()[:45] if pending else "complete"
            return f"{done}/{total} · {step}"
        except Exception:
            pass
    return ""


# ── Textual TUI ───────────────────────────────────────────────────────────────
CSS = """
Screen {
    background: #0a0a0a;
    color: #00ff41;
    layout: vertical;
}

#stats {
    height: 1;
    background: #001400;
    color: #00cc33;
    padding: 0 2;
    border-bottom: tall #003300;
}

#main-area {
    height: 1fr;
    layout: horizontal;
}

#log {
    width: 70%;
    height: 100%;
    background: #050505;
    color: #00ff41;
    padding: 0 1;
    scrollbar-color: #003300 #050505;
    border: none;
}

#domain-panel {
    width: 30%;
    height: 100%;
    background: #020602;
    color: #00cc33;
    border-left: tall #003300;
    padding: 0 1;
    scrollbar-color: #003300 #020602;
}

#status {
    height: 1;
    background: #001400;
    color: #00aa22;
    padding: 0 2;
}

#prompt-input {
    height: 3;
    background: #030303;
    border: tall #003300;
    color: #00ff41;
    padding: 0 2;
}

#prompt-input:focus {
    border: tall #00ff41;
    color: #00ff41;
}

Header {
    display: none;
}

Footer {
    display: none;
}
"""

class ReconApp(App):
    """Recon Agent TUI"""

    CSS = CSS
    TITLE = "recon"

    status: reactive[str] = reactive("idle")
    session_id: reactive[str] = reactive("")

    def __init__(self, initial_prompt: str = "", resume_sid: str = ""):
        super().__init__()
        self._initial_prompt = initial_prompt
        self._resume_sid     = resume_sid
        self._messages: list = []
        self._meta: dict     = {}
        self._agent_running  = False
        self._queue: list[str] = []
        self._client         = anthropic.AsyncAnthropic(api_key=KEY)
        self._turn           = 0
        self._domains_found  = 0

    def compose(self) -> ComposeResult:
        yield Label("", id="stats")
        with Horizontal(id="main-area"):
            yield RichLog(id="log", highlight=True, markup=True, wrap=True)
            yield RichLog(id="domain-panel", highlight=True, markup=True, wrap=False)
        yield Label("", id="status")
        yield Input(placeholder='▶  target domain or /help', id="prompt-input")

    def on_mount(self) -> None:
        log = self.query_one("#log", RichLog)
        log.write(
            "[bold green]╔══════════════════════════════════════════════════════════════╗[/bold green]\n"
            "[bold green]║[/bold green]  [bold white]██████╗ ███████╗ ██████╗ ██████╗ ███╗   ██╗[/bold white]               [bold green]║[/bold green]\n"
            "[bold green]║[/bold green]  [bold white]██╔══██╗██╔════╝██╔════╝██╔═══██╗████╗  ██║[/bold white]               [bold green]║[/bold green]\n"
            "[bold green]║[/bold green]  [bold white]██████╔╝█████╗  ██║     ██║   ██║██╔██╗ ██║[/bold white]               [bold green]║[/bold green]\n"
            "[bold green]║[/bold green]  [bold white]██╔══██╗██╔══╝  ██║     ██║   ██║██║╚██╗██║[/bold white]               [bold green]║[/bold green]\n"
            "[bold green]║[/bold green]  [bold white]██║  ██║███████╗╚██████╗╚██████╔╝██║ ╚████║[/bold white]               [bold green]║[/bold green]\n"
            "[bold green]║[/bold green]  [bold white]╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝[/bold white]               [bold green]║[/bold green]\n"
            "[bold green]╚══════════════════════════════════════════════════════════════╝[/bold green]\n"
            f"[dim green]  model: {MODEL}   session: {SESSION_DIR}[/dim green]\n"
            "[dim green]  /help  /sessions  /resume  /clear  /cost  /quit[/dim green]\n"
            "[green]──────────────────────────────────────────────────────────────[/green]\n"
        )
        # ── API key health check ──────────────────────────────────────────
        self.call_after_refresh(self._show_key_check)
        self._update_status("idle")

        # load session
        if self._resume_sid:
            self._messages, self._meta = _load(self._resume_sid)
            if self._messages:
                log.write(f"[dim]Resumed session {self._resume_sid}  ({len(self._messages)} messages)[/dim]\n")
                self.session_id = self._resume_sid
            else:
                log.write(f"[yellow]Session {self._resume_sid!r} not found — starting fresh[/yellow]\n")
        else:
            sessions = _sessions()
            if sessions:
                last = sessions[0]
                try:
                    age_s = (datetime.datetime.now() -
                             datetime.datetime.strptime(last["id"], "%Y%m%d_%H%M%S")).total_seconds()
                except Exception:
                    age_s = 9999
                if age_s < 7200:  # < 2 hours
                    self._messages, self._meta = _load(last["id"])
                    if self._messages:
                        self.session_id = last["id"]
                        log.write(
                            f"[dim]Auto-resumed session {last['id']}  "
                            f"({len(self._messages)} msgs)  · /clear to start fresh[/dim]\n"
                        )

        if self._initial_prompt:
            self.run_agent(self._initial_prompt)

        self.query_one("#prompt-input").focus()

    # ── input handling ─────────────────────────────────────────────────────────
    def on_input_submitted(self, event) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return

        log = self.query_one("#log", RichLog)

        # built-in commands
        if text.lower() in ("/quit", "/exit", "quit", "exit"):
            self.exit()
            return

        if text.lower() in ("/help", "help"):
            log.write(
                "\n[bold]Commands[/bold]\n"
                "[dim]────────────────────────────────────────────[/dim]\n"
                "  [cyan]/sessions[/cyan]        List saved sessions\n"
                "  [cyan]/resume[/cyan]          Resume most recent session\n"
                "  [cyan]/resume <id>[/cyan]     Resume specific session\n"
                "  [cyan]/clear[/cyan]           Start fresh session\n"
                "  [cyan]/model <name>[/cyan]    Switch model\n"
                "  [cyan]/cost[/cyan]            Show token usage\n"
                "  [cyan]/help[/cyan]            Show this help\n"
                "  [cyan]/quit[/cyan]            Exit\n\n"
                "  Anything else → sent to the agent as a prompt\n"
                "  [dim]You can type while the scan is running — message is queued[/dim]\n"
            )
            return

        if text.lower() in ("/sessions", "/history"):
            sessions = _sessions()
            if not sessions:
                log.write("[yellow]No saved sessions.[/yellow]\n")
            else:
                log.write("\n[bold]Saved sessions[/bold]\n[dim]────────────────[/dim]\n")
                for s in sessions:
                    log.write(f"  [cyan]{s['id']}[/cyan]  {s.get('target','—')}\n")
                log.write("\n")
            return

        if text.lower() in ("/cost", "/usage"):
            log.write(f"\n[dim]{_cost()}[/dim]\n\n")
            return

        if text.lower() == "/clear":
            self._messages, self._meta = [], {}
            self.session_id = ""
            log.write("[green]Fresh session started.[/green]\n")
            return

        if text.lower() == "/resume":
            sessions = _sessions()
            if not sessions:
                log.write("[yellow]No saved sessions.[/yellow]\n")
                return
            log.write("\n[bold]Saved sessions[/bold]\n[dim green]────────────────────────────────────────────[/dim green]\n")
            for i, s in enumerate(sessions[:8], 1):
                try:
                    age_s = (datetime.datetime.now() -
                             datetime.datetime.strptime(s["id"], "%Y%m%d_%H%M%S")).total_seconds()
                    age = f"{int(age_s//3600)}h {int((age_s%3600)//60)}m ago"
                except Exception:
                    age = ""
                log.write(f"  [cyan]{i}[/cyan]  {s['id']}  [dim]{s.get('target','—')}  {age}[/dim]\n")
            log.write(
                "[dim green]────────────────────────────────────────────[/dim green]\n"
                "[dim]Type [bold]/resume <session-id>[/bold] or [bold]/resume <number>[/bold] to load[/dim]\n\n"
            )
            # Auto-resume most recent if < 2h old
            try:
                age_s = (datetime.datetime.now() -
                         datetime.datetime.strptime(sessions[0]["id"], "%Y%m%d_%H%M%S")).total_seconds()
                if age_s < 7200:
                    self._messages, self._meta = _load(sessions[0]["id"])
                    self.session_id = sessions[0]["id"]
                    log.write(f"[green]✓ Auto-resumed most recent: {sessions[0]['id']}  ({len(self._messages)} msgs)[/green]\n\n")
            except Exception:
                pass
            return

        if text.lower().startswith("/resume "):
            arg = text.split(None, 1)[1].strip()
            # support numeric shortcut from /resume list
            if arg.isdigit():
                sessions = _sessions()
                idx = int(arg) - 1
                if 0 <= idx < len(sessions):
                    arg = sessions[idx]["id"]
                else:
                    log.write(f"[red]No session #{arg}[/red]\n")
                    return
            self._messages, self._meta = _load(arg)
            if self._messages:
                self.session_id = arg
                log.write(f"[green]✓ Resumed {arg}  ({len(self._messages)} messages)[/green]\n")
            else:
                log.write(f"[red]Session {arg!r} not found.[/red]\n")
            return

        if text.lower().startswith("/model "):
            global MODEL, _COST_IN, _COST_OUT
            MODEL = text.split(None, 1)[1].strip()
            if "opus" in MODEL:
                _COST_IN, _COST_OUT = 15.0/1e6, 75.0/1e6
            elif "haiku" in MODEL:
                _COST_IN, _COST_OUT = 0.8/1e6, 4.0/1e6
            else:
                _COST_IN, _COST_OUT = 3.0/1e6, 15.0/1e6
            log.write(f"[green]Model → {MODEL}[/green]\n")
            return

        # recon prompt or mid-scan message
        if self._agent_running:
            self._queue.append(text)
            log.write(
                f"\n[dim]────────────────────────────────────────────────────────────[/dim]\n"
                f"[bold green]  You (queued):[/bold green] {text}\n"
                f"[dim]  (will be sent to agent at the next turn boundary)[/dim]\n"
            )
        else:
            self.run_agent(text)

    # ── agent worker ───────────────────────────────────────────────────────────
    @work(exclusive=False, thread=False)
    async def run_agent(self, prompt: str) -> None:
        global MODEL
        log   = self.query_one("#log", RichLog)
        input_widget = self.query_one("#prompt-input")

        self._agent_running = True
        input_widget.placeholder = "Agent is running... (type to queue a message)"
        self._update_status("thinking")

        sid = self._meta.setdefault("sid", datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        if not self.session_id:
            self.session_id = sid

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        log.write(
            f"\n[dim]────────────────────────────────────────────────────────────[/dim]\n"
            f"  [dim]{ts}[/dim]  [bold]{prompt}[/bold]\n"
            f"[dim]────────────────────────────────────────────────────────────[/dim]\n"
        )

        self._messages.append({"role": "user", "content": prompt})

        for turn in range(300):
            self._turn += 1
            self._update_status("thinking")
            if turn == 280:
                log = self.query_one("#log", RichLog)
                log.write(
                    "\n[bold yellow]⚠  Turn 280/300 — approaching limit. "
                    "Wrap up: write final report now if all phases are done.[/bold yellow]\n"
                )
            # inject queued user message between turns
            if self._queue:
                queued = self._queue.pop(0)
                self._messages.append({"role": "user", "content": queued})
                ts2 = datetime.datetime.now().strftime("%H:%M:%S")
                log.write(
                    f"\n[dim]────────[/dim] [dim]{ts2}[/dim] "
                    f"[bold green](you)[/bold green] {queued}\n"
                )

            # trim old tool results to keep history lean
            self._messages = _trim_old_results(self._messages)

            # compact if needed (only near the 200k token limit)
            if _est(self._messages) > _COMPACT_AT:
                log.write(f"[dim]  · Compacting context (~{_est(self._messages):,} tokens)...[/dim]\n")
                self._messages = await _compact(self._messages, self._client)
                log.write("[dim]  · Done.[/dim]\n")

            # ── API call ───────────────────────────────────────────────────────
            final_msg = None
            try:
                async with self._stream_with_retry() as stream:
                    first = True
                    async for text in stream.text_stream:
                        if first:
                            log.write("\n")
                            first = False
                        log.write(text)
                    final_msg = await stream.get_final_message()
            except anthropic.AuthenticationError:
                log.write("[red]✗  Authentication failed — check ANTHROPIC_API_KEY[/red]\n")
                break
            except anthropic.RateLimitError:
                log.write("[yellow]!  Rate limited — waiting 60s...[/yellow]\n")
                await asyncio.sleep(60)
                continue
            except anthropic.APIConnectionError:
                log.write("[red]✗  Network error — check connection[/red]\n")
                break
            except anthropic.APIStatusError as e:
                log.write(f"[red]✗  API error {e.status_code}[/red]\n")
                break
            except Exception as e:
                log.write(f"[red]✗  {type(e).__name__}: {e}[/red]\n")
                break

            if not final_msg:
                break

            _add_tok(final_msg.usage.input_tokens, final_msg.usage.output_tokens)
            self._update_status("thinking")

            # build assistant message — only text + tool_use blocks
            content_dicts = []
            for block in final_msg.content:
                if block.type == "text":
                    content_dicts.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    content_dicts.append({
                        "type": "tool_use", "id": block.id,
                        "name": block.name, "input": block.input,
                    })
            self._messages.append({"role": "assistant", "content": content_dicts})

            if final_msg.stop_reason == "end_turn":
                _save(sid, self._messages, self._meta)
                log = self.query_one("#log", RichLog)
                log.write(f"\n[bold green]── Scan complete ──[/bold green]")
                log.write(f"[dim]{_cost()}[/dim]")
                whoxy_str = _whoxy_usage_str()
                if whoxy_str:
                    log.write(f"[dim]{whoxy_str}[/dim]")
                break

            if final_msg.stop_reason == "tool_use":
                tool_blocks = [b for b in final_msg.content if b.type == "tool_use"]
                if len(tool_blocks) > 1:
                    log.write(f"[dim cyan]  ▸ {len(tool_blocks)} parallel tool calls[/dim cyan]\n")

                # log tool headers before running (show what's launching)
                for block in tool_blocks:
                    name  = block.name
                    iargs = block.input or {}
                    ts_tool = datetime.datetime.now().strftime("%H:%M:%S")
                    if name == "bash":
                        cmd = iargs.get("command", "").strip()
                        lines = [l.strip() for l in cmd.splitlines() if l.strip()]
                        desc = next((l[2:].strip() for l in lines if l.startswith('#')), "")
                        code = next((l for l in lines if not l.startswith('#') and not l.startswith('source') and not l.startswith('export')), "")
                        log.write(f"\n[dim green]┌─ bash[/dim green] [dim]{ts_tool}[/dim]\n")
                        if desc:
                            log.write(f"[green]│ [bold white]{desc}[/bold white][/green]\n")
                        if code:
                            log.write(f"[dim green]│[/dim green] [dim cyan]{code[:120].replace('[','\\[')}[/dim cyan]\n")
                        log.write("[dim green]└" + "─"*60 + "[/dim green]\n")
                    elif name == "write_file":
                        path = iargs.get("path", "")
                        log.write(f"\n[dim green]┌─ write_file[/dim green] [dim]{ts_tool}[/dim]\n")
                        log.write(f"[green]│ [cyan]{path}[/cyan][/green]\n")
                        log.write("[dim green]└" + "─"*60 + "[/dim green]\n")
                    elif name == "read_file":
                        log.write(f"[dim green]  ▸ read  {iargs.get('path','')}[/dim green]\n")
                    else:
                        log.write(f"[dim green]  ▸ {name}  {str(iargs)[:80]}[/dim green]\n")
                self._update_status(f"● {len(tool_blocks)}×tool")

                # execute all tools concurrently
                async def _exec_one(block):
                    name  = block.name
                    iargs = block.input or {}
                    handler = DISPATCH.get(name)
                    if handler is None:
                        return block.id, name, f"[ERROR] Unknown tool: {name!r}"
                    try:
                        result = await asyncio.get_event_loop().run_in_executor(
                            None, handler, iargs)
                    except Exception as e:
                        result = f"[TOOL CRASH] {type(e).__name__}: {e}"
                    return block.id, name, result

                exec_results = await asyncio.gather(*[_exec_one(b) for b in tool_blocks])

                tool_results = []
                for (tool_use_id, name, result) in exec_results:
                    if result and name in ("bash", "web_fetch", "web_search"):
                        log.write(_highlight_result(result) + "\n")
                    if name in ("bash", "write_file"):
                        _auto_checklist()
                        self._domains_found = _count_domains()
                        self._refresh_domain_panel()
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result,
                    })

                self._messages.append({"role": "user", "content": tool_results})
                _save(sid, self._messages, self._meta)
                self._update_status("thinking")

        # end of run — show session ID prominently
        _save(sid, self._messages, self._meta)
        log.write(
            f"\n[bold green]╔══ SCAN PAUSED / COMPLETE ════════════════════════════════╗[/bold green]\n"
            f"[bold green]║[/bold green]  Session: [bold cyan]{sid}[/bold cyan]                  [bold green]║[/bold green]\n"
            f"[bold green]║[/bold green]  Resume:  [dim]/resume {sid}[/dim]    [bold green]║[/bold green]\n"
            f"[bold green]║[/bold green]  {_cost():<58}[bold green]║[/bold green]\n"
            f"[bold green]╚══════════════════════════════════════════════════════════╝[/bold green]\n\n"
        )

        self._agent_running = False
        self._update_status("idle")
        input_widget.placeholder = f"Session: {sid}  ·  /resume to continue  ·  /help for commands"

        # drain any queued messages
        if self._queue:
            next_msg = self._queue.pop(0)
            self.run_agent(next_msg)

    def _show_key_check(self) -> None:
        log = self.query_one("#log", RichLog)
        log.write("\n[bold green]  API KEY STATUS[/bold green]\n")
        log.write("[dim green]  " + "─"*50 + "[/dim green]\n")
        checks = _check_api_keys()
        for key, icon, detail in checks:
            color = "green" if icon == "✓" else ("yellow" if icon == "⚠" else "red")
            log.write(f"  [{color}]{icon}[/{color}]  {key:<28} {detail}\n")
        log.write("[dim green]  " + "─"*50 + "[/dim green]\n\n")

    def _refresh_domain_panel(self) -> None:
        try:
            panel = self.query_one("#domain-panel", RichLog)
            panel.clear()
            seen: set = set()
            NOISE = {'archive.org','wikipedia.org','wikimedia.org','bseindia.com',
                     'nseindia.com','sitemaps.org','wikidata.org','wikimediafoundation.org'}
            domains = []
            for pool_dir in sorted(_glob.glob('/home/kali/recon_agent/results/recon_*/pool')):
                for fname in sorted(os.listdir(pool_dir)):
                    if fname.endswith('.json'):
                        continue
                    try:
                        with open(os.path.join(pool_dir, fname)) as _f:
                            for line in _f:
                                d = line.strip()
                                if '.' in d and ' ' not in d and d not in NOISE and d not in seen:
                                    seen.add(d)
                                    domains.append(d)
                    except Exception:
                        pass
            panel.write(f"[bold green]  DOMAINS  [{len(domains)}][/bold green]\n")
            panel.write("[dim green]" + "─"*27 + "[/dim green]\n")
            for d in domains:
                panel.write(f"  [green]{d}[/green]\n")
        except Exception:
            pass

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _stream_with_retry(self, max_retries=6):
        """Async context manager that retries the full HTTP connection on transient errors."""
        delay = 3
        for attempt in range(max_retries):
            try:
                async with self._client.messages.stream(
                    model=MODEL, max_tokens=16384,
                    system=_SYSTEM_CACHED, tools=TOOLS,
                    messages=self._messages,
                ) as stream:
                    yield stream
                    return
            except anthropic.RateLimitError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
                else:
                    raise
            except anthropic.APIStatusError as e:
                if e.status_code >= 500 and attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                else:
                    raise
            except (anthropic.APIConnectionError, Exception) as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                else:
                    raise

    def _update_status(self, state: str):
        try:
            label = self.query_one("#status", Label)
            sid   = self.session_id or "no session"
            cost  = _cost()
            icon  = "●" if state not in ("idle",) else "○"
            color = "yellow" if state not in ("idle",) else "green"
            label.update(
                f"[{color}]{icon}[/{color}]  {state:<20}"
                f"[dim]│ session: {sid}  │{cost}[/dim]"
            )
        except Exception:
            pass
        # also refresh stats bar
        try:
            stats = self.query_one("#stats", Label)
            doms = self._domains_found
            t    = self._turn
            cost_str = _cost()
            step_str = _current_step()
            step_part = f"  [dim]│[/dim]  [dim cyan]{step_str}[/dim cyan]" if step_str else ""
            stats.update(
                f"[bold green]firecompass[/bold green] [dim green]recon[/dim green]"
                f"  [dim]│[/dim]  turn [bold white]{t}[/bold white]"
                f"  [dim]│[/dim]  domains [bold cyan]{doms}[/bold cyan]"
                f"{step_part}"
                f"  [dim]│[/dim]  {cost_str}"
            )
        except Exception:
            pass

    @staticmethod
    def _tool_label(name: str, args: dict) -> str:
        if name == "bash":
            cmd = args.get("command", "")
            first = cmd.strip().splitlines()[0][:100] if cmd.strip() else ""
            return first.replace("[", "\\[")
        if name == "web_fetch":
            return args.get("url", "")[:100]
        if name in ("read_file", "write_file"):
            return Path(args.get("path", "")).name
        return ""


# ── entry point ───────────────────────────────────────────────────────────────
def main():
    global MODEL, _COST_IN, _COST_OUT
    import argparse
    ap = argparse.ArgumentParser(description="Recon Agent")
    ap.add_argument("prompt", nargs="*", help="Initial prompt (optional)")
    ap.add_argument("--resume", "-r", action="store_true")
    ap.add_argument("--session", "-s", metavar="ID")
    ap.add_argument("--model", "-m", metavar="MODEL", default=MODEL)
    args = ap.parse_args()

    MODEL = args.model
    if "opus" in MODEL:
        _COST_IN, _COST_OUT = 15.0/1e6, 75.0/1e6
    elif "haiku" in MODEL:
        _COST_IN, _COST_OUT = 0.8/1e6, 4.0/1e6

    resume_sid = ""
    if args.session:
        resume_sid = args.session
    elif args.resume:
        s = _sessions()
        if s:
            resume_sid = s[0]["id"]

    initial = " ".join(args.prompt).strip() if args.prompt else ""
    app = ReconApp(initial_prompt=initial, resume_sid=resume_sid)
    app.run()


if __name__ == "__main__":
    main()
