# Implementation Plan

[Overview]
Rebuild AutoAppy's job discovery and scoring engine to dramatically increase relevant job coverage and scoring accuracy using only free tools, inspired by industry leaders (JobCopilot, FastApply, LoopCV) but with a fully offline-capable, free-tier architecture.

This plan addresses four interconnected problems observed in the live run analysis:
1. **Job Discovery Gap**: Current sources (JobSpy/LinkedIn, Adzuna, GH/Lever direct) miss thousands of relevant jobs. Industry leaders cover 500K+ company career pages. We need Google SERP-based discovery, SmartRecruiters API, Wellfound scraping, Cutshort scraping, and career page ATS auto-probing to dramatically widen the funnel.
2. **Scoring Inaccuracy**: The current single-pass LLM scoring is slow (burns free-tier tokens), gives inconsistent scores (65 default on failure pollutes queue), and lacks structured skill extraction. We add a **free two-stage pipeline**: TF-IDF cosine similarity pre-filter (eliminates bottom 60% instantly, no LLM needed) → FlashText skill extraction → LLM deep scoring (only top 40%).
3. **Auto-Apply Failures**: LinkedIn needs credentials for Easy Apply. Stripe/company portals block GH boards. Workday URL format issues. Aggregator redirects not resolved before launching browser. Need systematic redirect resolution at discovery time, not apply time.
4. **Manual Apply UX**: 29 jobs in manual_review queue with no easy way to act on them. Dashboard needs a one-click "Open & Apply" button per job with pre-filled docs.

The architecture stays fully Python, uses SQLite, runs on free LLM tiers (Gemini Flash + Groq), and adds only libraries already installed (scikit-learn, flashtext, rapidfuzz) plus Serper API (already have key).

[Types]
New data structures for multi-stage scoring, skill extraction, source metadata, and career page probe results.

**New: ScoringStages dataclass** (scoring/scorer.py)
```python
@dataclass
class ScoringStages:
    tfidf_score: float = 0.0          # Stage 1: TF-IDF cosine similarity (0-1)
    skill_match_score: float = 0.0     # Stage 2: FlashText skill overlap ratio (0-1)
    llm_score: float = 0.0             # Stage 3: LLM deep score (0-100)
    final_score: float = 0.0           # Weighted composite (0-100)
    matched_skills: list[str] = field(default_factory=list)   # Skills from JD matching resume
    missing_skills: list[str] = field(default_factory=list)   # Required skills not in resume
    extracted_jd_skills: list[str] = field(default_factory=list)  # All skills found in JD
    skipped_llm: bool = False          # True if TF-IDF was low enough to skip LLM
```

**Extended: ScoreResult dataclass** (scoring/scorer.py)
Add fields: `tfidf_score`, `skill_match_score`, `matched_skills`, `missing_skills`, `extracted_jd_skills`

**New: CareerPageProbeResult dataclass** (discovery/career_page_prober.py)
```python
@dataclass
class CareerPageProbeResult:
    company_name: str
    detected_ats: str          # greenhouse | lever | ashby | workday | smartrecruiters | unknown
    board_slug: str | None     # e.g. "stripe" for GH
    job_count: int
    jobs: list[RawJob]
    probe_method: str          # api | serp | redirect
```

**Extended: RawJob dataclass** (discovery/base.py)
Add fields:
- `posted_at: Optional[str]` — ISO date of posting
- `employment_type: Optional[str]` — full-time / contract / internship
- `department: Optional[str]` — Engineering / Product / Data
- `seniority: Optional[str]` — junior / mid / senior / staff / principal
- `skills_required: Optional[list[str]]` — extracted from description
- `redirect_resolved: bool = False` — whether apply_url has been redirect-resolved

**Extended: Job SQLAlchemy model** (tracker/models.py)
Add columns:
- `posted_at VARCHAR(32)` — job posting date
- `employment_type VARCHAR(32)` — full-time/contract
- `department VARCHAR(64)` — engineering/product etc
- `seniority_level VARCHAR(32)` — junior/mid/senior/staff/principal
- `skills_required TEXT` — JSON list of extracted skills
- `tfidf_score FLOAT` — TF-IDF pre-score
- `skill_match_score FLOAT` — FlashText skill overlap score
- `redirect_resolved BOOLEAN DEFAULT 0` — whether URL was resolved at discovery
- `source_query VARCHAR(256)` — the search query that found this job

[Files]
New files to be created and existing files to be modified for the complete rebuild.

**NEW FILES:**

`autoapply/discovery/serper_discovery.py`
— Google SERP-based job discovery using the Serper API key already in .env
— Queries: `site:boards.greenhouse.io {role} {location}`, `site:jobs.lever.co {role} {location}`, `site:jobs.ashbyhq.com {role}`, `site:careers.smartrecruiters.com {role} india`
— Parses direct ATS URLs from SERP results → RawJob objects
— Cost: free (Serper API key already available)

`autoapply/discovery/career_page_prober.py`
— Given a list of company names, auto-probes Greenhouse → Lever → Ashby → SmartRecruiters → Workday in sequence
— Uses the existing public ATS APIs (same approach as webdata_labs Apify actor but free/local)
— Returns RawJob list with direct apply URLs and ATS metadata
— Implements the "monitoring mode" concept: remembers seen job IDs, only returns delta

`autoapply/discovery/smartrecruiters.py`
— Fetches jobs from SmartRecruiters public API for configured company IDs
— Endpoint: `https://api.smartrecruiters.com/v1/companies/{id}/postings`
— No auth required for public job listings
— Already partially used in applicator — move discovery logic here

`autoapply/discovery/wellfound_scraper.py`
— Scrapes Wellfound (AngelList) job listings for startup/tech roles
— Uses httpx + BeautifulSoup (already installed)
— Focuses on India/Bangalore roles and remote-friendly startups
— Implements rate limiting and respectful scraping

`autoapply/discovery/cutshort_scraper.py`
— Scrapes Cutshort.io for India tech/startup jobs
— GraphQL or public JSON API endpoint
— Strong India signal: salary bands, funding stage, tech stack listed

`autoapply/scoring/skill_extractor.py`
— FlashText-based O(N) skill extraction from job descriptions and resumes
— Maintains a curated skills dictionary (tech skills, frameworks, tools)
— Extracts canonical skill names ("React.js" → "React", "node.js" → "Node.js")
— Returns matched/missing skill sets for scoring and resume tailoring

`autoapply/scoring/tfidf_scorer.py`
— TF-IDF cosine similarity pre-scorer using scikit-learn
— Builds TF-IDF vectors from resume text + job description text
— Returns similarity score (0.0–1.0)
— Used as Stage 1: jobs below 0.15 similarity are skipped without LLM call
— Also provides keyword overlap ratio as secondary signal

`autoapply/discovery/url_resolver_async.py`
— Async version of URL resolver that runs at discovery time (not apply time)
— Resolves aggregator redirects (Adzuna, Indeed, Naukri, Remotive) → canonical ATS URLs
— Updates job's apply_url and ats_type in DB immediately after discovery
— Runs concurrently using asyncio.gather for speed
— Marks job.redirect_resolved = True so apply phase skips resolution

**MODIFIED FILES:**

`autoapply/discovery/__init__.py`
— Add new sources: serper_discovery, career_page_prober, smartrecruiters, wellfound_scraper, cutshort_scraper
— Add post-discovery async URL resolution step
— Add source health check: report which sources returned 0 jobs (likely broken)
— Increase ThreadPoolExecutor max_workers to 12 (from 8)

`autoapply/discovery/base.py`
— Add new optional fields to RawJob: posted_at, employment_type, department, seniority, skills_required, redirect_resolved, source_query
— Add seniority detection from title: extract_seniority(title) → junior/mid/senior/staff/principal
— Extend clean_html() to also decode HTML entities more completely

`autoapply/scoring/scorer.py`
— Add two-stage pre-scoring before LLM: TF-IDF (Stage 1) → Skill extraction (Stage 2) → LLM (Stage 3)
— Stage 1 threshold: tfidf_score < 0.12 → skip LLM (mark as "skipped_low_similarity")
— Stage 2: inject extracted_jd_skills and matched_skills into LLM prompt
— Update ScoreResult with ScoringStages data
— Remove hardcoded fallback score of 65 → use 0 with error=True
— Add resume skill profile builder: build_resume_skill_profile(master_resume) → set of canonical skills

`autoapply/scoring/prompts.py`
— Add `skill_context` parameter to build_score_prompt()
— Inject matched skills and missing skills into scoring prompt for LLM
— Add `SKILL_EXTRACTION_PROMPT` template for standalone skill extraction from JD
— Update cover letter prompt to use matched skills as evidence points

`autoapply/tracker/models.py`
— Add 8 new columns to Job model: posted_at, employment_type, department, seniority_level, skills_required, tfidf_score, skill_match_score, redirect_resolved, source_query

`autoapply/tracker/db.py`
— Add new columns to _migrate_db()
— Add get_jobs_by_seniority(), get_jobs_by_skills(), get_jobs_pending_url_resolution() helpers
— Update update_job_score() to save tfidf_score and skill_match_score

`autoapply/applicator/__init__.py`
— Check redirect_resolved flag: if True, skip URL resolution at apply time (already done at discovery)
— Add LinkedIn credential check at startup with clear instruction if missing
— Add company portal whitelist: companies known to require login (stripe, notion, figma) → route directly to manual_review with note

`autoapply/applicator/url_resolver.py`
— Add resolve_remotive_apply_url(): Remotive/RemoteOK jobs link to company careers, resolve to actual ATS
— Improve follow_redirects() to detect JS-redirect pages (meta refresh, window.location)

`config.yaml`
— Add new sources section: serper_discovery, wellfound, cutshort, smartrecruiters targets
— Add scoring section: tfidf_threshold, skill_match_threshold
— Add discovery section: url_resolve_at_discovery, max_redirect_resolution_workers
— Add SERPER_API_KEY reference

`requirements.txt`
— Add: scikit-learn>=1.4.2, flashtext>=2.7, rapidfuzz>=3.0 (already installed)
— Remove sentence-transformers (not needed, TF-IDF is sufficient and lighter)

`.env.example`
— Add SERPER_API_KEY, LINKEDIN_EMAIL, LINKEDIN_PASSWORD

[Functions]
New functions and modifications to existing ones across the codebase.

**NEW FUNCTIONS:**

`autoapply/discovery/serper_discovery.py`
- `fetch_serper_jobs(roles, locations, serper_key, config) → List[RawJob]`
  Queries Google SERP with site: operators for Greenhouse, Lever, Ashby, SmartRecruiters URLs.
  Parses slug+job_id from matching URLs. Fetches full job via ATS API. Returns RawJob list.
  Rate limited to 100 queries/month on free Serper tier (plan queries carefully).

- `_build_serper_queries(roles, locations) → List[str]`
  Generates targeted queries: `site:boards.greenhouse.io "{role}" "{location}"`
  Limits to 20 queries per run to conserve free tier API calls.

- `_parse_ats_url_from_serp_result(url) → dict`
  Detects ATS from SERP result URL, extracts slug+job_id. Returns {ats, slug, job_id, url}.

`autoapply/discovery/career_page_prober.py`
- `probe_company_jobs(company_name, ats_hint=None) → CareerPageProbeResult`
  Given a company name, probes GH → Lever → Ashby → SmartRecruiters in order.
  Returns jobs found on first successful probe.

- `probe_companies_batch(company_names, config) → List[RawJob]`
  Runs probe_company_jobs() concurrently for all companies in config.
  Used for targeted company lists (Zomato, Swiggy, PhonePe, etc.) that aren't in GH/Lever lists.

- `_normalize_company_slug(company_name) → str`
  Converts "Zoho Corporation" → "zoho", "Y Combinator" → "ycombinator"
  Handles common brand variations and corporate suffix removal.

`autoapply/discovery/smartrecruiters.py`
- `fetch_smartrecruiters_jobs(company_ids, roles_filter, config) → List[RawJob]`
  Queries SmartRecruiters public API for each company ID.
  Filters by role keywords. Returns RawJob list.

`autoapply/discovery/wellfound_scraper.py`
- `fetch_wellfound_jobs(roles, config) → List[RawJob]`
  Scrapes Wellfound job listings using httpx + BeautifulSoup.
  Uses their public search endpoint with JSON response.
  Focuses on India/remote roles.

`autoapply/discovery/cutshort_scraper.py`
- `fetch_cutshort_jobs(roles, locations, config) → List[RawJob]`
  Scrapes Cutshort.io for India tech/startup jobs.
  Parses salary ranges, funding stage, tech stack.

`autoapply/scoring/skill_extractor.py`
- `build_skill_processor() → KeywordProcessor`
  Builds FlashText processor from comprehensive skills dictionary.
  Maps variations: {"reactjs": "React", "react.js": "React", ...}
  Covers: languages, frameworks, databases, tools, cloud, AI/ML.

- `extract_skills_from_text(text, processor) → list[str]`
  Extracts canonical skill names from any text in O(N) time.

- `compute_skill_match(jd_skills, resume_skills) → tuple[float, list, list]`
  Returns (overlap_ratio, matched_skills, missing_skills).
  overlap_ratio = |matched| / max(|jd_skills|, 1)

- `build_resume_skill_profile(master_resume) → set[str]`
  Extracts all skills from resume text, experience bullets, and skills dict.
  Returns canonical skill name set for fast lookup.

`autoapply/scoring/tfidf_scorer.py`
- `build_tfidf_scorer(resume_text) → TFIDFScorer`
  Builds a TF-IDF vectorizer fitted on the resume text.
  Returns a scorer object that can score any JD against the resume.

- `score_jd_against_resume(jd_text, resume_text) → float`
  Computes cosine similarity between TF-IDF vectors of JD and resume.
  Returns 0.0–1.0 similarity score.
  Uses scikit-learn TfidfVectorizer with English stop words.

- `compute_keyword_overlap(jd_text, resume_text) → float`
  Bag-of-words keyword overlap ratio (simpler, faster backup).

`autoapply/discovery/url_resolver_async.py`
- `async resolve_jobs_urls_batch(jobs, max_concurrent=10) → list[Job]`
  Resolves apply_url for all aggregator-sourced jobs concurrently.
  Updates each job's apply_url, ats_type, redirect_resolved in DB.
  Called after discovery, before scoring.

**MODIFIED FUNCTIONS:**

`autoapply/scoring/scorer.score_job()` (scorer.py)
- Add `tfidf_scorer` parameter (pre-built TFIDFScorer instance)
- Add `skill_processor` parameter (pre-built FlashText KeywordProcessor)
- Add `resume_skills` parameter (set of canonical skill names from resume)
- Stage 1: Compute tfidf_score. If < config threshold (default 0.12) → return ScoreResult(score=0, verdict="skip", skipped_llm=True)
- Stage 2: Extract JD skills, compute skill_match_score
- Stage 3: Inject skill context into LLM prompt, call LLM
- Return enriched ScoreResult with all stage scores

`autoapply/scoring/scorer.score_jobs_batch()` (scorer.py)
- Build tfidf_scorer, skill_processor, resume_skills ONCE before the loop (not per job)
- Pass these to score_job() for each job
- Track and log: jobs_skipped_tfidf, jobs_skipped_skill, jobs_llm_scored
- Print savings: "Saved X LLM calls via TF-IDF pre-filter"

`autoapply/scoring/prompts.build_score_prompt()` (prompts.py)
- Add `matched_skills: list[str]` parameter
- Add `missing_skills: list[str]` parameter  
- Inject into prompt: "SKILL MATCH: Found {matched} in JD that match your resume. MISSING: {missing}"
- This gives LLM concrete data instead of asking it to infer from full text

`autoapply/discovery/__init__.discover_jobs()` (discovery/__init__.py)
- Add new source tasks: serper_discovery, career_page_prober, smartrecruiters, wellfound, cutshort
- After all sources complete, run async URL resolution for aggregator-sourced jobs
- Add source health reporting: warn if any source returned 0 jobs

`autoapply/tracker/db.update_job_score()` (db.py)
- Add tfidf_score, skill_match_score parameters
- Save to new DB columns

`autoapply/applicator/__init__.apply_to_job()` (applicator/__init__.py)
- Check `job.redirect_resolved`: if True, skip URL resolution step (saves ~3s per job)
- Check company portal whitelist at start: if company in KNOWN_LOGIN_PORTALS → skip to manual_review immediately with specific note
- Add `KNOWN_LOGIN_PORTALS = {"stripe", "notion", "figma", "apple", "google"}` set

`run.py`
- Update run_scoring() to build tfidf_scorer and skill_processor once and pass to scorer
- Add --resolve-urls CLI flag: runs URL resolution on all unresolved aggregator jobs
- Add --probe-companies CLI flag: runs career_page_prober for configured company list

[Classes]
New classes for the TF-IDF scorer and skill extractor.

**NEW CLASSES:**

`class TFIDFScorer` (scoring/tfidf_scorer.py)
- `__init__(self, resume_text: str)`: Fits TfidfVectorizer on resume text, stores resume vector
- `score(self, jd_text: str) -> float`: Returns cosine similarity with resume (0.0-1.0)
- `batch_score(self, jd_texts: list[str]) -> list[float]`: Vectorized scoring for multiple JDs at once (fastest)
- Internal: Uses `TfidfVectorizer(ngram_range=(1,2), stop_words='english', max_features=5000)`
- Why 1-2 ngrams: catches "machine learning", "spring boot", "data pipeline" as units

`class SkillExtractor` (scoring/skill_extractor.py)
- `__init__(self)`: Builds FlashText KeywordProcessor from comprehensive skills dict
- `extract(self, text: str) -> list[str]`: Returns canonical skills from text in O(N)
- `match(self, jd_text: str, resume_skills: set[str]) -> tuple[float, list, list]`: Returns (ratio, matched, missing)
- `SKILLS_DICT`: 500+ tech skills with variations mapped to canonical names
  Categories: languages (Java, Python, Go, Rust...), frameworks (Spring Boot, FastAPI, Django...), 
  databases (PostgreSQL, Redis, MongoDB...), cloud (AWS, GCP, Azure...), 
  ai_ml (LLM, RAG, NLP, PyTorch, TensorFlow...), tools (Docker, K8s, Git, Kafka...)
  search (Elasticsearch, Apache Lucene, Solr...), messaging (Kafka, RabbitMQ, SQS...)

**MODIFIED CLASSES:**

`class ScoreResult` (scoring/scorer.py)
- Add: `tfidf_score: float = 0.0`, `skill_match_score: float = 0.0`
- Add: `matched_skills: list[str] = field(default_factory=list)`
- Add: `missing_skills: list[str] = field(default_factory=list)`
- Add: `extracted_jd_skills: list[str] = field(default_factory=list)`
- Add: `skipped_llm: bool = False`
- Update `display_score` property to show stage breakdown

`class RawJob` (discovery/base.py)
- Add optional fields: posted_at, employment_type, department, seniority, skills_required, redirect_resolved, source_query
- Add class method `from_serp_result(serp_hit, ats_data)` for building from SERP results

[Dependencies]
Only free, already-installable packages — no paid APIs, no GPU required.

**Already installed (confirmed working):**
- `scikit-learn==1.4.2` — TF-IDF vectorizer, cosine similarity
- `flashtext>=2.7` — O(N) keyword extraction
- `rapidfuzz>=3.0` — fuzzy string matching for skill name normalization
- `beautifulsoup4` — HTML parsing for Wellfound/Cutshort scrapers
- `httpx` — async HTTP client for concurrent URL resolution
- `requests` — sync HTTP for ATS API calls
- `playwright` — browser automation (already installed)

**To add to requirements.txt:**
```
scikit-learn>=1.4.2
flashtext>=2.7
rapidfuzz>=3.0
```

**External APIs used (all FREE):**
- Serper API (SERPER_API_KEY already available) — 100 queries/month free tier
  Used only for SERP-based job discovery (budget: ~20 queries per run)
- Gemini Flash API — already configured, free tier 1500 req/day
- Groq API — already configured, free tier 1000 req/day
- Greenhouse public boards API — free, no key needed
- Lever public API — free, no key needed
- Ashby public API — free, no key needed
- SmartRecruiters public job listings — free, no key needed
- Remotive.com API — free, no key needed
- RemoteOK API — free, no key needed
- Adzuna API — free tier, key already configured

**NOT added (would require GPU/paid):**
- sentence-transformers — requires PyTorch (~900MB download), overkill for this use case
- OpenAI embeddings — paid API
- Cohere embeddings — paid API

**Design decision rationale:**
TF-IDF cosine similarity is sufficient for pre-filtering because:
1. Resume and JD share domain vocabulary (tech terms, tool names)
2. We only need to separate clearly irrelevant jobs (score < 0.12) from potentially relevant ones
3. LLM handles nuanced semantic understanding for the top 40% that pass TF-IDF
4. scikit-learn TF-IDF runs in <10ms per job vs ~2s for LLM call
5. Projected savings: 60% fewer LLM calls, preserving free tier quota

[Testing]
Validation approach focused on measurable outcomes — more jobs found, better score distribution, higher auto-apply success rate.

**Validation after implementation:**

1. **Discovery coverage test** — Run `--discover-only` and count:
   - Total jobs found (target: >500 per run vs current ~200)
   - Jobs per source (verify each new source returns >0)
   - Unique companies covered (target: >200 vs current ~50)
   - India/Bangalore jobs specifically (target: >200 vs current ~100)

2. **Scoring accuracy test** — Compare before/after on a 50-job sample:
   - TF-IDF pre-filter catch rate: how many clearly irrelevant jobs does it correctly skip?
   - LLM call reduction: target 50-60% fewer LLM calls
   - Score distribution: check auto_apply/review/skip ratio is reasonable
   - Spot-check: manually verify 5 high-score jobs are genuinely relevant

3. **URL resolution test** — Verify aggregator redirect resolution:
   - Adzuna URL → canonical ATS URL conversion rate
   - ATS type detection accuracy after resolution
   - Speed: batch resolution should complete in <30s for 100 jobs

4. **Skill extraction test** — Unit test SkillExtractor:
   - Given a GH job description, verify known skills are extracted
   - Verify variations map correctly ("react.js" → "React")
   - Verify resume skill profile built correctly from master_resume.json

5. **End-to-end smoke test** — Run `python run.py --score-only` on 20 jobs:
   - Verify no crashes in new pipeline stages
   - Verify DB columns populated correctly
   - Verify LLM is only called for jobs that passed TF-IDF

6. **Apply success rate test** — Run `--apply-only` with known good jobs:
   - Verify Greenhouse API tried before Playwright
   - Verify known login portals (stripe) go directly to manual_review
   - Verify LinkedIn jobs show clear "add credentials" message

**Key metrics to track (in DB):**
- `tfidf_score` column — distribution should show clear bimodal (relevant vs not)
- `skill_match_score` column — should correlate with match_score
- `apply_attempts` column — should increase on retries
- `redirect_resolved` column — should be True for all Adzuna/Indeed jobs after resolution

[Implementation Order]
Sequenced to deliver value at each step and avoid breaking existing functionality.

1. **Add new DB columns** (tracker/models.py + tracker/db.py)
   — Add 9 new Job columns via _migrate_db(). Run-safe: columns added only if missing.
   — No functional change, just schema extension.

2. **Build SkillExtractor** (scoring/skill_extractor.py — new file)
   — Implement FlashText-based skill extraction with 500+ skill dictionary.
   — Unit-testable standalone: `python3 -c "from autoapply.scoring.skill_extractor import SkillExtractor"`

3. **Build TFIDFScorer** (scoring/tfidf_scorer.py — new file)
   — Implement scikit-learn TF-IDF cosine similarity scorer.
   — Unit-testable standalone with test JD and resume text.

4. **Integrate two-stage scoring** (scoring/scorer.py + scoring/prompts.py)
   — Add TF-IDF Stage 1 and skill extraction Stage 2 to score_job() and score_jobs_batch().
   — Update ScoreResult dataclass with new fields.
   — Update scoring prompt to inject skill context.
   — Test: run `--score-only` on 10 existing unscored jobs, verify LLM calls reduced.

5. **Add Google SERP job discovery** (discovery/serper_discovery.py — new file)
   — Implement Serper API queries for GH/Lever/Ashby/SmartRecruiters URLs.
   — Integrate into discover_jobs() in discovery/__init__.py.
   — Test: run `--discover-only`, verify new Lever/GH jobs appear.

6. **Add SmartRecruiters discovery** (discovery/smartrecruiters.py — new file)
   — Fetch jobs from SmartRecruiters API for configured companies (Freshworks, Zomato, etc.).
   — Integrate into discover_jobs().

7. **Add career page prober** (discovery/career_page_prober.py — new file)
   — Implement company name → ATS auto-detection probe.
   — Add 30+ Indian tech companies to probe list in config.yaml.
   — Integrate into discover_jobs() as optional source.

8. **Add Wellfound scraper** (discovery/wellfound_scraper.py — new file)
   — Scrape Wellfound for India/remote startup jobs.
   — Integrate into discover_jobs().

9. **Add async URL resolution at discovery time** (discovery/url_resolver_async.py)
   — Resolve Adzuna/Indeed/Remotive redirects right after discovery.
   — Mark jobs with redirect_resolved=True.
   — Test: verify Adzuna jobs have canonical ATS URLs in DB after run.

10. **Improve apply routing** (applicator/__init__.py)
    — Add KNOWN_LOGIN_PORTALS whitelist.
    — Skip redirect resolution for already-resolved jobs.
    — Add clear LinkedIn credential check with actionable message.

11. **Update config.yaml** with new source sections, scoring thresholds, SERPER_API_KEY reference

12. **Update requirements.txt** with new dependencies

13. **Final end-to-end test**: `python run.py --discover-only` then `python run.py --score-only` then `python run.py --apply-only`
