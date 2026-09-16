# Implementation Plan

[Overview]
Maximize AutoAppy job discovery, scoring quality, and dashboard UX to increase relevant matches and make review-to-apply seamless.

Four focus areas: (1) Discovery Expansion: Cutshort.io scraper, fix Lever/SR empty descriptions, enable Wellfound, wire URL resolution. (2) Richer Scoring: recency bonus, seniority fit, employment type signals. Updated rubric: Skill(35)+Experience(25)+Domain(20)+Recency(10)+Location(10). (3) Metadata Population at discovery time: seniority_level, employment_type, skills_required for every job. (4) Dashboard: clickable URL columns, dual View+Apply buttons, score breakdown, ATS filter.

[Types]
Extended scoring and metadata types.

Extended ScoreResult (scoring/scorer.py):
- recency_bonus: float = 0.0  # 0-10 pts based on posting age
- seniority_fit: float = 1.0  # 0.5-1.2 multiplier
- employment_type_ok: bool = True  # False if contract when FTE preferred

RawJob new method (discovery/base.py):
- populate_metadata(skill_extractor=None) -> None
  Inplace: seniority from title, employment_type from description regex, skills_required from SkillExtractor

Bug fix: RawJob.to_db_dict() must map self.seniority -> key "seniority_level" (currently broken, seniority_level always NULL in DB)

[Files]
Files to create and modify.

NEW FILE:
- autoapply/discovery/cutshort_scraper.py: Cutshort.io public API scraper, India tech jobs, populates posted_at/employment_type/skills_required

MODIFIED FILES:
- autoapply/discovery/base.py: Fix to_db_dict() seniority key bug; add populate_metadata() method; add employment_type regex detection
- autoapply/discovery/__init__.py: After upsert call resolve_aggregator_urls() (never called currently); call populate_metadata_batch(); add cutshort source; increase workers to 12
- autoapply/discovery/ats_direct.py: Fix Lever empty descriptions - add _fetch_lever_description() helper; call when description empty (fixes 39 jobs)
- autoapply/discovery/smartrecruiters_discovery.py: Fix 15 SR empty descriptions by fetching job detail from SR API
- autoapply/scoring/scorer.py: Add score_recency() and score_seniority_fit(); add posted_at/seniority_level params to score_job(); update score_jobs_batch()
- autoapply/scoring/prompts.py: Add recency_hint/seniority_hint/employment_type_hint params to build_score_prompt(); update rubric
- autoapply/dashboard/app.py: Add Apply URL/Posted At/Seniority/TF-IDF/Skill Match to load_jobs_df(); use LinkColumn for URLs; dual link buttons per expander; ATS filter; score breakdown
- run.py: Call resolve_aggregator_urls() after discovery; add --resolve-urls/--populate-metadata/--rescore CLI flags
- config.yaml: Enable wellfound; add cutshort section; add scoring/discovery flags

[Functions]
New and modified functions.

NEW FUNCTIONS:
- cutshort_scraper.fetch_cutshort_jobs(roles, locations, config) -> List[RawJob]: Cutshort.io scraper
- scorer.score_recency(posted_at: str | None) -> float: <7d=10, 7-30d=5, 30-60d=2, >60d=0, None=3
- scorer.score_seniority_fit(seniority_level, candidate_years=3) -> float: intern/junior=0.7, mid=1.0, senior=0.9, staff=0.6
- base.RawJob.populate_metadata(skill_extractor=None): inplace metadata extraction
- __init__.populate_metadata_batch(jobs): bulk DB update after discovery
- ats_direct._fetch_lever_description(slug, job_id) -> str: fetch + clean Lever job description

MODIFIED FUNCTIONS:
- base.RawJob.to_db_dict(): Fix seniority_level key; ensure employment_type and skills_required populated
- __init__.discover_jobs(): call resolve_aggregator_urls() + populate_metadata_batch() after upsert
- scorer.score_job(): add posted_at/seniority_level params; compute recency_bonus and seniority_multiplier
- scorer.score_jobs_batch(): pass job.posted_at and job.seniority_level to score_job()
- prompts.build_score_prompt(): add recency_hint/seniority_hint/employment_type_hint params; update rubric
- dashboard.load_jobs_df(): add Apply URL/Posted At/Seniority/TF-IDF/Skill Match columns
- dashboard All Jobs table: use st.column_config.LinkColumn for URL columns
- dashboard job expanders: show View Posting (job_url) AND Apply Direct (apply_url) buttons; show score breakdown
- run.run_discovery(): call resolve_aggregator_urls() and populate_metadata_batch() after save
- run.main(): add --resolve-urls, --populate-metadata, --rescore args

[Classes]
Minimal class changes.

MODIFIED CLASSES:
- ScoreResult (scoring/scorer.py): Add recency_bonus:float=0.0, seniority_fit:float=1.0, employment_type_ok:bool=True
  Update display_score: show [score=75 tfidf=0.18 skill=0.6 recency=+5]
- RawJob (discovery/base.py): Add populate_metadata() method; fix to_db_dict() seniority key

[Dependencies]
No new dependencies. All already installed:
- scikit-learn>=1.4.2, flashtext>=2.7, requests>=2.31.0, streamlit>=1.35.0 (LinkColumn available since 1.28)
Cutshort.io uses free public API, no auth, 1s polite delay.

[Testing]
Key validation tests.

1. Metadata Population Test: After --populate-metadata, verify >90% of 1755 jobs have seniority_level/employment_type populated
2. Recency Scoring Unit Test: score_recency(None)==3.0, score_recency("2026-09-14")>=8.0, score_recency("2026-06-01")==0.0
3. URL Resolution Test: After --resolve-urls, redirect_resolved=True count >100 for Adzuna/Indeed jobs
4. Lever Fix Test: Lever jobs with empty description == 0 (was 39)
5. SR Fix Test: SmartRecruiters jobs with empty description == 0 (was 15)
6. Dashboard URL Test: All Jobs table has clickable URL columns; expanders have dual link buttons
7. Cutshort Discovery Test: fetch_cutshort_jobs returns >0 India tech jobs with posted_at populated
8. Score Quality Test: After --score-only on 20 jobs, recency_bonus in reasoning, ~50% TF-IDF skipped

[Implementation Order]
Critical bug fixes first, then features, then UX.

1. Fix RawJob.to_db_dict() seniority key bug (discovery/base.py) - 1-line fix, highest priority
2. Add RawJob.populate_metadata() method (discovery/base.py)
3. Fix Lever empty descriptions - add _fetch_lever_description() (discovery/ats_direct.py)
4. Fix SmartRecruiters empty descriptions (discovery/smartrecruiters_discovery.py)
5. Wire URL resolution into discover_jobs() (discovery/__init__.py, run.py) + --resolve-urls CLI
6. Add populate_metadata_batch() and integrate after discovery (discovery/__init__.py, run.py)
7. Add score_recency() + score_seniority_fit() and wire into score_job() (scoring/scorer.py, prompts.py)
8. Add Cutshort scraper (discovery/cutshort_scraper.py) + integrate in discover_jobs() + config.yaml
9. Update config.yaml: enable wellfound, add cutshort, add flags
10. Dashboard improvements (dashboard/app.py): URLs, dual buttons, score breakdown, ATS filter
11. Add CLI flags --resolve-urls/--populate-metadata/--rescore (run.py)
12. Back-fill metadata for existing 1755 jobs: python run.py --populate-metadata
13. Final e2e test: discover-only -> populate-metadata -> score-only -> dashboard review
