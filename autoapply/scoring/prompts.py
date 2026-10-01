"""
All LLM prompt templates for AutoAppy.
v3: Domain-aware scoring prompts, per-user candidate_years, post-LLM multiplier context.
"""
import json

# ── Domain-aware system prompts ───────────────────────────────────────────────
_DOMAIN_SYSTEM_PROMPTS: dict[str, str] = {
    "embedded_testing": (
        "You are a senior technical recruiter specialising in embedded systems, firmware engineering, "
        "and software QA/testing roles. Evaluate candidate-job fit strictly — give high scores ONLY when "
        "the candidate's embedded/testing skills (C, C++, RTOS, test frameworks, hardware protocols, "
        "automotive standards) genuinely align with the JD requirements. "
        "Penalise heavily when the JD requires domain-specific hardware or testing knowledge the candidate lacks. "
        "Never use newlines inside the JSON. Never add text before or after the JSON."
    ),
    "data": (
        "You are a senior technical recruiter specialising in data engineering, data science, and "
        "analytics roles. Evaluate how well the candidate's data pipeline, warehousing, ML, and "
        "analytics skills match the JD. Score strictly — only high if there is genuine alignment. "
        "Never use newlines inside the JSON. Never add text before or after the JSON."
    ),
    "ai_ml": (
        "You are a senior technical recruiter specialising in AI, ML, and LLM engineering roles. "
        "Evaluate how well the candidate's ML, NLP, deep learning, and AI infrastructure skills "
        "match the JD. Score strictly — only score high when there is genuine model/research alignment. "
        "Never use newlines inside the JSON. Never add text before or after the JSON."
    ),
    "frontend": (
        "You are a senior technical recruiter specialising in frontend and full-stack web engineering. "
        "Evaluate how well the candidate's UI, framework, and web skills match the JD. "
        "Score strictly. Never use newlines inside the JSON. Never add text before or after the JSON."
    ),
    "backend": (
        "You are a senior technical recruiter with 15 years of experience hiring backend, platform, "
        "and infrastructure engineers. Evaluate candidate-job fit accurately. Be objective — only score "
        "high if there is genuine skill and experience alignment. "
        "Never use newlines inside the JSON. Never add text before or after the JSON."
    ),
    "general": (
        "You are a senior technical recruiter with 15 years of experience. Evaluate candidate-job fit "
        "accurately and respond with a single-line compact JSON object. Be objective — only score high "
        "if there is genuine skill and experience alignment. "
        "Never use newlines inside the JSON. Never add text before or after the JSON."
    ),
}


def get_score_system_prompt(domain: str = "general") -> str:
    """Return the domain-appropriate scoring system prompt."""
    return _DOMAIN_SYSTEM_PROMPTS.get(domain, _DOMAIN_SYSTEM_PROMPTS["general"])


# Keep for backwards compatibility
SCORE_SYSTEM_PROMPT = _DOMAIN_SYSTEM_PROMPTS["backend"]


def _build_skills_line(skills: dict) -> str:
    all_skills = []
    for category, items in skills.items():
        if isinstance(items, list):
            all_skills.extend(items)
    seen, unique = set(), []
    for s in all_skills:
        s = str(s).strip()
        if s and s not in seen:
            seen.add(s)
            unique.append(s)
    return ", ".join(unique[:35])


def build_score_prompt(
    resume_summary: str,
    skills: dict,
    experience_summary: str,
    jd: str,
    location_hint: str = "",
    candidate_meta: dict | None = None,
    matched_skills: list | None = None,
    missing_skills: list | None = None,
    recency_hint: str = "",
    seniority_hint: str = "",
    employment_type_hint: str = "",
    domain: str = "general",
    candidate_years: float = 3.0,
) -> str:
    skills_line = _build_skills_line(skills)
    meta = candidate_meta or {}
    current_company = meta.get("current_company", "")
    current_title = meta.get("current_title", "")

    # Use actual candidate_years (per-user), not hardcoded 3
    years_display = f"{candidate_years:.0f}" if candidate_years == int(candidate_years) else f"{candidate_years:.1f}"

    cand = resume_summary[:400]
    if current_title and current_company:
        cand += f"\nCurrent: {current_title} at {current_company} ({years_display} yrs exp)"
    elif current_title:
        cand += f"\nCurrent role: {current_title} ({years_display} yrs exp)"
    else:
        cand += f"\nExperience: {years_display} years"
    cand += f"\nSkills: {skills_line}"
    if experience_summary:
        cand += f"\n\nKey Experience:\n{experience_summary[:600]}"

    loc = ""
    if location_hint:
        loc = f"\nLOCATION: {location_hint} — add +5 to score if India/Bangalore/remote-eligible."

    skill_ctx = ""
    if matched_skills:
        skill_ctx += f"\nSKILL MATCH: {', '.join(matched_skills[:10])} found in JD that match candidate."
    if missing_skills:
        skill_ctx += f"\nSKILL GAP: {', '.join(missing_skills[:8])} required in JD but not in candidate profile."

    extra_ctx = ""
    if recency_hint:
        extra_ctx += f"\nRECENCY: {recency_hint}"
    if seniority_hint:
        extra_ctx += f"\nSENIORITY: {seniority_hint}"
    if employment_type_hint:
        extra_ctx += f"\nEMPLOYMENT TYPE: {employment_type_hint}"

    # Domain-specific rubric note
    domain_note = ""
    if domain == "embedded_testing":
        domain_note = (
            "\nDOMAIN: embedded/testing role — score HIGH only if candidate has C/C++, "
            "RTOS, test frameworks, or hardware protocol experience. Score LOW if candidate "
            "is purely backend/web and lacks hardware or testing experience."
        )
    elif domain == "data":
        domain_note = "\nDOMAIN: data engineering/science role — score based on pipeline, ML, analytics alignment."
    elif domain == "ai_ml":
        domain_note = "\nDOMAIN: AI/ML role — score based on model, NLP, LLM, research infrastructure alignment."
    elif domain == "frontend":
        domain_note = "\nDOMAIN: frontend/full-stack role — score based on UI framework and web skills alignment."

    return (
        f"Evaluate this candidate. Respond ONLY with valid complete JSON — no truncation.\n\n"
        f"CANDIDATE:\n{cand}\n\nJOB DESCRIPTION:\n{jd[:3000]}"
        f"{loc}{skill_ctx}{extra_ctx}{domain_note}\n\n"
        f"Score rubric (0-100):\n"
        f"- Skill match (35 pts)  - Experience level (25 pts)\n"
        f"- Domain alignment (20 pts)  - Recency/freshness (10 pts)  - Location/eligibility (10 pts)\n\n"
        f'Respond with ONLY: {{"score":0,"verdict":"skip","match_reasons":["r1"],'
        f'"skill_gaps":["g1"],"red_flags":[],"tailoring_variant":"balanced","summary_hint":"hint"}}\n\n'
        f"Rules: score=0-100, verdict=auto_apply(>=75)/review(60-74)/skip(<60),\n"
        f"tailoring_variant=backend/ai_ml/balanced/data_infra/embedded/testing, max 3 match_reasons,\n"
        f"max 3 skill_gaps, max 2 red_flags, summary_hint max 12 words."
    )


# ── Scoring v2 ────────────────────────────────────────────────────────────────
# The LLM judges ONLY genuine skill/experience fit. Recency, location, seniority
# and employment type are decided deterministically in gates.py and applied once
# by the scorer — including them here too is what caused the v1 double-counting.

PROMPT_VERSION = "v2"

_V2_DOMAIN_FOCUS: dict[str, str] = {
    "embedded_testing": (
        "This is an embedded/firmware/QA hiring decision. Credit C, C++, RTOS, hardware "
        "protocols, test frameworks and automotive standards. Do not credit generic web "
        "or backend experience as embedded experience."
    ),
    "data": "This is a data engineering/science hiring decision. Credit pipelines, warehousing, modelling and analytics depth.",
    "ai_ml": "This is an AI/ML hiring decision. Credit model training, NLP, LLM systems and ML infrastructure depth.",
    "frontend": "This is a frontend/full-stack hiring decision. Credit UI frameworks, browser platform and web performance depth.",
    "backend": "This is a backend/platform hiring decision. Credit distributed systems, API design, data stores and production operations depth.",
    "general": "Judge the candidate against whatever discipline the job description actually describes.",
}

SCORE_V2_SYSTEM_PROMPT = (
    "You are a hiring manager evaluating one candidate against one job description.\n"
    "{domain_focus}\n\n"
    "Rules you must follow:\n"
    "1. Judge ONLY skill, experience and domain fit. Do NOT consider job posting age, "
    "job location, visa eligibility, or employment type — those are handled separately.\n"
    "2. Reward demonstrated depth over keyword presence. A keyword in the resume with no "
    "supporting work is weak evidence.\n"
    "3. Be calibrated, not generous. A typical plausible applicant should land mid-range. "
    "Reserve the top of each band for genuinely strong matches.\n"
    "4. If the job description is vague or mostly company boilerplate, lower your confidence "
    "rather than inventing a fit.\n"
    "5. Output exactly one JSON object. No prose, no markdown fences, no newlines inside strings."
)


def get_score_v2_system_prompt(domain: str = "general") -> str:
    focus = _V2_DOMAIN_FOCUS.get(domain, _V2_DOMAIN_FOCUS["general"])
    return SCORE_V2_SYSTEM_PROMPT.format(domain_focus=focus)


SCORE_V2_REQUIRED_KEYS = (
    "skill_score", "experience_score", "domain_score",
    "stack_depth_score", "growth_score",
)


def _all_skills(skills: dict) -> list[str]:
    out: list[str] = []
    seen = set()
    for items in skills.values():
        if not isinstance(items, list):
            continue
        for item in items:
            s = str(item).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
    return out


def build_score_prompt_v2(
    *,
    job_title: str,
    job_company: str,
    job_location: str,
    jd: str,
    resume_summary: str,
    skills: dict,
    experience_summary: str,
    candidate_meta: dict | None = None,
    candidate_years: float | None = None,
    matched_skills: list | None = None,
    missing_skills: list | None = None,
    domain: str = "general",
    jd_char_budget: int = 6000,
) -> str:
    """Build the v2 user prompt. Title/company/location are included — v1 never sent them."""
    meta = candidate_meta or {}
    years_display = "unknown"
    if candidate_years is not None:
        years_display = (
            f"{candidate_years:.0f}" if candidate_years == int(candidate_years)
            else f"{candidate_years:.1f}"
        )

    cand = [resume_summary.strip()[:800]]
    current_title = meta.get("current_title", "")
    current_company = meta.get("current_company", "")
    if current_title or current_company:
        cand.append(f"Current: {current_title} at {current_company} ({years_display} yrs total experience)")
    else:
        cand.append(f"Total experience: {years_display} years")
    cand.append("Skills: " + ", ".join(_all_skills(skills)))
    if experience_summary:
        cand.append("\nWork history:\n" + experience_summary.strip()[:1500])

    hints = []
    if matched_skills:
        hints.append(
            "Overlapping keywords (weak signal — verify against the work history): "
            + ", ".join(matched_skills[:15])
        )
    if missing_skills:
        hints.append(
            "JD keywords absent from the resume: " + ", ".join(missing_skills[:10])
        )

    jd_text = jd.strip()[:jd_char_budget]

    return (
        f"JOB\n"
        f"Title: {job_title}\n"
        f"Company: {job_company}\n"
        f"Location: {job_location or 'not specified'}\n\n"
        f"JOB DESCRIPTION\n{jd_text}\n\n"
        f"CANDIDATE\n" + "\n".join(cand) + "\n\n"
        + ("\n".join(hints) + "\n\n" if hints else "")
        + "Score each dimension independently, using the full range of each:\n"
        "- skill_score (0-35): overlap between required skills and demonstrated candidate skills\n"
        "- experience_score (0-25): does the candidate's actual work match the scope and "
        "responsibility this role describes\n"
        "- domain_score (0-20): familiarity with this problem domain and industry\n"
        "- stack_depth_score (0-10): depth in the specific technologies the JD names\n"
        "- growth_score (0-10): would this role be a meaningful step up rather than lateral or a downgrade\n"
        "- confidence (0.0-1.0): how much the job description actually told you\n\n"
        'Respond with ONLY this JSON shape: {"skill_score":0,"experience_score":0,'
        '"domain_score":0,"stack_depth_score":0,"growth_score":0,"confidence":0.0,'
        '"match_reasons":["..."],"skill_gaps":["..."],"red_flags":[],'
        '"tailoring_variant":"balanced","summary_hint":"..."}\n\n'
        "Constraints: all *_score fields are integers within their stated range; "
        "tailoring_variant is one of backend/ai_ml/balanced/data_infra/embedded/testing; "
        "max 3 match_reasons; max 3 skill_gaps; max 2 red_flags; summary_hint max 14 words."
    )


RESUME_TAILOR_SYSTEM = """You are a professional resume writer who tailors resumes to job descriptions.
NEVER invent experience, titles, metrics, or skills. Only reorder, rephrase, emphasize real experience.
Match the JD language where truthful. Keep bullets specific and quantified. Return only valid JSON."""


def build_resume_tailor_prompt(master_resume: dict, jd: str, variant: str, score_hints: str) -> str:
    """
    Fixed: accepts dict (eliminates double json.dumps() bug).
    Resume data expanded to 4000 chars. JD expanded to 2000 chars.
    """
    if isinstance(master_resume, str):
        resume_str = master_resume[:4000]
    else:
        resume_str = json.dumps(master_resume, indent=2)[:4000]

    guidance = {
        "backend": "Emphasize distributed systems, APIs, databases, performance, reliability.",
        "ai_ml": "Emphasize ML pipelines, LLMs, NLP, model serving, Python/PyTorch.",
        "data_infra": "Emphasize data pipelines, ETL, Spark/Kafka, warehousing, data quality.",
        "balanced": "Emphasize full breadth — backend, data systems, and AI/ML equally.",
        "embedded": "Emphasize embedded systems, RTOS, hardware protocols, firmware, C/C++.",
        "testing": "Emphasize test automation, frameworks, quality processes, SDET experience.",
    }.get(variant, "Emphasize most relevant experience for this JD.")

    return (
        f"Tailor this resume for the JD. Variant: {variant}. {guidance}\n"
        f"Hint: {score_hints}\n\nRULES:\n"
        f"- Never invent experience, employers, titles, tools, metrics, or dates\n"
        f"- Only reorder, rephrase, emphasize real experience\n"
        f"- Mirror JD language where truthful; bullets must be specific and quantified\n\n"
        f"MASTER RESUME:\n{resume_str}\n\nJOB DESCRIPTION:\n{jd[:2000]}\n\n"
        f'Return ONLY this JSON:\n{{\n'
        f'  "chosen_summary": "<tailored 2-3 sentence summary>",\n'
        f'  "experience_bullets": [{{"project": "<exact project name>", "bullets": ["b1","b2","b3"]}}],\n'
        f'  "top_skills": ["s1","s2","s3","s4","s5","s6"],\n'
        f'  "additional_skills": ["s1","s2","s3"]\n}}'
    )


COVER_LETTER_SYSTEM = """You are a professional cover letter writer.
Write concise, authentic 3-paragraph cover letters. Never invent experience.
Be specific and achievement-focused. Avoid generic platitudes."""


def build_cover_letter_prompt(
    candidate: dict, company: str, role: str, jd: str,
    key_experience: str, tailoring_hint: str,
) -> str:
    """JD expanded from 1000 → 2000 chars."""
    return (
        f"Write a targeted cover letter.\n\n"
        f"CANDIDATE: {candidate['name']}\nROLE: {role} at {company}\n"
        f"KEY EXPERIENCE: {key_experience}\nTAILORING HINT: {tailoring_hint}\n\n"
        f"JOB DESCRIPTION:\n{jd[:2000]}\n\n"
        f"Write exactly 3 paragraphs:\n"
        f"1. Hook: Why this specific role/company — ref something from JD\n"
        f"2. Evidence: 2-3 specific measurable achievements matching role\n"
        f"3. Closing: Confident specific call to action\n\n"
        f'Return JSON: {{"subject":"<subject>","body":"<letter paragraphs by \\n\\n>","word_count":<int>}}'
    )
