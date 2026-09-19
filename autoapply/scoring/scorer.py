"""
LLM-powered job scoring engine.
Evaluates how well Tamilarasan's profile matches each job description.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from rich.console import Console

from autoapply.scoring.llm_client import LLMClient
from autoapply.scoring.prompts import get_score_system_prompt, build_score_prompt
from autoapply.utils.logger import log_scoring, log_error

console = Console()

# Slightly longer than LLMClient's DEFAULT_COOLDOWN_SECONDS so a retry pass
# actually finds cooled-down keys available again instead of retrying too early.
DEFAULT_RETRY_WAIT_SECONDS = 65


@dataclass
class ScoreResult:
    """Result of scoring a job against the candidate profile."""
    score: float = 0.0
    verdict: str = "skip"  # auto_apply | review | skip
    match_reasons: list[str] = field(default_factory=list)
    skill_gaps: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    tailoring_variant: str = "balanced"  # backend | ai_ml | balanced | data_infra
    summary_hint: str = ""
    error: bool = False

    # Two-stage pre-scoring data
    tfidf_score: float = 0.0
    skill_match_score: float = 0.0
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    extracted_jd_skills: list[str] = field(default_factory=list)
    skipped_llm: bool = False  # True if TF-IDF pre-filter skipped LLM

    # Recency and seniority signals (deterministically applied post-LLM)
    recency_bonus: float = 0.0      # 0-10 pts added after LLM score
    seniority_fit: float = 1.0      # 0.5-1.2 multiplier applied after LLM score
    employment_type_ok: bool = True  # False if contract/internship when FTE preferred

    # Thresholds stored on result for context — set by score_job from config
    auto_apply_threshold: float = 75.0
    review_threshold: float = 60.0

    # Per-user context
    domain: str = "general"
    candidate_years: float = 3.0

    @property
    def should_auto_apply(self) -> bool:
        return self.score >= self.auto_apply_threshold

    @property
    def should_review(self) -> bool:
        return self.review_threshold <= self.score < self.auto_apply_threshold

    @property
    def display_score(self) -> str:
        bar = "█" * int(self.score / 10) + "░" * (10 - int(self.score / 10))
        extras = []
        if self.tfidf_score:
            extras.append(f"tfidf={self.tfidf_score:.2f}")
        if self.skill_match_score:
            extras.append(f"skill={self.skill_match_score:.2f}")
        if self.recency_bonus:
            extras.append(f"recency=+{self.recency_bonus:.0f}")
        suffix = f" [{', '.join(extras)}]" if extras else ""
        return f"[{bar}] {self.score:.0f}/100{suffix}"


def score_recency(posted_at: Optional[str]) -> float:
    """
    Calculate a recency bonus based on how recently the job was posted.

    Returns:
        0.0-10.0 bonus points:
        < 3 days  → 10.0 (very fresh)
        3-7 days  → 8.0
        7-14 days → 5.0
        14-30 days → 3.0
        30-60 days → 1.0
        > 60 days  → 0.0
        None/unknown → 3.0 (neutral)
    """
    if not posted_at:
        return 3.0  # Neutral when unknown

    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # Parse ISO date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
        clean = posted_at.strip()[:10]  # Take date portion only
        posted = datetime.strptime(clean, "%Y-%m-%d")
        days_ago = (now - posted).days

        if days_ago < 3:
            return 10.0
        elif days_ago < 7:
            return 8.0
        elif days_ago < 14:
            return 5.0
        elif days_ago < 30:
            return 3.0
        elif days_ago < 60:
            return 1.0
        else:
            return 0.0
    except Exception:
        return 3.0  # Neutral on parse error


def score_seniority_fit(seniority_level: Optional[str], candidate_years: int = 3) -> float:
    """
    Calculate a seniority fit multiplier based on how well the job's seniority
    matches the candidate's experience (3 years → mid-level).

    Returns:
        0.5-1.2 multiplier applied to encourage/penalize seniority mismatches:
        intern     → 0.6 (overqualified)
        junior     → 0.75 (slightly overqualified)
        mid        → 1.0 (perfect fit)
        senior     → 0.90 (slight stretch but acceptable)
        staff      → 0.70 (too senior for 3 yrs)
        principal  → 0.55 (significantly overleveled)
        unknown    → 1.0 (no penalty)
    """
    if not seniority_level:
        return 1.0

    level = seniority_level.lower().strip()

    multipliers = {
        "intern": 0.60,
        "junior": 0.75,
        "mid": 1.00,
        "senior": 0.90,
        "staff": 0.70,
        "principal": 0.55,
        "manager": 0.65,
        "lead": 0.85,
        "unknown": 1.00,
    }
    return multipliers.get(level, 1.0)


def build_experience_summary(master_resume: dict) -> str:
    """
    Compact experience summary for LLM scoring prompt.
    Improved: shows role, company, project name, and top bullets per project.
    """
    parts = []
    for exp in master_resume.get("experiences", []):
        company = exp.get("company", "")
        role = exp.get("role", "")
        start = exp.get("start", "")
        end = exp.get("end", "Present")
        parts.append(f"{role} at {company} ({start}–{end}):")
        for proj in exp.get("projects", []):
            proj_name = proj.get("name", "")
            if proj_name:
                parts.append(f"  [{proj_name}]")
            for bullet in proj.get("bullets", [])[:2]:  # Top 2 per project
                parts.append(f"    - {bullet[:160]}")
    return "\n".join(parts[:20])  # Cap at 20 lines


def build_resume_summary_for_scoring(master_resume: dict) -> str:
    """Get the balanced summary for scoring context."""
    variants = master_resume.get("summary_variants", {})
    return variants.get("balanced", "")


def build_candidate_meta(master_resume: dict, config: dict) -> dict:
    """Extract candidate metadata for richer scoring context (per-user aware)."""
    candidate_cfg = config.get("candidate", {})
    personal = master_resume.get("personal", {})
    meta_section = master_resume.get("meta", {})

    # Try meta.total_experience_years → config → personal → fallback 3.0
    raw_years = (
        meta_section.get("total_experience_years")
        or candidate_cfg.get("years_of_experience")
        or personal.get("years_of_experience")
        or "3"
    )
    try:
        years_float = float(str(raw_years).replace("+", "").strip())
    except (ValueError, TypeError):
        years_float = 3.0

    return {
        "current_company": candidate_cfg.get(
            "current_company",
            personal.get("current_company", ""),
        ),
        "current_title": candidate_cfg.get(
            "current_title",
            personal.get("current_title", ""),
        ),
        "years_of_experience": str(years_float),
        "years_float": years_float,
    }


def is_bangalore_job(job) -> bool:
    """Check if a job is in Bangalore/India or eligible for India applicants."""
    loc = (getattr(job, 'location', '') or '').lower()
    return (
        'bangalore' in loc or
        'bengaluru' in loc or
        'india' in loc or
        getattr(job, 'is_remote', False)
    )


def score_job(
    job_title: str,
    job_company: str,
    job_description: str,
    master_resume: dict,
    llm_client: LLMClient,
    auto_apply_threshold: float = 75.0,
    review_threshold: float = 60.0,
    location_bonus: float = 0.0,
    config: dict | None = None,
    tfidf_scorer=None,
    skill_extractor=None,
    resume_skills: set | None = None,
    tfidf_threshold: float = 0.10,
    posted_at: Optional[str] = None,
    seniority_level: Optional[str] = None,
    employment_type: Optional[str] = None,
    resume_id: str = "",
    domain: str = "general",
    candidate_years: float = 3.0,
) -> ScoreResult:
    """
    Score a single job against the candidate's master resume.
    Improvements:
    - experience_summary now injected into scoring prompt
    - candidate_meta (company, title, years) passed for richer context
    - location_bonus now explicitly instructed to LLM (+5 pts)
    - Failure score changed from 65 (noisy) to 0 with score_error status
    """
    if not job_description or len(job_description.strip()) < 50:
        return ScoreResult(
            score=0.0,
            verdict="skip",
            match_reasons=["Insufficient job description to score accurately"],
            tailoring_variant="balanced",
            error=True,
        )

    # ── Stage 1: TF-IDF cosine similarity pre-filter ─────────────────────────────
    tfidf_score = 0.5  # Default neutral when scorer not available
    if tfidf_scorer and tfidf_scorer.available:
        tfidf_score = tfidf_scorer.score(job_description)
        if tfidf_score < tfidf_threshold:
            console.print(f"  [dim]TF-IDF pre-filter: {tfidf_score:.3f} < {tfidf_threshold} — skipping LLM[/dim]")
            return ScoreResult(
                score=0.0,
                verdict="skip",
                match_reasons=[f"Low text similarity score ({tfidf_score:.3f})"],
                tailoring_variant="balanced",
                tfidf_score=tfidf_score,
                skipped_llm=True,
            )

    # ── Stage 2: Skill extraction ───────────────────────────────────────────────
    skill_match_score = 0.0
    matched_skills: list[str] = []
    missing_skills: list[str] = []
    extracted_jd_skills: list[str] = []
    if skill_extractor and resume_skills:
        extracted_jd_skills = skill_extractor.extract(job_description)
        skill_match_score, matched_skills, missing_skills = skill_extractor.match(
            job_description, resume_skills
        )

    resume_summary = build_resume_summary_for_scoring(master_resume)
    skills = master_resume.get("skills", {})
    experience_summary = build_experience_summary(master_resume)
    candidate_meta = build_candidate_meta(master_resume, config or {})

    location_hint = ""
    if location_bonus > 0:
        location_hint = "This job is in Bangalore/India or is remote-eligible. Candidate is in Bangalore."

    # ── Recency and seniority signals ─────────────────────────────────────────
    recency_bonus = score_recency(posted_at)
    seniority_multiplier = score_seniority_fit(seniority_level, candidate_years=int(candidate_years))
    employment_type_ok = True
    if employment_type and employment_type.lower() in ("contract", "internship", "part-time"):
        employment_type_ok = False

    recency_hint = ""
    if posted_at:
        try:
            from datetime import datetime
            days_ago = (datetime.now() - datetime.strptime(posted_at[:10], "%Y-%m-%d")).days
            if days_ago < 7:
                recency_hint = f"Job posted {days_ago} day(s) ago — very fresh listing."
            elif days_ago < 30:
                recency_hint = f"Job posted {days_ago} days ago — recent."
            elif days_ago > 60:
                recency_hint = f"Job posted {days_ago} days ago — older listing, may be filled."
        except Exception:
            pass

    seniority_hint = ""
    if seniority_level and seniority_level not in ("mid", "unknown"):
        if seniority_level in ("intern", "junior"):
            seniority_hint = f"Role is {seniority_level}-level — candidate ({candidate_years:.0f} yrs exp) may be overqualified."
        elif seniority_level in ("staff", "principal", "manager"):
            seniority_hint = f"Role is {seniority_level}-level — may require more experience than candidate has ({candidate_years:.0f} yrs)."

    employment_type_hint = ""
    if not employment_type_ok:
        employment_type_hint = f"Role type is '{employment_type}' — candidate prefers full-time employment."

    prompt = build_score_prompt(
        resume_summary=resume_summary,
        skills=skills,
        experience_summary=experience_summary,
        jd=job_description,
        location_hint=location_hint,
        candidate_meta=candidate_meta,
        matched_skills=matched_skills,
        missing_skills=missing_skills,
        recency_hint=recency_hint,
        seniority_hint=seniority_hint,
        employment_type_hint=employment_type_hint,
        domain=domain,
        candidate_years=candidate_years,
    )

    # ── LLM Cache check (avoids re-scoring same JD) ───────────────────────────
    from autoapply.scoring.cache import cache_get, cache_set
    from autoapply.scoring.prompts import _build_skills_line
    skills_str = _build_skills_line(skills)
    cache_result = cache_get(
        jd=job_description,
        resume_summary=resume_summary,
        skills_str=skills_str,
        resume_id=resume_id,
        domain=domain,
    )
    if cache_result:
        console.print(f"  [dim]Cache hit — skipping LLM call[/dim]")
        result = cache_result
    else:
        # Use domain-appropriate system prompt
        system_prompt = get_score_system_prompt(domain)
        result = llm_client.chat_json(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=system_prompt,
            max_tokens=768,
            temperature=0.2,
        )
        if result:
            cache_set(
                jd=job_description,
                resume_summary=resume_summary,
                skills_str=skills_str,
                result=result,
                resume_id=resume_id,
                domain=domain,
            )

    if not result:
        console.print(f"[yellow]  Score failed for:[/yellow] {job_title} @ {job_company}")
        return ScoreResult(error=True, score=0.0, verdict="skip", tailoring_variant="balanced", tfidf_score=tfidf_score)

    # Parse and validate
    try:
        raw_llm_score = float(result.get("score", 0))
        raw_llm_score = max(0.0, min(100.0, raw_llm_score))

        # ── Post-LLM deterministic adjustments ───────────────────────────────
        # These were previously computed but silently dropped. Now applied:
        score = raw_llm_score

        # 1. Seniority multiplier (e.g. 0.75 for junior role, 0.90 for senior)
        score = score * seniority_multiplier

        # 2. Recency bonus (additive, capped at 100)
        score = min(100.0, score + recency_bonus)

        # 3. Employment type penalty (20% reduction for contract/internship)
        if not employment_type_ok:
            score = score * 0.80

        score = max(0.0, min(100.0, score))

        # Determine verdict based on thresholds (using adjusted score)
        if score >= auto_apply_threshold:
            verdict = "auto_apply"
        elif score >= review_threshold:
            verdict = "review"
        else:
            verdict = "skip"

        # Validate tailoring variant
        valid_variants = {"backend", "ai_ml", "balanced", "data_infra", "embedded", "testing"}
        raw_variant = result.get("tailoring_variant", "balanced")
        tailoring_variant = raw_variant if raw_variant in valid_variants else "balanced"

        if seniority_multiplier != 1.0 or recency_bonus > 0 or not employment_type_ok:
            console.print(
                f"  [dim]Post-LLM: raw={raw_llm_score:.0f} × seniority={seniority_multiplier:.2f} "
                f"+ recency={recency_bonus:.0f}"
                + (f" × emp_penalty=0.80" if not employment_type_ok else "")
                + f" → final={score:.0f}[/dim]"
            )

        return ScoreResult(
            score=score,
            verdict=verdict,
            match_reasons=result.get("match_reasons", [])[:5],
            skill_gaps=result.get("skill_gaps", [])[:5],
            red_flags=result.get("red_flags", [])[:3],
            tailoring_variant=tailoring_variant,
            summary_hint=result.get("summary_hint", ""),
            tfidf_score=tfidf_score,
            skill_match_score=skill_match_score,
            matched_skills=matched_skills,
            missing_skills=missing_skills,
            extracted_jd_skills=extracted_jd_skills,
            recency_bonus=recency_bonus,
            seniority_fit=seniority_multiplier,
            employment_type_ok=employment_type_ok,
            domain=domain,
            candidate_years=candidate_years,
        )

    except Exception as e:
        console.print(f"[yellow]  Score parse error:[/yellow] {e}")
        return ScoreResult(error=True, score=0.0, verdict="skip", tailoring_variant="balanced", tfidf_score=tfidf_score)


def score_jobs_batch(
    jobs: list,  # List of db Job objects with .description, .title, .company
    master_resume: dict,
    llm_client: LLMClient,
    config: dict,
) -> dict[int, ScoreResult]:
    """
    Score a batch of jobs. Returns dict of {job_id: ScoreResult}.
    Provides rich progress output.
    """
    from autoapply.tracker.db import update_job_score

    scoring_cfg = config.get("scoring", {})
    auto_threshold = scoring_cfg.get("auto_apply_threshold", 75)
    review_threshold = scoring_cfg.get("review_threshold", 60)

    results: dict[int, ScoreResult] = {}
    total = len(jobs)

    console.print(f"\n[bold blue]Scoring {total} jobs...[/bold blue]")

    # Build TF-IDF scorer and skill extractor ONCE for the whole batch
    tfidf_scorer = None
    skill_extractor = None
    resume_skills: set = set()
    tfidf_threshold = scoring_cfg.get("tfidf_threshold", 0.10)

    # Derive per-user domain and years for prompt calibration
    candidate_meta_derived = build_candidate_meta(master_resume, config)
    domain = master_resume.get("meta", {}).get("domain", "backend")
    candidate_years = candidate_meta_derived.get("years_float", 3.0)

    try:
        from autoapply.scoring.tfidf_scorer import TFIDFScorer, build_resume_text
        from autoapply.scoring.skill_extractor import SkillExtractor
        resume_text = build_resume_text(master_resume)
        tfidf_scorer = TFIDFScorer(resume_text)
        skill_extractor = SkillExtractor()
        resume_skills = SkillExtractor.build_resume_skill_profile(master_resume)
        if tfidf_scorer.available:
            console.print(f"  [dim]TF-IDF pre-scorer ready (threshold={tfidf_threshold}) | "
                          f"Domain: {domain} | {candidate_years:.0f} yrs exp | "
                          f"Resume skill profile: {len(resume_skills)} skills[/dim]")
    except Exception as e:
        console.print(f"  [dim]Two-stage scoring init error (non-fatal): {e}[/dim]")

    max_workers = scoring_cfg.get("max_concurrent_workers", 4)
    console.print(f"  [dim]Scoring with up to {max_workers} concurrent workers (parallel per key)[/dim]")

    def _score_one(job):
        loc_bonus = 5.0 if is_bangalore_job(job) else 0.0
        score_result = score_job(
            job_title=job.title,
            job_company=job.company,
            job_description=job.description or "",
            master_resume=master_resume,
            llm_client=llm_client,
            auto_apply_threshold=auto_threshold,
            review_threshold=review_threshold,
            location_bonus=loc_bonus,
            config=config,
            tfidf_scorer=tfidf_scorer,
            skill_extractor=skill_extractor,
            resume_skills=resume_skills,
            tfidf_threshold=tfidf_threshold,
            posted_at=getattr(job, "posted_at", None),
            seniority_level=getattr(job, "seniority_level", None),
            employment_type=getattr(job, "employment_type", None),
            domain=domain,
            candidate_years=candidate_years,
        )
        return job, score_result

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = [executor.submit(_score_one, job) for job in jobs]
        for done_count, future in enumerate(as_completed(futures), 1):
            job, score_result = future.result()
            location_flag = " 🇮🇳" if is_bangalore_job(job) else ""

            verdict_color = {
                "auto_apply": "green",
                "review": "yellow",
                "skip": "red",
            }.get(score_result.verdict, "white")

            console.print(
                f"  [{done_count}/{total}] [cyan]{job.title}[/cyan] @ [magenta]{job.company}[/magenta]  "
                f"{score_result.display_score} "
                f"[{verdict_color}]{score_result.verdict.upper()}[/{verdict_color}]{location_flag}"
            )

            if score_result.skill_gaps:
                console.print(f"    [dim]Gaps: {', '.join(score_result.skill_gaps[:3])}[/dim]")

            # Save to database
            if not score_result.error:
                update_job_score(
                    job_id=job.id,
                    score=score_result.score,
                    reasoning=score_result.summary_hint,
                    skill_gaps=score_result.skill_gaps,
                    tailoring_variant=score_result.tailoring_variant,
                    red_flags=score_result.red_flags,
                    tfidf_score=score_result.tfidf_score,
                    skill_match_score=score_result.skill_match_score,
                )

            results[job.id] = score_result

    # Retry pass: jobs that failed due to transient rate-limiting/API errors (not
    # real scoring failures) get one more sequential attempt once cooldowns from
    # the concurrent pass have likely expired, instead of being permanently
    # mislabeled as low-fit skips.
    failed_jobs = [job for job in jobs if results[job.id].error]
    if failed_jobs:
        import time as _time
        console.print(f"\n[dim]Retrying {len(failed_jobs)} jobs that failed due to API errors...[/dim]")
        _time.sleep(DEFAULT_RETRY_WAIT_SECONDS)
        for job in failed_jobs:
            loc_bonus = 5.0 if is_bangalore_job(job) else 0.0
            retry_result = score_job(
                job_title=job.title,
                job_company=job.company,
                job_description=job.description or "",
                master_resume=master_resume,
                llm_client=llm_client,
                auto_apply_threshold=auto_threshold,
                review_threshold=review_threshold,
                location_bonus=loc_bonus,
                config=config,
                tfidf_scorer=tfidf_scorer,
                skill_extractor=skill_extractor,
                resume_skills=resume_skills,
                tfidf_threshold=tfidf_threshold,
                posted_at=getattr(job, "posted_at", None),
                seniority_level=getattr(job, "seniority_level", None),
                employment_type=getattr(job, "employment_type", None),
                domain=domain,
                candidate_years=candidate_years,
            )
            if not retry_result.error:
                update_job_score(
                    job_id=job.id,
                    score=retry_result.score,
                    reasoning=retry_result.summary_hint,
                    skill_gaps=retry_result.skill_gaps,
                    tailoring_variant=retry_result.tailoring_variant,
                    red_flags=retry_result.red_flags,
                    tfidf_score=retry_result.tfidf_score,
                    skill_match_score=retry_result.skill_match_score,
                )
                console.print(f"  [green]Retry succeeded:[/green] {job.title} @ {job.company} -> {retry_result.display_score}")
            results[job.id] = retry_result

    # Summary
    auto_count = sum(1 for r in results.values() if r.verdict == "auto_apply")
    review_count = sum(1 for r in results.values() if r.verdict == "review")
    skip_count = sum(1 for r in results.values() if r.verdict == "skip")

    tfidf_skipped = sum(1 for r in results.values() if r.skipped_llm)
    llm_called = total - tfidf_skipped

    console.print(f"\n[bold]Scoring complete:[/bold]")
    console.print(f"  [green]Auto-apply:[/green] {auto_count}")
    console.print(f"  [yellow]Review:[/yellow] {review_count}")
    console.print(f"  [red]Skip:[/red] {skip_count}")
    if tfidf_skipped:
        console.print(f"  [dim]TF-IDF pre-filter saved {tfidf_skipped}/{total} LLM calls ({tfidf_skipped*100//total}%)[/dim]")


    return results


def score_jobs_batch_for_resume(
    resume_id: int,
    resume_json: dict,
    search_config: dict,
    llm_client: LLMClient,
    config: dict,
    limit: int | None = None,
    user_profile: dict | None = None,
) -> dict[int, ScoreResult]:
    """
    Score the shared job pool against one additional uploaded resume.
    Uses per-user domain, thresholds, and TF-IDF threshold from user_profile.
    """
    from autoapply.tracker.db import get_jobs_pending_scoring_for_resume, update_job_score_for_resume

    scoring_cfg = config.get("scoring", {})
    profile = user_profile or {}

    # Per-user thresholds (from DB profile, then search_config, then global config)
    auto_threshold = (profile.get("auto_apply_threshold")
                      or search_config.get("auto_apply_threshold")
                      or scoring_cfg.get("auto_apply_threshold", 68))
    review_threshold_val = (profile.get("review_threshold")
                             or search_config.get("review_threshold")
                             or scoring_cfg.get("review_threshold", 52))
    tfidf_threshold = (profile.get("tfidf_threshold")
                       or scoring_cfg.get("tfidf_threshold", 0.10))
    jobs_per_run = limit or scoring_cfg.get("jobs_per_run", 150)

    # Domain and candidate years from profile
    domain = profile.get("domain") or resume_json.get("meta", {}).get("domain", "general")
    candidate_meta_derived = build_candidate_meta(resume_json, config)
    candidate_years = profile.get("years_experience") or candidate_meta_derived.get("years_float", 3.0)

    # Domain-aware title keyword pre-filter (avoids scoring irrelevant jobs)
    domain_title_keywords = profile.get("strong_keywords", [])[:15] if domain != "general" else None

    jobs = get_jobs_pending_scoring_for_resume(
        resume_id, limit=jobs_per_run,
        title_keywords=domain_title_keywords,
    )

    # Per-resume company exclusion filter
    exclude_companies = {c.lower() for c in (profile.get("exclude_companies") or search_config.get("exclude_companies", []))}
    if exclude_companies:
        jobs = [j for j in jobs if (j.company or "").lower() not in exclude_companies]

    name = resume_json.get("personal", {}).get("name", f"resume#{resume_id}")
    console.print(
        f"\n[bold blue]Scoring {len(jobs)} jobs for '{name}' "
        f"(domain={domain}, {candidate_years:.0f} yrs, "
        f"auto≥{auto_threshold}, review≥{review_threshold_val})[/bold blue]"
    )

    if not jobs:
        return {}

    tfidf_scorer = None
    skill_extractor = None
    resume_skills: set = set()

    try:
        from autoapply.scoring.tfidf_scorer import TFIDFScorer, build_resume_text
        from autoapply.scoring.skill_extractor import SkillExtractor
        resume_text = build_resume_text(resume_json)
        tfidf_scorer = TFIDFScorer(resume_text)
        skill_extractor = SkillExtractor()
        resume_skills = SkillExtractor.build_resume_skill_profile(resume_json)
        if tfidf_scorer.available:
            console.print(f"  [dim]TF-IDF threshold={tfidf_threshold} | Skills profile: {len(resume_skills)} skills[/dim]")
    except Exception as e:
        console.print(f"  [dim]Two-stage scoring init error (non-fatal): {e}[/dim]")

    max_workers = scoring_cfg.get("max_concurrent_workers", 4)
    results: dict[int, ScoreResult] = {}

    def _score_one(job):
        loc_bonus = 5.0 if is_bangalore_job(job) else 0.0
        return job, score_job(
            job_title=job.title,
            job_company=job.company,
            job_description=job.description or "",
            master_resume=resume_json,
            llm_client=llm_client,
            auto_apply_threshold=auto_threshold,
            review_threshold=review_threshold_val,
            location_bonus=loc_bonus,
            config=config,
            tfidf_scorer=tfidf_scorer,
            skill_extractor=skill_extractor,
            resume_skills=resume_skills,
            tfidf_threshold=tfidf_threshold,
            posted_at=getattr(job, "posted_at", None),
            seniority_level=getattr(job, "seniority_level", None),
            employment_type=getattr(job, "employment_type", None),
            resume_id=str(resume_id),
            domain=domain,
            candidate_years=float(candidate_years),
        )

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = [executor.submit(_score_one, job) for job in jobs]
        for future in as_completed(futures):
            job, score_result = future.result()
            if not score_result.error:
                update_job_score_for_resume(
                    job_id=job.id,
                    resume_id=resume_id,
                    score=score_result.score,
                    reasoning=score_result.summary_hint,
                    skill_gaps=score_result.skill_gaps,
                    tailoring_variant=score_result.tailoring_variant,
                    red_flags=score_result.red_flags,
                    tfidf_score=score_result.tfidf_score,
                    skill_match_score=score_result.skill_match_score,
                    verdict=score_result.verdict,
                    adjusted_score=score_result.score,
                    recency_bonus_applied=score_result.recency_bonus,
                    seniority_multiplier_applied=score_result.seniority_fit,
                )
            results[job.id] = score_result

    # Retry pass: jobs that failed due to transient rate-limiting/API errors
    failed_jobs = [job for job in jobs if results[job.id].error]
    if failed_jobs:
        import time as _time
        console.print(f"  [dim]Retrying {len(failed_jobs)} jobs that failed due to API errors...[/dim]")
        _time.sleep(DEFAULT_RETRY_WAIT_SECONDS)
        for job in failed_jobs:
            loc_bonus = 5.0 if is_bangalore_job(job) else 0.0
            retry_result = score_job(
                job_title=job.title,
                job_company=job.company,
                job_description=job.description or "",
                master_resume=resume_json,
                llm_client=llm_client,
                auto_apply_threshold=auto_threshold,
                review_threshold=review_threshold_val,
                location_bonus=loc_bonus,
                config=config,
                tfidf_scorer=tfidf_scorer,
                skill_extractor=skill_extractor,
                resume_skills=resume_skills,
                tfidf_threshold=tfidf_threshold,
                posted_at=getattr(job, "posted_at", None),
                seniority_level=getattr(job, "seniority_level", None),
                employment_type=getattr(job, "employment_type", None),
                resume_id=str(resume_id),
                domain=domain,
                candidate_years=float(candidate_years),
            )
            if not retry_result.error:
                update_job_score_for_resume(
                    job_id=job.id,
                    resume_id=resume_id,
                    score=retry_result.score,
                    reasoning=retry_result.summary_hint,
                    skill_gaps=retry_result.skill_gaps,
                    tailoring_variant=retry_result.tailoring_variant,
                    red_flags=retry_result.red_flags,
                    tfidf_score=retry_result.tfidf_score,
                    skill_match_score=retry_result.skill_match_score,
                    verdict=retry_result.verdict,
                    adjusted_score=retry_result.score,
                    recency_bonus_applied=retry_result.recency_bonus,
                    seniority_multiplier_applied=retry_result.seniority_fit,
                )
            results[job.id] = retry_result

    auto_count = sum(1 for r in results.values() if r.verdict == "auto_apply")
    review_count = sum(1 for r in results.values() if r.verdict == "review")
    skip_count = len(results) - auto_count - review_count
    console.print(
        f"  [green]Auto-apply:[/green] {auto_count}  "
        f"[yellow]Review:[/yellow] {review_count}  "
        f"[red]Skip:[/red] {skip_count}"
    )

    return results
