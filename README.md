# Recon Agent — Documentation

## Overview

Recon Agent is an autonomous OSINT tool for discovering every root domain owned by a target company. Given a primary domain, it runs a structured 6-phase investigation using DNS, SSL certificates, WHOIS registrant data, web crawling, subsidiary databases, and third-party APIs — then produces a final report classifying every candidate as **INCLUDED**, **EXCLUDED**, or **DANGLING**.

The agent is Claude (`claude-sonnet-4-6`) with tool access. It reasons through the investigation, writes and executes its own scripts, and adapts based on what it finds. The methodology file (`methodology.md`) defines exactly what to do at each phase.

---

## Files

```
recon_agent/
├── auto_recon.py       # TUI mode — interactive terminal interface
├── headless_run.py     # Headless mode — logs to file, no UI (for automation)
├── methodology.md      # Agent playbook — 54 steps across 6 phases
├── .env                # API keys
├── requirements.txt    # Python dependencies
├── sessions/           # Saved conversation history (one JSON per scan)
└── results/            # Scan outputs — one subdirectory per engagement
    └── recon_<target>/
        ├── pool/               # Step output files (one .txt per step)
        ├── scripts/            # Python scripts written by the agent
        ├── downloads/          # PDFs, raw API responses
        ├── reports/            # partial_phase*.md + final_report.md
        ├── checklist.txt       # Phase checklist with [DONE] markers
        ├── provenance.jsonl    # One JSON record per domain discovery event
        └── stake_register.txt  # Subsidiary ownership percentages
```

---

## Running a Scan

### TUI mode (interactive)

```bash
# Launch the chat interface, then type your target
python3 auto_recon.py

# Start immediately with a prompt
python3 auto_recon.py "do recon on example.com"

# Resume the last session
python3 auto_recon.py --resume
```

The TUI shows the agent's reasoning, tool calls, and results in a live terminal UI. You can type mid-scan to redirect or ask questions.

### Headless mode (automation)

```bash
python3 headless_run.py "do recon on example.com"
```

Same agent logic, no UI. Every turn is logged to `headless_recon.log` with timestamps, token counts, and cost. Use this for background scans, cron jobs, or SSH sessions.

---

## API Keys (`.env`)

```bash
ANTHROPIC_API_KEY=         # Required — Claude API access
WHOXY_API_KEY=             # High priority — reverse WHOIS (most signal per dollar)
BUILTWITH_API_KEY=         # Medium — inbound redirect sweep
SHODAN_API_KEY=            # Medium — SSL cert org search
INTELX_API_KEY=            # Medium — phonebook domain search
SECURITYTRAILS_API_KEY=    # Medium — associated domains
WHOISXMLAPI_KEY=           # Low — backup WHOIS
RECON_MODEL=               # Optional — override model (default: claude-sonnet-4-6)
```

The agent works without paid API keys but finds significantly fewer domains. `WHOXY_API_KEY` provides the most unique signal.

---

## Architecture

### Two runners, one agent

`headless_run.py` imports everything from `auto_recon.py` — the system prompt, tool definitions, and handlers. There is no duplicated logic.

```
auto_recon.py
├── Agent definition: SYSTEM prompt, TOOLS schema, DISPATCH handlers
├── TUI interface (Textual) — for interactive use
│
headless_run.py
└── Imports agent from auto_recon.py
    └── Log-to-file interface — for automation
```

Any change to the system prompt or tools in `auto_recon.py` is automatically picked up by `headless_run.py`.

### Tools

The agent has five tools:

| Tool | Purpose |
|---|---|
| `bash` | Run shell commands on Kali Linux. API keys are pre-exported. Enforces batching — rejects single-domain operations. |
| `write_file` | Write Python/shell scripts to disk before executing. Required for all multi-domain operations. |
| `read_file` | Read pool files, checklist, methodology, results. |
| `web_fetch` | Fetch a known URL (API endpoints, specific pages, crt.sh). |
| `web_search` | DuckDuckGo search — used for subsidiary lookups, press releases, government filings. |

### Parallel execution

Both runners execute tool calls with `asyncio.gather`. When the agent issues multiple tool calls in one response (e.g. `bash` + `web_search` + `web_fetch` simultaneously), they run concurrently — reducing scan time and API cost significantly.

### Context compaction

When conversation history grows too large, the runner summarises old turns into a compact record (every domain found, every step completed, every registrant email/org discovered) and replaces the history with that summary. This lets scans run indefinitely without hitting context limits.

### Checklist auto-updater

At the start of each turn, the agent scans the engagement's `pool/` and `scripts/` directories and automatically marks checklist items as `[DONE]` based on which step files exist. This prevents re-running completed steps after a resume.

---

## 6-Phase Methodology

The agent follows `methodology.md` as its playbook. Each phase must be fully completed (all checklist items marked `[DONE]`) before the next begins.

### Phase 1 — Seed Collection

Gather the first batch of candidates from sources tied directly to the primary domain.

| Step | Technique | What it finds |
|---|---|---|
| crt.sh | Wildcard `%.domain` + org name query | SSL SANs, all certs issued to the org |
| Homepage crawl | curl + link extraction | Outbound domains linked from the main site |
| Annual report | PDF download → pdftotext + grep | Subsidiary names and stake percentages |
| Wikipedia / Wikidata | SPARQL query (P355 subsidiaries) | Ownership structure, brand names |
| Brand variant DNS | Primary SLD across all TLDs | Defensive registrations, lapsed domains |
| Government search | site:gov.in queries | MCA filings, registered entities |

### Phase 2 — Technical Pivots

Use infrastructure fingerprints to find domains that share ownership signals with the primary.

| Step | Technique | What it finds |
|---|---|---|
| Favicon hash | mmh3 hash → Shodan | Sites sharing the same favicon |
| GA/GTM tracker | Grep source for UA-/GTM-/G- IDs → SpyOnWeb | Domains sharing the same analytics account |
| DNS record pivot | DMARC, SPF, CAA, MTA-STS | Email domains, CDN relationships |
| HTTP header pivot | CSP, CORS, Link headers | Cross-domain trust relationships |
| GAU + Wayback | Historical URL archive → root extraction | Old brand domains, lapsed registrations |
| CRT time-correlation | Certs issued ±30 days from primary | Batch-registered domains by the same org |
| ASN + PTR sweep | BGPView → prefixes → dnsx PTR | Domains hosted on company-owned IP space |

### Phase 3 — Subsidiary Recursion

For every confirmed >50% subsidiary, repeat key discovery steps.

- crt.sh wildcard + org search on each subsidiary domain
- ccTLD sweep: `.co.in`, `.net.in`, `.org.in`, `.firm.in`, `.gen.in`, `.ind.in`
- BuiltWith inbound redirect sweep on every confirmed domain
- Stake % verified for every entity before inclusion

### Phase 4 — Registrant Pivot

Use WHOIS registrant data to find other domains registered by the same person or company.

- WhoXY WHOIS on primary domain → extract registrant email + org
- Reverse WHOIS by registrant email (company emails only; privacy proxies skipped)
- Reverse WHOIS by company name (primary + confirmed subsidiaries)
- WhoXY WHOIS on each confirmed subsidiary → collect additional emails/orgs
- `amass intel` WHOIS and org pivot

> WhoXY reverse WHOIS costs 1 credit per page ($0.01). The agent checks balance before any reverse query and caps at 3 pages per query. If balance < 50, all reverse steps are skipped and logged as a coverage gap.

### Phase 5 — Additional Sources

Supplementary databases that may surface domains missed in earlier phases.

| Source | What it provides |
|---|---|
| Shodan | SSL cert org search; ASN/BGP org attribution |
| SecurityTrails | Associated domains API |
| IntelX | Phonebook search by domain and org name |
| VirusTotal | Related domains |
| URLScan.io | Search by domain, ASN, page title |

### Phase 6 — Verification

Validate every candidate before writing the final report.

- Dedup all pool files into one sorted candidate list
- DNS profile (A + NS + MX) on all candidates
- WHOIS + RDAP on all candidates
- HTTP content check where WHOIS is unclear
- Stake register complete: every INCLUDED domain has a recorded ownership percentage

---

## Ownership Rules

| Rule | Detail |
|---|---|
| Stake threshold | >50% → INCLUDE and recurse. ≤50% (including exactly 50%) → EXCLUDE. |
| Verify before including | DNS resolving alone is not enough. Need at least one of: WHOIS registrant match / SSL cert org match / HTTP branding. |
| Root domains only | Strip subdomains. `api.example.com` → `example.com`. |
| Registrar trap | Some registrars appear as the WHOIS registrant instead of the actual owner. Always verify via HTTP content. |
| Brand variants | A domain containing the company name is not proof of ownership. Always WHOIS it. |
| WHOIS history | Reverse WHOIS returns current and historical registrations. Always verify current WHOIS after a reverse hit. |
| NS pivoting | Only pivot on company-owned nameservers. Never pivot on shared NS providers (Cloudflare, GoDaddy, Route53). |

---

## Output

### Domain classifications

| Class | Meaning |
|---|---|
| **INCLUDED** | Confirmed owned — >50% stake or direct WHOIS/HTTP/SSL match |
| **DANGLING** | Registered and controlled by the target but serving no active content — takeover risk if registration lapses |
| **EXCLUDED** | Not owned, ≤50% stake, squatter, or unrelated company |

A domain with WHOIS returning "No match for domain" has expired and been deleted from the registry — flag as **HIGH TAKEOVER RISK** (immediately re-registerable by anyone).

### Provenance log (`provenance.jsonl`)

Every domain written to the pool has a provenance entry. One JSON record per line:

```json
{
  "domain": "example-subsidiary.com",
  "step": "step04_whoxy",
  "source": "whoxy_reverse_email",
  "via": "admin@primarycompany.com",
  "signal": "whois_email",
  "confidence": "high",
  "ts": "2025-07-14T10:23:45Z"
}
```

### Final report (`reports/final_report.md`)

```
## INCLUDED — Confirmed owned (>50% stake or direct WHOIS match)
| Domain | Company | Stake | Evidence | Discovered via | NS |

## DANGLING — Registered by target but inactive/parked
| Domain | Issue | Risk | Discovered via |

## EXCLUDED — Not owned or ≤50% stake
| Domain | Reason |

## COVERAGE GAPS — sources that returned 0 or failed
| Source | Status | Note |
```

---

## Cost

**Model:** `claude-sonnet-4-6` — $3.00 / 1M input tokens · $15.00 / 1M output tokens

The system prompt is cached (Anthropic prompt caching), so repeat turns within a session pay the cache read rate rather than the full input rate.

**Target turns:** ≤15 turns per full scan with parallel tool execution. The TUI and headless log both display running cost per turn.

**WhoXY:** 1 credit = $0.01 per reverse WHOIS page. The agent logs usage at end of scan:
```
WhoXY: +142 reverse credits used ($1.42)  remaining=9,658
```
