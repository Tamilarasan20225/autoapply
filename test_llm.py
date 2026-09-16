#!/usr/bin/env python3
"""Quick test to verify LLM scoring is working on backend/AI relevant jobs."""

import os, json, yaml
from dotenv import load_dotenv
load_dotenv()

with open('config.yaml') as f:
    config = yaml.safe_load(f)
with open('master_resume.json') as f:
    master_resume = json.load(f)

from autoapply.tracker.db import init_db, get_session
from autoapply.tracker.models import Job
from autoapply.scoring.llm_client import LLMClient
from autoapply.scoring.scorer import score_job

init_db('data/autoapply.db')

llm = LLMClient(config=config)
print('Providers:', [p['name'] for p in llm.providers])

# Get jobs with backend/python/java keywords in description
session = get_session()
jobs = session.query(Job).filter(
    Job.status == 'discovered',
    Job.description.isnot(None),
).filter(
    Job.description.ilike('%backend%') |
    Job.description.ilike('%java%') |
    Job.description.ilike('%python%') |
    Job.description.ilike('%data pipeline%')
).order_by(Job.source).limit(3).all()
session.close()

print(f'Found {len(jobs)} relevant jobs to test\n')

for job in jobs:
    print(f'Scoring: {job.title} @ {job.company} [{job.source}]')
    result = score_job(
        job_title=job.title,
        job_company=job.company,
        job_description=job.description,
        master_resume=master_resume,
        llm_client=llm,
    )
    icon = "✅" if result.verdict == "auto_apply" else ("🟡" if result.verdict == "review" else "❌")
    print(f'  {icon} Score: {result.score:.0f}/100  Verdict: {result.verdict}')
    print(f'  Variant: {result.tailoring_variant}')
    if result.match_reasons:
        print(f'  Match: {result.match_reasons[0]}')
    if result.skill_gaps:
        print(f'  Gaps: {result.skill_gaps[0]}')
    print()

print('✅ LLM scoring verified!')
