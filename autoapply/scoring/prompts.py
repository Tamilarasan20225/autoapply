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
