# Automated job discovery

`tools/discover_postings.py` finds new job postings without any LLM/agent
calls — plain HTTP requests plus a keyword-overlap fit heuristic — and adds
them to `MARKET_HISTORY`. High-fit finds (score ≥ 7/10) get an immediate
WhatsApp alert; everything else shows up in the next daily digest.

## Sources

### LinkedIn (`tools/linkedin-search/`)

Vendored from [MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search)
(MIT licensed), a TypeScript/Bun CLI with zero runtime dependencies that
queries LinkedIn's public `jobs-guest` endpoints — no login, no API key.

> **ToS note:** the skill's own README states automated access is against
> LinkedIn's Terms of Service. Kept to 5 short queries per run, personal
> use only, per that same guidance — see `tools/linkedin-search/SKILL.md`.

### Workday (`WORKDAY_SOURCES` in `discover_postings.py`)

Companies that run their careers site on Workday expose the same JSON API
their own site's JavaScript calls:

```
POST https://<tenant>.wd{1-5}.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
Content-Type: application/json

{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "<keyword>"}
```

This is lower ToS risk than LinkedIn (it's not an explicit scraping
violation, just an unpublished-but-public endpoint), and higher precision
since you're querying one specific target company directly.

**No public location facet** — results come back for every country the
company hires in, so `fetch_workday()` filters client-side via
`_is_chile_viable_location()`. Confirmed by testing: without this filter, an
Abbott query returned postings from Malaysia, Uzbekistan, Algeria, etc.
alongside the 4 actually relevant to Chile.

A first version of this filter just checked for "Chile" or "Remote" in
`locationsText`, but "Remote" alone isn't Chile-eligible — a lot of Workday
postings are "`<Country>` - Remote" (remote *within* that country's borders,
its own residency/work-authorization requirements). That let a Merck
"USA - REMOTE - REMOTE" posting requiring US Southeast residency straight
through, and since `compute_fit()` only looks at the title, it scored a
perfect 10/10 and got auto-added as a HIGH-priority application. Fixed by
flipping to an allowlist: a "remote" location only counts as Chile-viable if
it names Chile/LATAM/global explicitly, or is a bare unqualified "Remote"
with nothing else — anything naming another specific country is excluded.
`_has_us_region_restriction()` catches the same failure mode when it's baked
into the *title* instead (the Merck posting literally had "(Southeast)" in
its title). Both checks also run on `fetch_linkedin()` results, not just
Workday's.

#### Finding a company's tenant + site ID

1. Visit the company's careers page and see if it redirects to (or embeds
   an iframe pointing at) `*.myworkdayjobs.com` — that's the tenant.
2. The `<site>` slug isn't always guessable. Try common patterns against
   the endpoint above with `curl`:
   ```bash
   for site in External Careers "${company}careers" "${Company}Careers"; do
     curl -s -X POST "https://<tenant>.wd5.myworkdayjobs.com/wday/cxs/<tenant>/$site/jobs" \
       -H "Content-Type: application/json" \
       -d '{"appliedFacets":{},"limit":1,"offset":0,"searchText":""}'
   done
   ```
   A `404` with `errorCode: S21` means wrong site ID — keep trying. A `200`
   with a `total` count confirms it. (Abbott's turned out to be
   `abbottcareers`, found this way.)
3. `<tenant>.wd1` through `<tenant>.wd5` are all in use across different
   companies — if `wd5` 404s outright (not an S21 error), try the others.
4. Add the confirmed `{company, tenant, wd, site}` to `WORKDAY_SOURCES`.

**Companies still worth discovering**: `WORKDAY_SOURCES` currently covers
Abbott, Pfizer, Merck (MSD), AstraZeneca, and Sanofi. J&J and GSK are
confirmed on Workday (tenant responds, just need the right site slug) but
not yet wired in.

## Fit heuristic

Plain keyword matching (`FIT_KEYWORDS` in `discover_postings.py`), no LLM.
Any title containing "liaison" + medical/scientific/science, or the bare
"MSL" abbreviation, is treated as a near-exact match for Francisco's core
target role and scores ≥ 8/10 regardless of other keywords — this covers
title variants like "Medical Science Liaison" vs "Medical Scientific
Liaison" and Spanish postings ("MSL Oncología"). Additional keywords (Medical
Affairs, Clinical Research, Regulatory Affairs, GCP, etc. — English and
Spanish) add smaller bonuses on top.

This is intentionally simple and won't catch every nuance an LLM-based fit
score would — it's a first-pass filter to decide "immediate alert or just
bundle into the daily digest," not a final judgment on the role.

## Not yet automated

- **Greenhouse / Lever / SmartRecruiters**: clean public APIs, ToS-clean,
  but no confirmed usage among Francisco's target pharma companies — low
  priority.
- **Jooble API**: has Chile coverage (`cl.jooble.org`) but needs a free API
  key registration — worth adding.
- **iCIMS** (likely used by IQVIA, Syneos Health, ICON): endpoint pattern
  identified but not verified working yet.
- **Chilean job boards** (BNE, Laborum, Trabajando.com, Chiletrabajos):
  no clean public API found; only sitemap URL enumeration, which yields
  URLs but not structured fields, and sits in more of a ToS gray area.
