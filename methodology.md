# FireCompass Passive Recon — Technique Reference
# Agent reads this once as a knowledge base. Write your own scripts using helpers.md.
# Format: each step → technique, API/URL, key params, what to extract, what to pivot on.

---

## CONVENTIONS
- Pool files: every step writes `$POOL/stepNN_name.txt` — one root domain per line
- Idempotency: skip if `$POOL/stepNN_name.txt` already exists and non-empty
- Root domains only: `sub.example.com` → `example.com` (use `to_root()` from recon_common)
- Ownership threshold: `score_ownership(signals)` must return OWNED or LIKELY to include
- Log every rejection with reason (rejected domains go to `$POOL/rejected.txt`)
- WhoXY MUST use `curl -sk` — Python urllib gets HTTP 403 from their WAF
- API keys are auto-exported to every bash call — no need to `source .env` manually
- STAKE CHECK: whenever a subsidiary or JV is found, look up ownership stake % before recursing
  Sources: annual report table, BSE/NSE disclosure, MCA filing, Wikipedia infobox, GLEIF
  Classification — STRICT:
    >50% stake  → SUBSIDIARY   — include domain, full 2-level recursion
    ≤50%        → EXCLUDED     — do NOT include (JV, minority, associate — not target-controlled)
  Even 50% exactly = EXCLUDED (neither party has control, e.g. Adani JV at 50%)
  Record stake % in pool: `echo "{domain}|{entity}|{stake}%" >> $ENGAGEMENT_DIR/stake_register.txt`
- RECURSE (2 LEVELS, subsidiaries >50% only):
    Level 1: crt.sh wildcard + WhoXY registrant pivot + BuiltWith inbound on subsidiary domain
    Level 2: repeat crt.sh + WhoXY on each Level-1 domain confirmed as owned
    BuiltWith: run on EVERY confirmed domain (primary + all subsidiaries), not just primary
- VERIFY ALL: every domain in the pool must be verified (DNS + one ownership signal) before the
  final report — never skip verification just because evidence wasn't found in discovery
- IF UNSURE: always fetch the domain and read the page — `curl -skL --max-time 15 https://{domain}`
  Check: page title, copyright footer, logo alt text, "About" link text, contact email domain
  A parked/for-sale page = DANGLING or EXCLUDED. A 404 on a resolving domain = INACTIVE.
  A page clearly showing the target's branding = include even if WHOIS is privacy-protected.
  Do not guess — open it and read it.

## CRITICAL RULES
- Never enumerate subdomains — root domains only
- NS-only domains count: registered even if no A record (DANGLING category)
- BuiltWith inbound domains skip WhoXY — they're already verified redirecting
- Verify every domain before including: DNS + at least one ownership signal
- Do NOT pivot on shared nameservers (Cloudflare, GoDaddy, AWS Route53, telecoms)
  Reverse-NS only if the NS hostname contains the company's own SLD
  If reverse-NS returns >200 results → hosting service → SKIP

## KNOWN REGISTRAR TRAP — Endurance International Group India Pvt Ltd
  BigRock / HostGator India domains show "Endurance International Group India Private Limited"
  as the REGISTRANT in WHOIS — this is the REGISTRAR acting on behalf of a client, NOT the owner.
  If WHOIS registrant = "Endurance International Group" → the actual owner is UNKNOWN → EXCLUDE
  unless HTTP content and page title clearly confirm the target company owns/operates the domain.
  Other Indian registrars to treat the same way: PDR Ltd (LogicBoxes), ResellerClub, Mitsu Technologies,
  Web Commerce Communications Ltd (WebNIC), Dattatec.com.

## BRAND VARIANT RULE — name match alone is NOT ownership proof
  Finding easyfuel.com, indane.com, servo.com, cpcl.org etc. via DNS does not mean the target owns them.
  Popular brand names (Servo, Indane, CPCL, NRL) are also used by completely unrelated companies.
  Brand variant domains MUST pass WHOIS verification (registrant matches target or known subsidiary).
  If WHOIS registrant is unrelated → EXCLUDE even if domain name exactly matches a known brand.

## WHOXY HISTORICAL DATA CAUTION
  WhoXY reverse-WHOIS returns current AND historical registrations. A domain previously registered
  by the target may now be owned by someone else (expired, sold, reused).
  Rule: always verify CURRENT WHOIS after WhoXY returns a result.
  Also: IOCL R&D Centre / IOCL RHQ / regional IOCL entities register domains for external
  conferences, academic projects, and partner orgs — those domains are NOT IOCL properties.
  An individual IOCL employee as registrant does NOT mean IOCL owns the domain.
  Verification required: current WHOIS registrant matches + HTTP content shows target branding.

## SETUP
```bash
export ORG_NAME="..."
export PRIMARY_DOMAIN="..."
export PRIMARY_SLD=$(echo "$PRIMARY_DOMAIN" | sed 's/\.[^.]*$//')
export ENGAGEMENT_DIR="/home/kali/recon_agent/results/recon_${PRIMARY_DOMAIN//./_}"
export POOL="$ENGAGEMENT_DIR/pool"
export SCRIPTS="$ENGAGEMENT_DIR/scripts"
mkdir -p "$POOL" "$SCRIPTS" "$ENGAGEMENT_DIR/downloads" "$ENGAGEMENT_DIR/reports"
echo "$PRIMARY_DOMAIN" > "$POOL/step00_primary.txt"
pip3 install -q requests tldextract mmh3 dnspython 2>/dev/null
apt-get install -y poppler-utils jq whois exiftool amass -qq 2>/dev/null
export PATH="$PATH:$HOME/go/bin"
source /home/kali/recon/.env 2>/dev/null || true
```

---

## PHASE 1 — SEED COLLECTION

### Step 1 — Homepage Scrape
- Fetch: `https://{PRIMARY_DOMAIN}/` and `/about /group /brands /subsidiaries /sitemap.xml /en/ /contact /investors`
- Tool: `curl -skL --max-time 20 -A "Mozilla/5.0..."` or Playwright if JS-heavy
- Extract: all outbound root domains from href/src attributes using `extract_roots_from_html()`
- Filter: drop noise (google.com, facebook.com, cloudflare.com, amazonaws.com, cdn providers)

### Step 2 — BFS Crawl of Confirmed Domains
- Crawl up to 60 pages across all currently confirmed domains
- Seed paths: `/`, `/about`, `/our-brands`, `/group`, `/sitemap.xml`, `/en/`, `/contact`
- Extract: outbound root domains from each page using `extract_roots_from_html()`
- Follow internal links (same domain) up to depth 3

### Step 3 — Annual Report PDF → Subsidiaries + Stake
- Find PDF: check `https://{PRIMARY_DOMAIN}/investor-relations`, `/investors`, `/annual-report`
  Also search DDG: `"{ORG_NAME}" annual report 2024 filetype:pdf`
- Download: `curl -skL --max-time 60 -o annual_report.pdf "{URL}"`
- Extract text: `pdftotext annual_report.pdf annual_report.txt` (or `pip3 install PyPDF2`)
- Parse: grep for "subsidiaries", "acquisitions", "consolidated entities", "wholly owned", "joint venture"
  Extract company names AND their % stake from "Statement of Subsidiaries" tables (usually Schedule/Annexure)
  Annual reports always list: Company Name | Country | % Shareholding — capture all three
- Write to stake_register: `{domain}|{company_name}|{stake}%` for each entry found

### Step 3b — Document Metadata (FOCA approach)
- Search DDG: `site:{PRIMARY_DOMAIN} filetype:pdf` and `"{ORG_NAME}" filetype:pdf`
  Also: `site:{PRIMARY_DOMAIN} filetype:docx filetype:pptx`
- Download up to 10 documents
- Run: `exiftool -Author -Creator -Company -Producer -Subject {file}` on each
- Extract: email domains from Author/Creator fields; URLs from Subject/Description
- Also grep text for internal domain patterns like `{sld}.internal`, `{sld}-corp.com`

### Step 4 — Company Website Brand/Group Pages
- Fetch: `/group/companies`, `/our-brands`, `/portfolio`, `/business-units`, `/subsidiaries`
  `/en/about/our-group`, `/corporate/group-companies`, `/about/subsidiaries`
- Extract: company/brand names listed on these pages
- These names feed into Steps 5, 7, 11 (DDG resolution per subsidiary name)

### Step 5 — Wikipedia + Wikidata
- Wikipedia: `https://en.wikipedia.org/wiki/{ORG_NAME_URL_ENCODED}`
  Parse "Subsidiaries" infobox field and any acquisition tables in body
  Also parse ownership % if listed (Wikipedia often shows "51% stake", "wholly owned" in infobox)
- Wikidata SPARQL (P355 = subsidiary, P749 = parent org, P1451 = motto, P127 = owned by):
  `https://query.wikidata.org/sparql?query=SELECT ?sub ?subLabel ?stake WHERE { wd:{QID} wdt:P355 ?sub. OPTIONAL { wd:{QID} p:P355 ?stmt. ?stmt ps:P355 ?sub. ?stmt pq:P1107 ?stake. } SERVICE wikibase:label{...} }`
  Get QID via: `https://www.wikidata.org/w/api.php?action=wbsearchentities&search={ORG}&language=en&format=json`
- Extract: subsidiary company names + stake % → DDG-resolve each to a domain (Step 11 pattern)
- For JVs without % on Wikipedia: check the subsidiary's own Wikipedia page — infobox "Parent" often shows %

### Step 6 — LinkedIn Company Page
- URL: `https://www.linkedin.com/company/{slug}/subsidiaries/`
- Fetch with curl (may be paywalled — extract what's visible)
- Extract: subsidiary company names listed in org chart

### Step 7 — OpenCorporates / GLEIF / SEC EDGAR / Crunchbase
- GLEIF fuzzy search: `https://api.gleif.org/api/v1/fuzzycompanySearch?q={ORG_NAME}&pageSize=20`
  Returns legal entity names + LEI codes. Use LEI to get children:
  `https://api.gleif.org/api/v1/lei-records?filter[entity.legalAddress.country]={CC}&filter[relationships.ultimate-parent.data.id]={LEI}`
- SEC EDGAR: `https://efts.sec.gov/LATEST/search-index?q={ORG_NAME}&forms=10-K,10-K405`
  Parse exhibit 21 (subsidiaries list) from 10-K filings
- OpenCorporates: `https://api.opencorporates.com/v0.4/companies/search?q={ORG_NAME}&jurisdiction_code=us`
- Crunchbase: search org page manually if API key available
- Extract: all subsidiary/affiliate company names

### Step 7b — GitHub Org
- Find org slug: search `github.com/{ORG_SLUG}` or `github.com/{PRIMARY_SLD}`
- API: `https://api.github.com/orgs/{slug}/repos?per_page=100`
- For each repo: fetch README.md, .github/workflows/*.yml, docker-compose.yml, Makefile
- Extract: domain mentions via regex `[a-z0-9-]+\.[a-z]{2,}` — filter to org-related domains

### Step 8 — Brand Variant DNS Check
- Generate variants: `{PRIMARY_SLD}.com .net .org .co .io .tech .ai .group .digital .services .global .cloud .online .info .biz`
  Also: `{PRIMARY_SLD}-group.com`, `{PRIMARY_SLD}corp.com`, `{PRIMARY_SLD}technologies.com`, `my{PRIMARY_SLD}.com`
  ccTLD variants: `.in .ae .co.uk .com.au .sg .ca .de .fr .us .eu`
- Also run for EACH confirmed subsidiary SLD — subsidiary brand variants may be separately owned
  Indian ccTLD full set for each SLD: `.co.in .net.in .org.in .firm.in .gen.in .ind.in .nic.in`
- Check DNS: `dig A {variant} @8.8.8.8 +short` — candidate if resolves
- Check NS: `dig NS {variant} @8.8.8.8 +short` — candidate even without A record
- ⚠ MANDATORY WHOIS VERIFICATION: DNS resolving ≠ owned by target.
  Run `whois {variant}` on every DNS hit. Include only if registrant org matches target or known subsidiary.
  If registrant = Endurance International Group / unrelated company → EXCLUDE.
  Brand names like "Servo", "Indane", "CPCL", "NRL" are used by many unrelated orgs worldwide.

### Step 9 — Google Copyright Dork
- DDG search: `"© 2024 {ORG_NAME}" -site:{PRIMARY_DOMAIN}`
  Also: `"© 2023 {ORG_NAME}" -site:{PRIMARY_DOMAIN}`
  Also: `"Copyright {ORG_NAME}" -site:{PRIMARY_DOMAIN}`
- Extract root domains from search result URLs
- Use `curl -sk "https://html.duckduckgo.com/html/?q={QUERY}" | grep -oP 'href="https?://[^"]+'`

### Step 9b — Extended Google Dork Patterns
- `"a {ORG_NAME} company"` — finds domains of subsidiaries that use this footer phrase
- `"{ORG_NAME}" site:linkedin.com/company` — LinkedIn subsidiary pages
- `filetype:pdf "{ORG_NAME}" "list of subsidiaries"`
- `"{PRIMARY_SLD}" site:opencorporates.com` — registered companies with similar names
- `inurl:{PRIMARY_SLD} -site:{PRIMARY_DOMAIN}` — sister domains using SLD in path
- For each dork: extract domains from result URLs, filter noise

---

## PHASE 2 — TECHNICAL PIVOTS

### Step 10 — Certificate Transparency (crt.sh)
- Wildcard query: `https://crt.sh/?q=%.{domain}&output=json`
  Fields: `common_name`, `name_value` (SAN list), `issuer_ca_id`, `not_before`
- Org search: `https://crt.sh/?O={ORG_NAME_URL_ENCODED}&output=json`
- For each cert: extract all SANs, run `to_root()` on each
- Pivot: if new root domain found, run crt.sh on that domain too (recurse 1 level)
- ⚠ RECURSE ON SUBSIDIARIES: after Step 29 confirms each subsidiary, run crt.sh wildcard on
  that subsidiary domain — subsidiaries often have their own cert-linked sister domains
- Time-correlation (Step 16c): note `not_before` of primary cert; find certs same org ±30 days

### Step 11 — Favicon Hash (Shodan)
- Fetch: `curl -sk https://{PRIMARY_DOMAIN}/favicon.ico -o favicon.ico`
- Hash: `python3 -c "import mmh3,base64,sys; d=open('favicon.ico','rb').read(); print(mmh3.hash(base64.encodebytes(d)))"`
- Shodan search: `https://api.shodan.io/shodan/host/search?key={SHODAN_KEY}&query=http.favicon.hash:{HASH}`
  Extract `ip_str` and `hostnames` from results
- Also try SpyOnWeb: `https://spyonweb.com/favicon-{HASH}` (manual check)
- Reverse DNS found IPs: `dig -x {IP} @8.8.8.8 +short` → extract root domains

### Step 12 — GA/GTM Tracker ID Pivot
- Extract from page source: `grep -oP 'UA-\d+-\d+|GTM-[A-Z0-9]+|G-[A-Z0-9]+' page.html`
- SpyOnWeb: `https://spyonweb.com/{TRACKER_ID}` — lists all sites sharing the same GA/GTM ID
- PublicWWW: `https://publicwww.com/websites/{TRACKER_ID}/` — source code search
- Extract: all domains shown sharing the tracker ID

### Step 12b — DMARC rua/ruf Pivot
- Query: `dig TXT _dmarc.{domain} @8.8.8.8 +short`
- Parse `rua=mailto:{addr}` and `ruf=mailto:{addr}` — extract domain from email address
- If rua domain ≠ primary domain → potential sister/monitoring domain → verify ownership

### Step 12c — HTTP Header Domain Pivot
- Fetch: `curl -skI https://{domain}` — check these headers:
  `Content-Security-Policy` — all domains in script-src, img-src, connect-src, frame-src
  `Access-Control-Allow-Origin` — CORS-trusted domains
  `Link` — preconnect/prefetch targets
  `Strict-Transport-Security`, `X-Frame-Options` — sometimes reveal related domains
- Extract root domains from header values, filter noise

### Step 12d — MTA-STS / BIMI / CAA DNS Pivot
- MTA-STS policy: `curl -sk https://mta-sts.{domain}/.well-known/mta-sts.txt` — check `mx:` fields
- BIMI record: `dig TXT default._bimi.{domain} +short` — extract logo URL domain
- CAA record: `dig CAA {domain} +short` — CA domains, sometimes reveal infra domains
- SPF deep parse: `dig TXT {domain} +short` — follow all `include:` chains recursively
  Each include domain may be a pivot (e.g. `include:spf.protection.outlook.com` vs `include:mail.{sistercompany}.com`)

### Step 13 — DNS Record Mining
- Query all record types: `dig NS MX TXT SOA {domain} @8.8.8.8`
- NS pivot: ONLY if NS hostname contains company's own SLD (e.g. `ns1.hcltech.com`)
  → `viewdns.info/reversens/?ns={NS_HOST}` or `https://api.hackertarget.com/findshareddns/?q={NS_HOST}`
- MX pivot: if MX hostname contains company SLD → extract root domain
- SPF `include:` → recurse; `ip4:` → reverse DNS those IPs
- TXT records: look for domain ownership proof strings mentioning other domains

### Step 14 — Shodan SSL Cert Org Search
- Query: `https://api.shodan.io/shodan/host/search?key={SHODAN_KEY}&query=ssl.cert.subject.O:"{ORG_NAME}"&facets=domains&minify=true`
- Also try partial org name if full name returns 0
- Extract: `ip_str`, `hostnames`, `domains` from each result
- Reverse DNS found IPs: `dig -x {IP} @8.8.8.8 +short` → root domains

### Step 14c — Shodan `org:` Infrastructure Search
- Query: `https://api.shodan.io/shodan/host/search?key={SHODAN_KEY}&query=org:"{ORG_NAME}"`
  This searches Shodan's BGP WHOIS attribution (different from cert search — finds servers
  where ISP/RIR has attributed the IP block to ORG_NAME)
- Extract all `ip_str` values → reverse DNS → `dig -x {IP} @8.8.8.8 +short` → root domains
- Cross-reference with ASN data from Step 22

### Step 14b — FOFA / ZoomEye / Netlas
- FOFA: `https://fofa.info/api/v1/search/all?email={FOFA_EMAIL}&key={FOFA_KEY}&qbase64={base64('cert.subject.org="{ORG_NAME}"')}&size=100&fields=host,domain`
- ZoomEye: `https://api.zoomeye.org/web/search?query=ssl.cert.organization:"{ORG_NAME}"` with `Authorization: JWT {TOKEN}`
- Netlas: `https://app.netlas.io/api/responses/?q=certificate.subject.organization:{ORG_NAME}` with `X-API-Key: {KEY}`
- Extract: domains/hostnames from results

### Step 15 — Censys Certificate Search
- API v2: POST `https://search.censys.io/api/v2/certificates/search`
  Headers: `Authorization: Basic {base64(API_ID:SECRET)}`
  Body: `{"q": "parsed.subject.organization: \"{ORG_NAME}\"", "fields": ["parsed.names", "parsed.subject.common_name"]}`
- Extract: all domain names from `parsed.names` field
- Also search: `https://search.censys.io/api/v2/hosts/search` with `services.tls.certificate.parsed.subject.organization:{ORG_NAME}`

### Step 16 — GAU + Wayback Historical URLs
- GAU: `gau --subs {domain} --o $POOL/step16_gau.txt` (go install github.com/lc/gau/v2/cmd/gau@latest)
  If GAU unavailable: Wayback CDX API:
  `curl -sk "http://web.archive.org/cdx/search/cdx?url=*.{domain}&output=text&fl=original&collapse=urlkey&limit=5000"`
- Extract: all unique root domains from URLs in results
- Interesting patterns: API subdomains, partner URLs, old brand domains

### Step 16b — Passive DNS
- CIRCL PDNS: `curl -sk -u {USER}:{PWD} https://www.circl.lu/pdns/query/{domain}` — historical DNS records
- DNSDB (Farsight): `curl -sk -H "X-API-KEY: {KEY}" "https://api.dnsdb.info/lookup/rrset/name/*.{domain}?limit=1000"`
- Extract: all domains that ever pointed to the org's IP space; historical A records

### Step 16c — CRT Time-Correlation
- Get primary domain's first cert: `https://crt.sh/?q={PRIMARY_DOMAIN}&output=json` → note earliest `not_before`
- Find certs issued within ±30 days to same org: `https://crt.sh/?O={ORG_NAME}&output=json`
  Filter to `not_before` within window → these are domains the company registered/set up at the same time
- High signal: companies often get TLS certs for all new domains in the same week

### Step 17 — HTTP Response Header Mining
- Fetch headers: `curl -skI https://{domain}`
- Parse: `Content-Security-Policy`, `Link`, `Access-Control-Allow-Origin`, `X-Powered-By`
- CSP domains in `script-src img-src connect-src frame-src font-src` that aren't CDNs → investigate
- Also fetch `https://{domain}/robots.txt` → Sitemap: directives → new domains

---

## PHASE 3 — BUILTWITH INBOUND SWEEP

### Step 18 — BuiltWith Inbound Redirects
- For each confirmed domain: `curl -sk "https://api.builtwith.com/redirect1/api.json?KEY={BUILTWITH_KEY}&LOOKUP={domain}"`
  Returns domains that redirect TO this domain (inbound redirects = confirmed owned)

⚠ RESPONSE STRUCTURE — the redirect1 API returns:
```json
{"Lookup": "example.com", "Inbound": [{"Domain": "other.com", "FirstDetected": "...", "LastDetected": "..."}], "Outbound": []}
```
Extract: `data["Inbound"][i]["Domain"]`  — NOT `data["Results"][i]["Redirect"][i]["domain_name"]`

- Extract root domain from each `Inbound[].Domain` value (strip subdomains via tldextract)
- HTTP-verify each result: confirm it actually redirects to the known domain
- No WhoXY check needed — redirect verification is sufficient proof of ownership
- Filter noise (CDNs, ad networks, google.com etc.) before writing to pool

---

## PHASE 4 — ADDITIONAL DISCOVERY

### Step 19 — SecurityTrails Associated Domains
- API: `curl -sk -H "APIKEY: {SECURITYTRAILS_KEY}" "https://api.securitytrails.com/v1/domain/{domain}/associated"`
- Also: `https://api.securitytrails.com/v1/search/list` POST with `{"filter": {"whois_email": "{EMAIL}"}}`
- Extract: all domains in `records[]` field

### Step 20 — IntelX Phonebook Search
- Search: POST `https://2.intelx.io/phonebook/search?k={INTELX_KEY}`
  Body: `{"term": "{ORG_NAME}", "buckets": [], "lookuplevel": 0, "maxresults": 200, "timeout": 20, "datefrom": "", "dateto": "", "sort": 4, "media": 0, "terminate": []}`
- Poll results: GET `https://2.intelx.io/phonebook/search/result?k={INTELX_KEY}&id={ID}&limit=200`
- Run three queries: by primary domain, by org name, by MEA/primary subsidiary domain
- Extract: domains from `selectors[].selectorvalue` field — VALIDATE before adding to pool:
  - If value contains `@`, take the part after `@` (email → domain)
  - Strip all non-DNS chars: `re.sub(r'[^a-zA-Z0-9._-]', '', val).strip('.')`
  - Parse with tldextract — only accept if `ext.domain` and `ext.suffix` both non-empty
  - Any selector with `&`, `#`, spaces, or other URL chars is noise — skip it

### Step 21 — Reverse IP Lookup
- Get IP: `dig A {PRIMARY_DOMAIN} @8.8.8.8 +short | head -1`
- HackerTarget: `curl -sk "https://api.hackertarget.com/reverseiplookup/?q={IP}"`
- ViewDNS: `curl -sk "https://viewdns.info/reverseip/?host={domain}&apikey={KEY}&output=json"`
- Filter results: keep only domains that pass entity token check (contain company name tokens)
- Co-hosted domains on shared hosting → usually not owned; require WHOIS match to confirm

### Step 22 — ASN Lookup + Reverse DNS Sweep
- Find ASN: `curl -sk "https://api.bgpview.io/search?query_term={ORG_NAME}"`
  Also: `curl -sk "https://bgp.he.net/search?search%5Bsearch%5D={ORG_NAME}&commit=Search" | grep -oP 'AS\d+'`
- Get prefixes: `curl -sk "https://api.bgpview.io/asn/{ASN}/prefixes"` — extract `ipv4_prefixes[].prefix`
- Reverse DNS sweep:
  ```bash
  echo "{CIDR}" | mapcidr -silent | dnsx -ptr -resp-only -silent -o $POOL/step22_ptr.txt
  # mapcidr: go install github.com/projectdiscovery/mapcidr/cmd/mapcidr@latest
  # dnsx: go install github.com/projectdiscovery/dnsx/cmd/dnsx@latest
  ```
- Extract root domains from PTR hostnames using `to_root()`

### Step 22b — Cert-SAN Sweep of ASN IPs
- For IPs in org's ASN that have port 443 open (use Shodan data from Step 14c):
  `echo | openssl s_client -connect {IP}:443 -servername {domain} 2>/dev/null | openssl x509 -noout -text | grep -A2 'Subject Alternative Name'`
- Extract all SAN domains → `to_root()` → filter to org-related

### Step 23 — App Store Developer ID → Privacy Policy Domains
- iTunes search: `curl -sk "https://itunes.apple.com/search?term={ORG_NAME}&entity=software&limit=50"`
  Extract `artistId` from first result
- All apps by developer: `curl -sk "https://itunes.apple.com/lookup?id={artistId}&entity=software&limit=200"`
- For each app: fetch `trackViewUrl`, extract `privacyPolicyUrl` field → root domain
- Google Play: `curl -sk "https://play.google.com/store/search?q={ORG_NAME}&c=apps" | grep -oP 'com\.{ORG_SLUG}[^"]*'`

### Step 24 — Press Releases + EDGAR 8-K Acquisition Filings
- EDGAR 8-K search: `https://efts.sec.gov/LATEST/search-index?q={ORG_NAME}&forms=8-K&dateRange=custom&startdt=2015-01-01`
  8-K item 1.01 = Material Definitive Agreement (often M&A). Extract acquisition target names.
- PR Newswire / GlobeNewswire: DDG search `"{ORG_NAME}" acquisition site:prnewswire.com`
- Each acquisition target name → DDG-resolve to domain (same as subsidiary resolution)

---

## PHASE 5 — WHOXY REGISTRANT PIVOT

### Step 25 — WhoXY Reverse-WHOIS

⚠ CREDIT BUDGET: reverse WHOIS = 1 credit/page ($0.01). Budget is 10,000 credits total.
⚠ SCAN CAP: max 30 reverse credits per scan ($0.30). Stop all reverse queries when hit.

**Pre-flight balance check (MANDATORY before any reverse query):**
```bash
BAL=$(curl -sk "https://api.whoxy.com/?key=$WHOXY_API_KEY&account=balance")
REV=$(echo "$BAL" | python3 -c "import json,sys; print(json.load(sys.stdin).get('reverse_whois_balance',0))")
echo "WhoXY reverse balance: $REV credits"
# If REV < 50, log in COVERAGE GAPS and skip all reverse queries below.
```

**Step 1 — Collect ALL seeds first (live WHOIS on every confirmed domain):**

Run live WHOIS in one batch on ALL confirmed domains (primary + every confirmed subsidiary).
Do NOT run any reverse queries until this batch is complete.
```python
import json, subprocess, pathlib, os
domains = open(f"{os.environ['ENGAGEMENT_DIR']}/included_domains.txt").read().splitlines()
seeds = {"emails": set(), "companies": set()}
SKIP_DOMAINS = {"domainsbyproxy.com","whoisguard.com","privacyprotect.org",
                "contactprivacy.com","withheldforprivacy.com","nameprivacy.com",
                "gmail.com","yahoo.com","hotmail.com","outlook.com","protonmail.com",
                "godaddy.com","bigrock.in","namecheap.com","networksolutions.com",
                "hugedomains.com","sedo.com","afternic.com","dan.com"}
SKIP_COMPANIES = {"hugeDomains","domains by proxy","godaddy","namebright",
                  "sedo","afternic","endurance international"}
for d in domains:
    r = subprocess.run(["curl","-sk",f"https://api.whoxy.com/?key={os.environ['WHOXY_API_KEY']}&whois={d}&format=json"],
                       capture_output=True, text=True)
    data = json.loads(r.stdout)
    rc = data.get("registrant_contact", {})
    email = rc.get("email_address","").lower().strip()
    company = rc.get("company_name","").strip()
    if email and email.split("@")[-1] not in SKIP_DOMAINS:
        seeds["emails"].add(email)
    if company and not any(s in company.lower() for s in SKIP_COMPANIES):
        seeds["companies"].add(company)
# Save seeds
with open(f"{os.environ['ENGAGEMENT_DIR']}/whoxy_seeds.json","w") as f:
    json.dump({"emails": list(seeds["emails"]), "companies": list(seeds["companies"])}, f, indent=2)
print(f"Seeds: {len(seeds['emails'])} emails, {len(seeds['companies'])} companies")
```

**Step 2 — Filter seeds strictly (before any reverse query):**
- Emails: keep ONLY those ending `@{primary_sld}.*` or `@{confirmed_subsidiary_sld}.*`
- Companies: keep PRIMARY + confirmed >50% subsidiaries only; skip JV/minority/unconfirmed
- Normalize company names: strip "Ltd" "Limited" "Inc" "Corp" "Co." "Private" "Pvt"
- Deduplicate: skip any email/company already queried this scan

**Step 3 — Reverse WHOIS (one controlled round, priority order):**

Track `credits_used = 0`. Stop immediately when `credits_used >= 30`.
Priority order:
  1. Primary SLD emails (highest signal)
  2. Primary company name
  3. Subsidiary SLD emails
  4. Subsidiary company names

```bash
# Per-query pattern (email):
curl -sk "https://api.whoxy.com/?key=$WHOXY_API_KEY&reverse=whois&email={EMAIL}&page=1"
# Per-query pattern (company):
curl -sk "https://api.whoxy.com/?key=$WHOXY_API_KEY&reverse=whois&company={COMPANY}&page=1"
```
- Page 1 → 0 results: stop (0 extra credits used).
- Page 1 → results: fetch pages 2 and 3 only. Never go beyond page 3.
- After each page: `credits_used += 1`. If `credits_used >= 30` → log WHOXY SCAN BUDGET EXHAUSTED in COVERAGE GAPS and stop.

**Skip these email domains (privacy proxies / registrar / webmail):**
  domainsbyproxy.com, whoisguard.com, privacyprotect.org, contactprivacy.com,
  withheldforprivacy.com, nameprivacy.com, domainprivacygroup.com,
  gmail.com, yahoo.com, hotmail.com, outlook.com, protonmail.com,
  godaddy.com, bigrock.in, namecheap.com

⚠ FIELD NAME: WhoXY reverse-WHOIS returns `search_result[].domain_name` (NOT `domain_list`)
  Live WHOIS returns `registrant_contact.email_address` and `registrant_contact.company_name`

### Step 25b — amass intel
- Run: `amass intel -d {PRIMARY_DOMAIN} -whois -src -timeout 15 -o $POOL/step25b_amass.txt`
- Also: `amass intel -org "{ORG_NAME}" -src -timeout 15 >> $POOL/step25b_amass.txt`
- Extract root domains from output using `to_root()`
- amass intel = root domain discovery via reverse-WHOIS + ASN + cert transparency
  (NOT amass enum which does subdomain enumeration — never run amass enum)

### Step 25c — Registration Batch Pivot (Same-Day Registrations)
- WhoXY WHOIS history for primary domain:
  `curl -sk "https://api.whoxy.com/?key={WHOXY_KEY}&history={PRIMARY_DOMAIN}"`
  Extract `create_date` from earliest record (e.g. `2003-08-19`)
- Query domains registered on same date by same company:
  `curl -sk "https://api.whoxy.com/?key={WHOXY_KEY}&reverse=whois&company={COMPANY}&date={DATE}"`
  Also query ±3 days (companies often register batches over a week)
- Repeat for confirmed >50% subsidiary domains (max 5) if balance permits: get creation date, batch-pivot

### Step 26 — WhoisXML Reverse-WHOIS + ARIN/RIPE Network WHOIS
- WhoisXML: POST `https://reverse-whois-api.whoisxmlapi.com/api/v2`
  Body: `{"apiKey": "{KEY}", "searchType": "current", "mode": "purchase", "punycode": true, "basicSearchTerms": {"include": ["{ORG_NAME}"]}}`
  Also search by registrant email domains
- ARIN (North America): `curl -sk "https://rdap.arin.net/registry/entity/{ORG_HANDLE}"` — get network list
  Search: `curl -sk "https://rdap.arin.net/registry/entities?fn={ORG_NAME}&role=registrant"`
- RIPE (Europe): `curl -sk "https://rdap.db.ripe.net/entities?fn={ORG_NAME}"` → get org-id → `https://rdap.db.ripe.net/entity/{ORG_ID}`
- APNIC (Asia-Pacific): `curl -sk "https://rdap.apnic.net/entities?fn={ORG_NAME}"`

---

## PHASE 6 — ADVANCED OSINT PIVOTS

### Step 33 — URLScan.io
- Search by domain: `curl -sk -H "API-Key: {URLSCAN_KEY}" "https://urlscan.io/api/v1/search/?q=domain:{PRIMARY_DOMAIN}&size=100"`
- Search by ASN: `https://urlscan.io/api/v1/search/?q=asn:{ASN}&size=100`
- Search by page title: `https://urlscan.io/api/v1/search/?q=page.title:"{ORG_NAME}"&size=100`
- Extract: `results[].page.domain` from each result → `to_root()`
- Also: `results[].lists.domains[]` for domains found on each scanned page

### Step 34 — PublicWWW Source Code Search
- Search tracking ID: `https://publicwww.com/websites/{GA_ID}/` (from Step 12)
- Search copyright string: `https://publicwww.com/websites/"{ORG_NAME}"/`
- Search JS endpoint patterns: `https://publicwww.com/websites/"{PRIMARY_SLD}."/`
- Requires premium for full results — scrape what's visible in free tier

### Step 35 — VirusTotal Graph (Related Domains)
- Related domains per confirmed domain:
  `curl -sk -H "x-apikey: {VT_KEY}" "https://www.virustotal.com/api/v3/domains/{domain}/related_domains"`
  Also: `/communicating_files`, `/subdomains` (use subdomains only to find more root domains, not to enumerate them)
- Extract: domains from `data[].id` where `data[].type == "domain"`

### Step 36 — Bug Bounty Scope Lists
- HackerOne GraphQL: `curl -sk -H "Authorization: Bearer {H1_TOKEN}" -H "Content-Type: application/json" -d '{"query":"{ teams(first:100) { edges { node { handle, in_scope_assets { edges { node { asset_identifier } } } } } } }"}' https://api.hackerone.com/graphql`
- Bugcrowd: `curl -sk "https://bugcrowd.com/programs.json"` — scope includes all owned domains
- Intigriti: `https://api.intigriti.com/core/researcher/programs?pageSize=100`
- Filter to programs matching `{ORG_NAME}` or `{PRIMARY_SLD}` — scope = all confirmed domains they control

### Step 37 — Trademark / UDRP Records
- WIPO UDRP: `https://www.wipo.int/amc/en/domains/search/` — search complainant = ORG_NAME
  Dispute records reveal domains the company actively protects/wants
- USPTO TESS: `https://tmsearch.uspto.gov/api/search/efetch?query=on:{ORG_NAME_ENCODED}&fields=mark,owner`
  Owner search reveals related brand names registered to the company
- Extract: domain names from dispute records; brand names from trademark records → resolve to domains

### Step 38 — TLD / ccTLD Variation Sweep
- For each confirmed SLD (e.g. `hcltech`, `hcl`, `actian`):
  Probe these TLDs: `.com .net .org .co .io .tech .ai .group .digital .in .ae .co.uk .com.au .sg .ca .de .fr .us .eu .me .info .biz .co.in .net.in .org.in .firm.in .gen.in .ind.in`
  Also country-specific patterns for target company's home country
- Check DNS: `dig A {sld}.{tld} @8.8.8.8 +short` — include resolved variants
- Indian company special case: also probe `{sld}.co.in .net.in .org.in .firm.in .gen.in .ind.in .nic.in`
- ⚠ REFINERY/FACILITY SWEEP — for Indian energy, oil & gas, industrial companies:
  Probe `{city}refinery.in`, `{city}refinery.com`, `{city}petrochemicals.in` for all major cities
  where the company has facilities. IOCL cities: panipat, mathura, gujarat, haldia, barauni,
  bongaigaon, digboi, guwahati, paradip, numaligarh, mangalore, vizag, koyali, trombay
  ⚠ MANDATORY WHOIS on every hit — many refinery city domains are parked or owned by individuals/
  hosting companies (Endurance International Group = BigRock registrar = NOT the target company).
  Include ONLY if WHOIS registrant org = target or confirmed >50% subsidiary.
  Known exclusions: nrl.com (National Rugby League Australia), bvfcl.com (Endurance registrar trap),
  hurl.co.in (HURL — IOCL stake 29.67% ≤50% → excluded), avi-oil.com (AVI-OIL — 50% JV → excluded)

### Step 39 — Sitemap + robots.txt + Hunter.io Email Pivot
- Sitemap recursive: `curl -sk https://{domain}/sitemap.xml` — parse `<loc>` tags
  If sitemap index, follow `<sitemap><loc>` pointers
- robots.txt: `curl -sk https://{domain}/robots.txt` — follow `Sitemap:` directives
- Hunter.io domain search: `curl -sk "https://api.hunter.io/v2/domain-search?domain={domain}&api_key={HUNTER_KEY}"`
  Extract `data.emails[].domain` — email domains found → may reveal sister domains
  Also extract all unique domain patterns from email addresses

---

## PHASE 7 — VERIFICATION & CATEGORIZATION

### Step 27 — DNS Triple Verification
- For every candidate in pool: `dig A {domain} @8.8.8.8 +short` → has_a
- `dig NS {domain} @8.8.8.8 +short` → has_ns (registered even without A)
- `dig MX {domain} @8.8.8.8 +short` → has_mx
- Use `dns_profile(domain)` from recon_common.py — returns `{registered, has_a, a, ns, mx, exchange_mx}`
- Unregistered domains (NXDOMAIN on all three) → EXCLUDED

### Step 28 — HTTP Verification + Parking Detection
- `curl -skIL --max-time 15 https://{domain}` — follow redirects, get final URL + status
- Extract page title: `curl -sk --max-time 15 https://{domain} | grep -i '<title'`
- Parking check: use `is_parked(body, title, redirect_url)` from recon_common.py
  Patterns: "domain for sale", "GoDaddy", "Sedo", "Afternic", "Dan.com", "ParkingCrew", "buy this domain"
- If redirects to PRIMARY_DOMAIN → OWNED (strong signal)
- If redirects to unrelated domain → investigate redirect target

### Step 28b — Government Domain Discovery (.gov.in)
- Indian PSUs (public sector undertakings) and their subsidiaries can hold .gov.in domains via NIC
- DDG search: `site:gov.in "{ORG_NAME}"` and `site:gov.in "{SUBSIDIARY_NAME}"` for each subsidiary
- Also try: `{ORG_SLD}tenders.gov.in`, `{ORG_SLD}etenders.gov.in`, `{ORG_SLD}apply.gov.in`
- For each subsidiary: `{SUB_SLD}tenders.gov.in`, `{SUB_SLD}etenders.gov.in`
- .gov.in domains are always owned by the named government entity — no WHOIS verification needed
- NIC GIGW portal: `https://guidelines.gov.in/list-of-govt-websites` — searchable list

### Step 29 — Subsidiary Domain Verification + Stake Gate
- For each subsidiary company name: check if a domain was found (via crt.sh, WhoXY, etc.)
- DDG-resolve if needed: `"{SUBSIDIARY_NAME}" official website` → extract domain from top result
- Verify with DNS + WHOIS org match or SSL cert org match
- STAKE LOOKUP (if not already in stake_register) — sources in priority order:
    1. BSE/NSE shareholding disclosure (Indian listed companies — most accurate, updated quarterly):
       BSE: `curl -sk "https://api.bseindia.com/BseIndiaAPI/api/ShareHoldingPatern/w?scripcode={BSE_CODE}&flag=C&quarterdate="` 
       NSE: `curl -sk "https://www.nseindia.com/api/corporate-shareholding-patterns?symbol={SYMBOL}&series=EQ"`
       Find BSE code: DDG search `"{SUBSIDIARY_NAME}" BSE scripcode` or `site:bseindia.com "{SUBSIDIARY_NAME}"`
       Promoter group % = parent company's effective stake
    2. MCA21 filing (all Indian registered companies — authoritative):
       Search: `curl -sk "https://efiling.mca.gov.in/eFiling/helperservices/masterdata/getmasterdata?companyName={NAME}"`
       Annual return (MGT-7) lists exact promoter shareholding %
       Also check: `site:mca.gov.in "{SUBSIDIARY_NAME}"` via DDG
    3. Annual report table (parent company's report — "Statement pursuant to Section 129"):
       Always lists % shareholding for every subsidiary/associate/JV
    4. Wikipedia infobox — useful but often outdated by 1–2 years, verify against BSE/NSE
    5. GLEIF: `https://api.gleif.org/api/v1/fuzzycompanySearch?q={NAME}` → get LEI → check relationship type
    6. If all fail → record stake as "unknown" → treat as JV (26–50% rule)
    ⚠ Wikipedia is the LEAST reliable for stake % — always cross-check with BSE/NSE or MCA for Indian companies
- After stake confirmed, apply recursion rule:
    >50%  → SUBSIDIARY: include domain, run Level-1 AND Level-2 recursion
    ≤50%  → EXCLUDED: do NOT include domain, do NOT recurse (even 50% exactly = not controlled)
  Known ≤50% exclusions for IOCL: Petronet LNG (12.5%), HURL (29.67%), AVI-OIL (~50%), Adani JV (50%)

### Step 30 — Live WHOIS Registrant Check
- `whois {domain}` — parse registrant org, email, name
- Match to known entity list (ORG_NAME + all subsidiary names)
- `brand_phrase_hits(text, org_name)` from recon_common.py — True if ≥2 distinctive tokens match
- Unknown/privacy-protected: flag for RDAP check (Step 30b)

### Step 30b — RDAP Structured Registrant Check
- RDAP: `curl -sk "https://rdap.org/domain/{domain}"` → JSON response
  Parse `.entities[].vcardArray` for fn (name), org, email fields
  Parse `.entities[].roles` — look for "registrant" role
- More reliable than raw whois text for parsing structured registrant data
- Use to resolve ambiguous/REDACTED whois results from Step 30

### Step 31 — Ownership Scoring
- Use `score_ownership(signals)` from recon_common.py
- Build `signals` set from evidence collected in Steps 27-30b:
  - `whois_registrant_match` (weight 40) — WHOIS org matches
  - `builtwith_verified_redirect` (weight 35) — confirmed inbound redirect
  - `whoxy_reverse_match` (weight 30) — found via reverse-WHOIS
  - `dmarc_rua_shared` (weight 25) — DMARC rua domain match
  - `custom_ns_cluster` (weight 20) — NS on company's own nameserver
  - `redirects_to_confirmed` (weight 20) — HTTP redirects to known domain
  - `brand_phrase_in_title` (weight 15) — org name in page title
  - `exchange_mx` (weight 5, weak) — Microsoft 365 MX
  - `owned_cloud_provider` (weight 5, weak) — cloud hosting
- Verdict OWNED (≥30) or LIKELY (≥15) → include; WEAK or NONE → EXCLUDED

### Step 32 — Final Report
```
=== DOMAIN REPORT: {ORG_NAME} ===

ACTIVE (N)    — resolves + confirmed owned (>50% stake or direct ownership)
  example.com          [source]  stake=100%   evidence=whois_match+http_content
  cpcl.co.in           [source]  stake=51.88% evidence=whois_match (CPCL subsidiary)

INACTIVE (N)  — registered + owned, no live HTTP
  example.net  — [source] NS present, no A record, WHOIS match

DANGLING (N)  — registered, no A record, potential takeover risk
  old-brand.com — [source] registered but parked/expired, WHOIS previously matched target

EXCLUDED (N)  — found but not owned by target
  petronetlng.com  — IOCL stake only 12.5% (minority)
  servo.com        — unrelated org (different company owns this TLD variant)
  baraunirefinery.in — registrant = Endurance International Group (registrar, not target)
  indianoiladani.com — 50% JV, not controlled by target
```
- Only include domains where target holds >50% stake OR direct ownership is confirmed via WHOIS+HTTP
- ≤50% stake (any JV, minority, associate) = EXCLUDED — not part of target's attack surface
- Brand variants require WHOIS confirmation — do not include based on brand name match alone

---

## SHARED HELPERS — recon_common.py
Import: `sys.path.insert(0, '/home/kali/recon'); from recon_common import *`

| Function | Signature | Use |
|----------|-----------|-----|
| `to_root` | `(hostname) → str` | Extract registrable root domain using PSL |
| `clean_roots` | `(hosts) → set` | Root + dedup + noise filter a collection |
| `is_noise` | `(root) → bool` | True if CDN/registrar/social/analytics domain |
| `dig` | `(domain, rtype="A") → str` | Raw dig +short output via @8.8.8.8 |
| `a_record` | `(domain) → str` | First A record IP or "" |
| `dns_profile` | `(domain) → dict` | {registered, has_a, a, ns, mx, exchange_mx} |
| `is_registered` | `(domain) → bool` | True if any DNS record exists |
| `curl_text` | `(url, timeout=15) → str` | Fetch URL with browser UA |
| `curl_json` | `(url, timeout=15, headers={}) → dict` | Fetch URL, parse JSON |
| `post_json` | `(url, payload, headers={}) → dict` | POST JSON, parse response |
| `extract_roots_from_html` | `(html) → set` | Extract all root domains from HTML |
| `load_pool` | `(pool_dir) → set` | Load all step*.txt files into one set |
| `write_roots` | `(pool_dir, filename, roots) → int` | Normalise + write roots to pool file |
| `step_done` | `(pool_dir, filename) → bool` | True if step output already exists |
| `is_parked` | `(body, title="", redirect_url="") → bool` | Parking/for-sale page detection |
| `score_ownership` | `(signals: set) → (score, verdict, has_strong)` | Ownership verdict |
| `brand_phrase_hits` | `(text, org_name) → bool` | ≥2 distinctive org name tokens in text |
