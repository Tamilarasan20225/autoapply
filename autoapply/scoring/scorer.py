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
from autoapply.scoring.prompts import (
    PROMPT_VERSION,
    SCORE_V2_REQUIRED_KEYS,
    build_score_prompt,
    build_score_prompt_v2,
    get_score_system_prompt,
    get_score_v2_system_prompt,
)
from autoapply.scoring import gates as G
from autoapply.utils.logger import log_scoring, log_error

console = Console()

# Slightly longer than LLMClient's DEFAULT_COOLDOWN_SECONDS so a retry pass
# actually finds cooled-down keys available again instead of retrying too early.
DEFAULT_RETRY_WAIT_SECONDS = 65

SCORE_VERSION = 2

# LLM sub-score caps. These sum to 100 and are the ONLY thing the LLM judges;
# recency/geo/seniority/employment are deterministic and applied once by the scorer.
_SUBSCORE_CAPS = {
    "skill_score": 35,
    "experience_score": 25,
    "domain_score": 20,
    "stack_depth_score": 10,
    "growth_score": 10,
}


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

    # ── v2 audit trail ───────────────────────────────────────────────────────
    score_version: int = SCORE_VERSION
    raw_llm_score: float = 0.0
    score_model: Optional[str] = None
    score_prompt_version: str = PROMPT_VERSION
    score_breakdown: dict = field(default_factory=dict)
    hard_gate_failures: list[str] = field(default_factory=list)
    jd_required_years: Optional[float] = None
    jd_geo_scope: str = "unknown"
    description_quality: str = "full"
    confidence: float = 1.0
    needs_enrichment: bool = False
    gated: bool = False     # deterministically rejected, not an LLM failure

    @property
    def db_status(self) -> str:
        if self.needs_enrichment:
            return "needs_enrichment"
        if self.error:
            return "score_error"
        if self.gated:
            return "gated"
        return "scored"

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


def score_seniority_fit(seniority_level: Optional[str], candidate_years: float = 3.0) -> float:
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


def _verdict_for(score: float, auto_apply_threshold: float, review_threshold: float) -> str:
    if score >= auto_apply_threshold:
        return "auto_apply"
    if score >= review_threshold:
        return "review"
    return "skip"


def _coerce_subscore(raw, cap: int) -> Optional[float]:
    """Return a sub-score clamped to its cap, or None if it isn't a number."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(float(cap), value))


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
    job_location: Optional[str] = None,
    is_remote: bool = False,
    salary_min: Optional[float] = None,
    salary_max: Optional[float] = None,
    salary_currency: Optional[str] = None,
    pin_model: Optional[str] = None,
) -> ScoreResult:
    """
    Scoring v2 — deterministic gates, then a single LLM fit judgement.

    Each signal is applied exactly once. The LLM judges skill/experience/domain
    fit only; recency, geography, seniority and employment type are decided in
    gates.py and combined here. Every component is recorded in score_breakdown.
    """
    scoring_cfg = (config or {}).get("scoring", {})

    gate = G.evaluate_gates(
        title=job_title,
        description=job_description,
        location=job_location,
        is_remote=is_remote,
        posted_at=posted_at,
        employment_type=employment_type,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        candidate_years=float(candidate_years) if candidate_years is not None else None,
        yoe_tolerance=float(scoring_cfg.get("yoe_tolerance_years", 2.0)),
        min_salary_inr=scoring_cfg.get("min_salary_inr"),
        allow_unknown_geo=bool(scoring_cfg.get("allow_unknown_geo", True)),
    )

    def _gated(reasons: list[str], *, needs_enrichment: bool = False) -> ScoreResult:
        return ScoreResult(
            score=0.0,
            verdict="skip",
            match_reasons=reasons,
            tailoring_variant="balanced",
            gated=not needs_enrichment,
            needs_enrichment=needs_enrichment,
            hard_gate_failures=gate.failures,
            jd_required_years=gate.required_years,
            jd_geo_scope=gate.geo_scope,
            description_quality=gate.description_quality,
            score_breakdown=gate.as_dict(),
            recency_bonus=gate.recency_bonus,
            seniority_fit=gate.seniority_multiplier,
            employment_type_ok=gate.full_time,
            domain=domain,
            candidate_years=candidate_years,
            auto_apply_threshold=auto_apply_threshold,
            review_threshold=review_threshold,
        )

    if gate.needs_enrichment:
        return _gated(
            [f"Description too thin to score ({gate.description_quality})"],
            needs_enrichment=True,
        )
    if not gate.passed:
        console.print(f"  [dim]Gated: {', '.join(gate.failures)}[/dim]")
        return _gated([f"Hard gate: {f}" for f in gate.failures])

    # ── Stage 1: TF-IDF triage (cost saver only, never a final score) ────────
    tfidf_score = None
    if tfidf_scorer and tfidf_scorer.available:
        tfidf_score = tfidf_scorer.score(job_description)
        # Only gate on TF-IDF when the IDF fit came from a real corpus.
        if tfidf_scorer.well_fitted and tfidf_score is not None and tfidf_score < tfidf_threshold:
            console.print(f"  [dim]TF-IDF triage: {tfidf_score:.3f} < {tfidf_threshold} — skipping LLM[/dim]")
            result = _gated([f"Low text similarity ({tfidf_score:.3f})"])
            result.tfidf_score = tfidf_score
            result.skipped_llm = True
            result.hard_gate_failures = gate.failures + [f"tfidf_{tfidf_score:.3f}_below_{tfidf_threshold}"]
            return result

    # ── Stage 2: Skill extraction ───────────────────────────────────────────
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

    jd_budget = int(scoring_cfg.get("jd_char_budget", 6000))
    prompt = build_score_prompt_v2(
        job_title=job_title,
        job_company=job_company,
        job_location=job_location or "",
        jd=job_description,
        resume_summary=resume_summary,
        skills=skills,
        experience_summary=experience_summary,
        candidate_meta=candidate_meta,
        candidate_years=candidate_years,
        matched_skills=matched_skills,
        missing_skills=missing_skills,
        domain=domain,
        jd_char_budget=jd_budget,
    )

    model_used = pin_model or llm_client.primary_model()

    # ── LLM cache (keyed on model + prompt version, so a weaker model's score
    #    is never replayed for a stronger one) ────────────────────────────────
    from autoapply.scoring.cache import cache_get, cache_set
    cache_kwargs = dict(
        jd=job_description,
        resume_summary=resume_summary,
        skills_str=",".join(sorted(str(s) for group in skills.values()
                                   if isinstance(group, list) for s in group)),
        resume_id=resume_id,
        domain=domain,
        model=model_used or "",
        prompt_version=PROMPT_VERSION,
        candidate_years=candidate_years,
    )
    result = cache_get(**cache_kwargs)
    if result:
        console.print("  [dim]Cache hit — skipping LLM call[/dim]")
    else:
        llm_cfg = (config or {}).get("llm", {})
        result = llm_client.chat_json(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=get_score_v2_system_prompt(domain),
            max_tokens=int(llm_cfg.get("max_tokens_scoring", 1024)),
            temperature=0.0,
            pin_model=pin_model,
            required_keys=SCORE_V2_REQUIRED_KEYS,
        )
        if result:
            model_used = llm_client.last_model or model_used
            cache_set(result=result, **cache_kwargs)

    if not result:
        console.print(f"[yellow]  Score failed for:[/yellow] {job_title} @ {job_company}")
        return ScoreResult(
            error=True, score=0.0, verdict="skip", tailoring_variant="balanced",
            tfidf_score=tfidf_score or 0.0, score_breakdown=gate.as_dict(),
            hard_gate_failures=gate.failures, jd_required_years=gate.required_years,
            jd_geo_scope=gate.geo_scope, description_quality=gate.description_quality,
            domain=domain, candidate_years=candidate_years,
        )

    subscores: dict[str, float] = {}
    for key, cap in _SUBSCORE_CAPS.items():
        value = _coerce_subscore(result.get(key), cap)
        if value is None:
            # A missing or non-numeric sub-score is a model failure, not a zero.
            console.print(f"[yellow]  Invalid '{key}' in LLM response — treating as error[/yellow]")
            return ScoreResult(
                error=True, score=0.0, verdict="skip", tailoring_variant="balanced",
                tfidf_score=tfidf_score or 0.0, score_breakdown=gate.as_dict(),
                domain=domain, candidate_years=candidate_years,
            )
        subscores[key] = value

    raw_llm_score = sum(subscores.values())

    confidence = _coerce_subscore(result.get("confidence", 1.0), 1) or 0.0
    confidence = max(0.0, min(1.0, confidence))

    # Thin-but-scoreable descriptions shrink toward the review band rather than
    # being trusted at full strength.
    confidence_damping = 1.0
    if gate.description_quality == G.QUALITY_PARTIAL or confidence < 0.5:
        confidence_damping = 0.85 + 0.15 * confidence

    employment_multiplier = 1.0 if gate.full_time else 0.80
    geo_multiplier = 1.0 if gate.geo_scope == G.GEO_INDIA_OK else 0.92

    score = raw_llm_score
    score *= gate.seniority_multiplier
    score *= employment_multiplier
    score *= geo_multiplier
    score *= confidence_damping
    score += gate.recency_bonus
    # Objective keyword overlap as a small tie-breaker — v1 computed it and never used it.
    score += min(4.0, skill_match_score * 4.0)
    score = max(0.0, min(100.0, score))

    verdict = _verdict_for(score, auto_apply_threshold, review_threshold)

    valid_variants = {"backend", "ai_ml", "balanced", "data_infra", "embedded", "testing"}
    raw_variant = result.get("tailoring_variant", "balanced")
    tailoring_variant = raw_variant if raw_variant in valid_variants else "balanced"

    breakdown = {
        "subscores": {k: round(v, 1) for k, v in subscores.items()},
        "raw_llm_score": round(raw_llm_score, 1),
        "confidence": round(confidence, 2),
        "confidence_damping": round(confidence_damping, 3),
        "seniority_multiplier": round(gate.seniority_multiplier, 3),
        "employment_multiplier": employment_multiplier,
        "geo_multiplier": geo_multiplier,
        "recency_bonus": round(gate.recency_bonus, 2),
        "skill_overlap_bonus": round(min(4.0, skill_match_score * 4.0), 2),
        "final_score": round(score, 1),
        "gates": gate.as_dict(),
        "model": model_used,
        "prompt_version": PROMPT_VERSION,
    }

    return ScoreResult(
        score=score,
        verdict=verdict,
        match_reasons=[str(r) for r in result.get("match_reasons", [])][:5],
        skill_gaps=[str(g) for g in result.get("skill_gaps", [])][:5],
        red_flags=[str(f) for f in result.get("red_flags", [])][:3],
        tailoring_variant=tailoring_variant,
        summary_hint=str(result.get("summary_hint", "")),
        tfidf_score=tfidf_score or 0.0,
        skill_match_score=skill_match_score,
        matched_skills=matched_skills,
        missing_skills=missing_skills,
        extracted_jd_skills=extracted_jd_skills,
        recency_bonus=gate.recency_bonus,
        seniority_fit=gate.seniority_multiplier,
        employment_type_ok=gate.full_time,
        domain=domain,
        candidate_years=candidate_years,
        auto_apply_threshold=auto_apply_threshold,
        review_threshold=review_threshold,
        raw_llm_score=raw_llm_score,
        score_model=model_used,
        score_breakdown=breakdown,
        hard_gate_failures=gate.failures,
        jd_required_years=gate.required_years,
        jd_geo_scope=gate.geo_scope,
        description_quality=gate.description_quality,
        confidence=confidence,
    )


def _persist_score(job, result: "ScoreResult") -> None:
    """Write a score plus its full audit trail. Gated/enrichment rows are
    recorded distinctly so nothing is silently indistinguishable from a real 0."""
    from autoapply.tracker.db import update_job_score

    update_job_score(
        job_id=job.id,
        score=result.score,
        reasoning=result.summary_hint or "; ".join(result.match_reasons[:2]),
        skill_gaps=result.skill_gaps,
        tailoring_variant=result.tailoring_variant,
        red_flags=result.red_flags,
        tfidf_score=result.tfidf_score,
        skill_match_score=result.skill_match_score,
        status=result.db_status,
        score_version=result.score_version,
        raw_llm_score=result.raw_llm_score,
        verdict=result.verdict,
        score_model=result.score_model,
        score_prompt_version=result.score_prompt_version,
        score_breakdown=result.score_breakdown,
        hard_gate_failures=result.hard_gate_failures,
        jd_required_years=result.jd_required_years,
        jd_geo_scope=result.jd_geo_scope,
        description_quality=result.description_quality,
    )


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
        # Build JD corpus from all job descriptions for meaningful IDF weights
        jd_corpus = [j.description for j in jobs if j.description]
        tfidf_scorer = TFIDFScorer(resume_text, jd_corpus=jd_corpus)
        skill_extractor = SkillExtractor()
        resume_skills = SkillExtractor.build_resume_skill_profile(master_resume)
        if tfidf_scorer.available:
            console.print(f"  [dim]TF-IDF pre-scorer ready (corpus={len(jd_corpus)} JDs, threshold={tfidf_threshold}) | "
                          f"Domain: {domain} | {candidate_years:.0f} yrs exp | "
                          f"Resume skill profile: {len(resume_skills)} skills[/dim]")
    except Exception as e:
        console.print(f"  [dim]Two-stage scoring init error (non-fatal): {e}[/dim]")

    max_workers = scoring_cfg.get("max_concurrent_workers", 4)
    console.print(f"  [dim]Scoring with up to {max_workers} concurrent workers (parallel per key)[/dim]")

    # Pin one model for the whole batch: mixing a 70B and an 8B into the same
    # match_score column makes a fixed threshold meaningless.
    pin_model = scoring_cfg.get("pin_model") or llm_client.primary_model()
    if pin_model:
        console.print(f"  [dim]Model pinned to {pin_model} (no silent downgrade)[/dim]")

    def _score_one(job):
        score_result = score_job(
            job_title=job.title,
            job_company=job.company,
            job_description=job.description or "",
            master_resume=master_resume,
            llm_client=llm_client,
            auto_apply_threshold=auto_threshold,
            review_threshold=review_threshold,
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
            job_location=getattr(job, "location", None),
            is_remote=bool(getattr(job, "is_remote", False)),
            salary_min=getattr(job, "salary_min", None),
            salary_max=getattr(job, "salary_max", None),
            salary_currency=getattr(job, "salary_currency", None),
            pin_model=pin_model,
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

            _persist_score(job, score_result)
            results[job.id] = score_result

    # Retry pass: jobs that failed due to transient rate-limiting/API errors (not
    # real scoring failures) get one more sequential attempt once cooldowns from
    # the concurrent pass have likely expired, instead of being permanently
    # mislabeled as low-fit skips.
    # Only true LLM/transport failures are retryable — a gated job has a real,
    # deterministic answer and retrying it would just burn quota.
    failed_jobs = [job for job in jobs if results[job.id].error]
    if failed_jobs:
        import time as _time
        console.print(f"\n[dim]Retrying {len(failed_jobs)} jobs that failed due to API errors...[/dim]")
        _time.sleep(DEFAULT_RETRY_WAIT_SECONDS)
        for job in failed_jobs:
            _, retry_result = _score_one(job)
            if not retry_result.error:
                console.print(f"  [green]Retry succeeded:[/green] {job.title} @ {job.company} -> {retry_result.display_score}")
            _persist_score(job, retry_result)
            results[job.id] = retry_result

    # Summary
    auto_count = sum(1 for r in results.values() if r.verdict == "auto_apply")
    review_count = sum(1 for r in results.values() if r.verdict == "review")
    skip_count = sum(1 for r in results.values() if r.verdict == "skip")
    gated_count = sum(1 for r in results.values() if r.gated)
    enrich_count = sum(1 for r in results.values() if r.needs_enrichment)
    error_count = sum(1 for r in results.values() if r.error)

    tfidf_skipped = sum(1 for r in results.values() if r.skipped_llm)

    console.print(f"\n[bold]Scoring complete:[/bold]")
    console.print(f"  [dim]Gated: {gated_count} | Needs enrichment: {enrich_count} | Errors: {error_count}[/dim]")
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
        jd_corpus = [j.description for j in jobs if j.description]
        tfidf_scorer = TFIDFScorer(resume_text, jd_corpus=jd_corpus)
        skill_extractor = SkillExtractor()
        resume_skills = SkillExtractor.build_resume_skill_profile(resume_json)
        if tfidf_scorer.available:
            console.print(f"  [dim]TF-IDF (corpus={len(jd_corpus)} JDs, threshold={tfidf_threshold}) | Skills: {len(resume_skills)}[/dim]")
    except Exception as e:
        console.print(f"  [dim]Two-stage scoring init error (non-fatal): {e}[/dim]")

    max_workers = scoring_cfg.get("max_concurrent_workers", 4)
    results: dict[int, ScoreResult] = {}
    pin_model = scoring_cfg.get("pin_model") or llm_client.primary_model()

    def _score_one(job):
        return job, score_job(
            job_title=job.title,
            job_company=job.company,
            job_description=job.description or "",
            master_resume=resume_json,
            llm_client=llm_client,
            auto_apply_threshold=auto_threshold,
            review_threshold=review_threshold_val,
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
            job_location=getattr(job, "location", None),
            is_remote=bool(getattr(job, "is_remote", False)),
            salary_min=getattr(job, "salary_min", None),
            salary_max=getattr(job, "salary_max", None),
            salary_currency=getattr(job, "salary_currency", None),
            pin_model=pin_model,
        )

    def _persist(job, res):
        update_job_score_for_resume(
            job_id=job.id,
            resume_id=resume_id,
            score=res.score,
            reasoning=res.summary_hint or "; ".join(res.match_reasons[:2]),
            skill_gaps=res.skill_gaps,
            tailoring_variant=res.tailoring_variant,
            red_flags=res.red_flags,
            tfidf_score=res.tfidf_score,
            skill_match_score=res.skill_match_score,
            verdict=res.verdict,
            adjusted_score=res.score,
            recency_bonus_applied=res.recency_bonus,
            seniority_multiplier_applied=res.seniority_fit,
            status=res.db_status,
            score_version=res.score_version,
            raw_llm_score=res.raw_llm_score,
            score_model=res.score_model,
            score_prompt_version=res.score_prompt_version,
            score_breakdown=res.score_breakdown,
            hard_gate_failures=res.hard_gate_failures,
        )

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = [executor.submit(_score_one, job) for job in jobs]
        for future in as_completed(futures):
            job, score_result = future.result()
            _persist(job, score_result)
            results[job.id] = score_result

    # Retry pass: only genuine LLM/transport failures, never deterministic gates
    failed_jobs = [job for job in jobs if results[job.id].error]
    if failed_jobs:
        import time as _time
        console.print(f"  [dim]Retrying {len(failed_jobs)} jobs that failed due to API errors...[/dim]")
        _time.sleep(DEFAULT_RETRY_WAIT_SECONDS)
        for job in failed_jobs:
            _, retry_result = _score_one(job)
            _persist(job, retry_result)
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
