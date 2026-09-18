"""
AutoAppy Streamlit Dashboard.
Run: streamlit run autoapply/dashboard/app.py

Sprint improvements:
- Manual status override (applied/interview/rejected/skipped)
- Interview pipeline tracker
- Cache stats widget
- Source analytics
- Salary insights
- Latest jobs section (newest first for higher conversion)
- Live pipeline log viewer (real-time stdout + log file tail)
"""

import sys
import re
import subprocess
import threading
import time
from pathlib import Path

# Ensure project root is in path
PROJECT_ROOT = str(Path(__file__).parent.parent.parent)
sys.path.insert(0, PROJECT_ROOT)

import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime, timedelta, timezone


def _utcnow() -> datetime:
    """Return timezone-naive UTC now — matches SQLite stored values."""
    return datetime.utcnow()


def _naive(dt: datetime) -> datetime:
    """Strip tzinfo so offset-naive DB datetimes can be subtracted safely."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

from autoapply.tracker.db import init_db, get_all_jobs, get_session, get_latest_jobs
from autoapply.tracker.models import Job, RunLog
# ── Page Config ──────────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="AutoAppy Dashboard",
    page_icon="🤖",
    layout="wide",
)

# ── Init DB ───────────────────────────────────────────────────────────────────────────────────
# On Streamlit Cloud the app runs from /mount/src/autoapply/
# DB is committed to the repo at data/autoapply.db relative to project root
DB_PATH = str(Path(PROJECT_ROOT) / "data" / "autoapply.db")
init_db(DB_PATH)

# ── Log file path (written by autoapply.utils.logger) ─────────────────────────
LOG_FILE = Path(PROJECT_ROOT) / "logs" / "autoapply.log"

# ── Custom CSS ─────────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .metric-card { background: #1e293b; padding: 16px; border-radius: 10px; margin-bottom: 8px; }
    .status-badge { padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }
    .badge-green { background: #22c55e22; color: #22c55e; }
    .badge-yellow { background: #f59e0b22; color: #f59e0b; }
    .badge-red { background: #ef444422; color: #ef4444; }
    div[data-testid="stExpander"] { border: 1px solid #334155; border-radius: 8px; }
</style>
""", unsafe_allow_html=True)


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────

def _safe(obj, attr, default="—"):
    """Safe attribute getter — returns default if attribute missing or None."""
    val = getattr(obj, attr, None)
    if val is None:
        return default
    return val


def _strip_ansi(text: str) -> str:
    """Remove ANSI colour/style codes from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def load_jobs_df() -> pd.DataFrame:
    """Load all jobs into a DataFrame for display."""
    jobs = get_all_jobs(limit=2000)
    if not jobs:
        return pd.DataFrame()

    rows = []
    for j in jobs:
        try:
            # Coerce match_score to float or None — never leave as non-numeric
            raw_score = getattr(j, "match_score", None)
            try:
                score_val = float(raw_score) if raw_score is not None else None
            except (TypeError, ValueError):
                score_val = None

            # Coerce tfidf_score and skill_match_score to float
            raw_tfidf = getattr(j, "tfidf_score", None)
            try:
                tfidf_val = round(float(raw_tfidf), 3) if raw_tfidf is not None else 0.0
            except (TypeError, ValueError):
                tfidf_val = 0.0

            raw_skill = getattr(j, "skill_match_score", None)
            try:
                skill_val = round(float(raw_skill), 2) if raw_skill is not None else 0.0
            except (TypeError, ValueError):
                skill_val = 0.0

            rows.append({
                "ID": j.id,
                "Company": _safe(j, "company", "Unknown"),
                "Title": _safe(j, "title", "Unknown"),
                "Source": _safe(j, "source", "—"),
                "Score": score_val,
                "Status": _safe(j, "status", "discovered"),
                "ATS": _safe(j, "ats_type", "—"),
                "Location": _safe(j, "location", "—"),
                "Remote": "✓" if getattr(j, "is_remote", False) else "",
                "Applied On": j.applied_at.strftime("%b %d") if getattr(j, "applied_at", None) else "—",
                "Discovered": j.discovered_at.strftime("%b %d") if getattr(j, "discovered_at", None) else "—",
                "URL": _safe(j, "job_url", ""),
                "Apply URL": _safe(j, "apply_url", ""),
                "Posted At": _safe(j, "posted_at", "—"),
                "Seniority": _safe(j, "seniority_level", "—"),
                "Employment": _safe(j, "employment_type", "—"),
                "TF-IDF": tfidf_val,
                "Skill Match": skill_val,
                "Resume": _safe(j, "resume_path", ""),
                "Cover Letter": _safe(j, "cover_letter_path", ""),
                "Score Reasoning": _safe(j, "score_reasoning", ""),
                "Skill Gaps": _safe(j, "skill_gaps", ""),
                "Follow Up": j.follow_up_date.strftime("%b %d") if getattr(j, "follow_up_date", None) else "—",
                "Interview Stage": _safe(j, "interview_stage", "—"),
                "Notes": _safe(j, "notes", ""),
                "Salary Min": float(j.salary_min) if getattr(j, "salary_min", None) is not None else None,
                "Salary Max": float(j.salary_max) if getattr(j, "salary_max", None) is not None else None,
            })
        except Exception:
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Ensure Score column is strictly numeric (float64) so .round(), comparisons, etc. never crash
    df["Score"] = pd.to_numeric(df["Score"], errors="coerce")
    df["TF-IDF"] = pd.to_numeric(df["TF-IDF"], errors="coerce").fillna(0.0)
    df["Skill Match"] = pd.to_numeric(df["Skill Match"], errors="coerce").fillna(0.0)
    return df


def get_run_logs() -> list:
    session = get_session()
    try:
        return session.query(RunLog).order_by(RunLog.started_at.desc()).limit(10).all()
    finally:
        session.close()


def update_job_field(job_id: int, **kwargs):
    """Update job fields from the dashboard."""
    from autoapply.tracker.db import update_job_status
    status = kwargs.pop("status", None)
    if status:
        update_job_status(job_id, status, **kwargs)
    else:
        from autoapply.tracker.db import get_session
        from autoapply.tracker.models import Job
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if job:
                for k, v in kwargs.items():
                    if hasattr(job, k):
                        setattr(job, k, v)
                session.commit()
        finally:
            session.close()


def run_pipeline_command(cmd_args: list) -> str:
    """Run a pipeline command and return combined output (blocking)."""
    try:
        result = subprocess.run(
            [sys.executable] + cmd_args,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=600,
        )
        output = result.stdout + result.stderr
        return _strip_ansi(output)
    except subprocess.TimeoutExpired:
        return "⚠️ Command timed out after 10 minutes."
    except Exception as e:
        return f"❌ Error: {e}"


def run_pipeline_streaming(cmd_args: list, log_placeholder) -> str:
    """
    Run a pipeline command and stream stdout+stderr live into a Streamlit placeholder.
    Returns the full combined output when done.
    """
    proc = subprocess.Popen(
        [sys.executable] + cmd_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=PROJECT_ROOT,
        bufsize=1,
    )
    lines = []
    try:
        for raw_line in proc.stdout:
            line = _strip_ansi(raw_line)
            lines.append(line)
            log_placeholder.code("".join(lines[-200:]), language="text")
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        lines.append("\n⚠️ Process timed out and was killed.")
        log_placeholder.code("".join(lines[-200:]), language="text")
    except Exception as e:
        lines.append(f"\n❌ Stream error: {e}")
        log_placeholder.code("".join(lines[-200:]), language="text")
    return "".join(lines)


def read_log_tail(n_lines: int = 100) -> str:
    """
    Read the last N lines from logs/autoapply.log.
    Returns empty string if the file doesn't exist yet.
    """
    try:
        if not LOG_FILE.exists():
            return ""
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-n_lines:])
    except Exception as e:
        return f"Error reading log: {e}"


# ── Header ──────────────────────────────────────────────────────────────────────────────────
st.title("🤖 AutoAppy Dashboard")
st.markdown("*Automated Job Application System — Tamilarasan S*")
st.divider()

# ── Multi-Resume Management ──────────────────────────────────────────────────────────────────
with st.expander("📄 Resumes — upload & manage additional profiles", expanded=False):
    import json as _json
    from autoapply.tracker.db import (
        create_resume, get_all_resumes, set_resume_active, get_scored_jobs_for_resume,
    )
    from autoapply.generator.resume_parser import parse_resume_file, ResumeParseError

    st.caption(
        "Upload a resume (.json matching master_resume.json's schema, or .pdf/.docx) "
        "to score the shared job pool against it too. Discovery+scoring only — "
        "auto-apply remains restricted to the default resume."
    )

    upload_col, config_col = st.columns(2)
    with upload_col:
        resume_label = st.text_input("Label (e.g. person's name)", key="new_resume_label")
        uploaded_file = st.file_uploader("Resume file", type=["json", "pdf", "docx"], key="new_resume_file")
    with config_col:
        new_roles = st.text_area("Target roles (one per line)", key="new_resume_roles", height=80)
        new_locations = st.text_area("Locations (one per line)", key="new_resume_locations", height=80)
        new_exclude = st.text_input("Exclude companies (comma-separated)", key="new_resume_exclude")

    if st.button("➕ Add Resume", key="add_resume_btn"):
        if not resume_label or not uploaded_file:
            st.warning("Provide a label and a resume file.")
        else:
            try:
                resume_json = parse_resume_file(uploaded_file.getvalue(), uploaded_file.name)
                search_config = {
                    "roles": [r.strip() for r in new_roles.splitlines() if r.strip()],
                    "locations": [l.strip() for l in new_locations.splitlines() if l.strip()],
                    "exclude_companies": [c.strip() for c in new_exclude.split(",") if c.strip()],
                }
                create_resume(
                    label=resume_label,
                    resume_json=_json.dumps(resume_json),
                    raw_file_path=None,
                    search_config=_json.dumps(search_config),
                )
                st.success(f"Resume '{resume_label}' added.")
                st.rerun()
            except ResumeParseError as e:
                st.error(f"Could not parse resume: {e}")

    st.divider()
    resumes = get_all_resumes()
    if not resumes:
        st.info("No additional resumes uploaded yet.")
    else:
        for r in resumes:
            rc1, rc2, rc3 = st.columns([3, 1, 1])
            rc1.markdown(f"**{r.label}** — {'🟢 active' if r.is_active else '⚪ inactive'}")
            if rc2.button("Toggle", key=f"toggle_resume_{r.id}"):
                set_resume_active(r.id, not r.is_active)
                st.rerun()
            with rc3:
                pass

        st.divider()
        resume_labels = {r.label: r.id for r in resumes}
        selected_label = st.selectbox("View scored jobs for:", list(resume_labels.keys()), key="resume_view_select")
        if selected_label:
            pairs = get_scored_jobs_for_resume(resume_labels[selected_label], limit=200)
            if pairs:
                view_df = pd.DataFrame([{
                    "Company": job.company, "Title": job.title, "Score": score.match_score,
                    "Status": score.status, "URL": job.job_url,
                } for job, score in pairs])
                st.dataframe(view_df, use_container_width=True, hide_index=True)
            else:
                st.info("No scored jobs yet for this resume — run the scoring pipeline.")

# ── Load Data ─────────────────────────────────────────────────────────────────────────────────
df = load_jobs_df()

if df.empty:
    st.info("🔍 No jobs yet. Click **Run Pipeline** below to start discovering jobs.")
    st.divider()
else:
    # ── Top Metrics ─────────────────────────────────────────────────────────────────────────────
    total = len(df)
    applied = len(df[df["Status"] == "applied"])
    manual_review = len(df[df["Status"] == "manual_review"])
    interviews = len(df[df["Status"].isin(["interview", "phone_screen", "technical", "offer"])])
    skipped = len(df[df["Status"] == "skipped"])
    scored_count = df["Score"].notna().sum()
    auto_apply_count = int((df["Score"] >= 75).sum()) if "Score" in df.columns else 0
    review_count = int(((df["Score"] >= 60) & (df["Score"] < 75)).sum())

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("📋 Total Found", total)
    col2.metric("🎯 Auto-Apply Ready", auto_apply_count, help="Score ≥ 75")
    col3.metric("🔍 Review Queue", review_count, help="Score 60–74")
    col4.metric("✅ Applied", applied)
    col5.metric("💼 Interviews", interviews)

    if applied > 0 and interviews > 0:
        col6.metric("📈 Response Rate", f"{round(interviews/applied*100,1)}%")
    else:
        col6.metric("📈 Scored", scored_count)

    st.divider()

    # ── Charts Row ────────────────────────────────────────────────────────────────────────────
    chart_col1, chart_col2, chart_col3 = st.columns(3)

    with chart_col1:
        st.subheader("📊 Applications by Status")
        status_counts = df["Status"].value_counts()
        color_map = {
            "applied": "#22c55e",
            "manual_review": "#f59e0b",
            "skipped": "#94a3b8",
            "scored": "#60a5fa",
            "discovered": "#c084fc",
            "failed": "#ef4444",
            "interview": "#10b981",
            "phone_screen": "#06b6d4",
            "technical": "#8b5cf6",
            "offer": "#f97316",
        }
        fig = px.pie(
            values=status_counts.values,
            names=status_counts.index,
            color=status_counts.index,
            color_discrete_map=color_map,
            hole=0.4,
        )
        fig.update_layout(margin=dict(t=0, b=0, l=0, r=0), height=250)
        st.plotly_chart(fig, use_container_width="stretch")

    with chart_col2:
        st.subheader("🎯 Score Distribution")
        scored_df = df[df["Score"].notna()]
        if not scored_df.empty:
            fig2 = px.histogram(
                scored_df, x="Score", nbins=20,
                color_discrete_sequence=["#60a5fa"],
            )
            fig2.add_vline(x=75, line_dash="dash", line_color="green",
                           annotation_text="Auto-apply")
            fig2.add_vline(x=60, line_dash="dash", line_color="orange",
                           annotation_text="Review")
            fig2.update_layout(margin=dict(t=0, b=0, l=0, r=0), height=250, showlegend=False)
            st.plotly_chart(fig2, use_container_width="stretch")
        else:
            st.info("No scored jobs yet — run scoring first")

    with chart_col3:
        st.subheader("🏭 Top Sources")
        source_counts = df["Source"].value_counts().head(8)
        fig3 = px.bar(
            x=source_counts.values,
            y=source_counts.index,
            orientation="h",
            color_discrete_sequence=["#818cf8"],
        )
        fig3.update_layout(margin=dict(t=0, b=0, l=0, r=0), height=250,
                           showlegend=False, yaxis_title="", xaxis_title="Count")
        st.plotly_chart(fig3, use_container_width="stretch")

    st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# ── 🚀 AUTO-APPLY CONTROL CENTER ─────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("🚀 Auto-Apply Control Center")

if not df.empty:
    auto_ready = df[(df["Score"] >= 75) & (df["Status"] == "scored")]
    gh_ready = auto_ready[auto_ready["ATS"] == "greenhouse"]
    lever_ready = auto_ready[auto_ready["ATS"] == "lever"]
    ashby_ready = auto_ready[auto_ready["ATS"] == "ashby"]
    other_ready = auto_ready[~auto_ready["ATS"].isin(["greenhouse", "lever", "ashby"])]

    info_col1, info_col2, info_col3, info_col4, info_col5 = st.columns(5)
    info_col1.metric("🟢 Ready to Auto-Apply", len(auto_ready), help="Status=scored, Score≥75")
    info_col2.metric("🏦 Greenhouse", len(gh_ready))
    info_col3.metric("⚡ Lever", len(lever_ready))
    info_col4.metric("🔷 Ashby", len(ashby_ready))
    info_col5.metric("📋 Manual Needed", len(other_ready))

    if len(auto_ready) > 0:
        direct_count = len(gh_ready) + len(lever_ready) + len(ashby_ready)
        st.success(
            f"✅ **{len(auto_ready)} jobs are ready!** "
            f"{direct_count} auto-submit via ATS, {len(other_ready)} need manual apply."
        )
else:
    st.info("ℹ️ No jobs in database yet. Run **Full Pipeline** to discover and apply.")

st.markdown("")

# Buttons
btn_col1, btn_col2, btn_col3, btn_col4, btn_col5, btn_col6, btn_col7, btn_col8 = st.columns(8)

with btn_col1:
    trigger_full = st.button("🚀 Run Auto-Apply", type="primary",
                             help="Discover → score → apply (real submissions)")
with btn_col2:
    trigger_refresh = st.button("🔄 Refresh Latest", type="primary",
                                help="Pull latest jobs from all sources and score newest ones first")
with btn_col3:
    trigger_dryrun = st.button("🧪 Dry Run",
                               help="Same as Auto-Apply but fills forms WITHOUT submitting")
with btn_col4:
    trigger_score = st.button("📊 Score Only",
                              help="Discover + score with LLM, skip applying")
with btn_col5:
    trigger_discover = st.button("🔍 Discover Only",
                                 help="Only fetch new job listings")
with btn_col6:
    trigger_apply_only = st.button("⚡ Apply Only",
                                   help="Apply to already-scored jobs (skip discovery)")
with btn_col7:
    trigger_resolve_urls = st.button("🌐 Resolve URLs",
                                     help="Resolve aggregator redirect URLs to direct ATS links")
with btn_col8:
    trigger_populate_meta = st.button("📋 Fill Metadata",
                                      help="Back-fill seniority/skills for existing jobs")

# ── Pipeline Execution ────────────────────────────────────────────────────────────────────────────
cmd_to_run = None
cmd_label = ""

if trigger_full:
    cmd_to_run = ["run.py"]
    cmd_label = "🚀 Running Full Auto-Apply Pipeline..."
elif trigger_refresh:
    cmd_to_run = ["run.py", "--refresh"]
    cmd_label = "🔄 Pulling Latest Jobs + Scoring Newest First..."
elif trigger_dryrun:
    cmd_to_run = ["run.py", "--dry-run"]
    cmd_label = "🧪 Running Dry Run..."
elif trigger_score:
    cmd_to_run = ["run.py", "--score-only"]
    cmd_label = "📊 Running Score-Only Pipeline..."
elif trigger_discover:
    cmd_to_run = ["run.py", "--discover-only"]
    cmd_label = "🔍 Running Discovery-Only..."
elif trigger_apply_only:
    cmd_to_run = ["run.py", "--apply-only"]
    cmd_label = "⚡ Running Apply-Only (using scored jobs)..."
elif trigger_resolve_urls:
    cmd_to_run = ["run.py", "--resolve-urls"]
    cmd_label = "🌐 Resolving aggregator redirect URLs..."
elif trigger_populate_meta:
    cmd_to_run = ["run.py", "--populate-metadata"]
    cmd_label = "📋 Filling metadata for existing jobs..."

if cmd_to_run:
    st.divider()
    st.markdown(f"### {cmd_label}")
    st.warning("⚠️ Pipeline is running in real-time. You can see every step below. Do NOT close this tab.")

    # ── Two-column live view: stdout stream (left) + log file tail (right) ──
    live_col, log_col = st.columns([3, 2])

    with live_col:
        st.markdown("**🟡 Live Pipeline Output** *(real-time stdout)*")
        st.caption(f"▶ `python {' '.join(cmd_to_run)}`")
        stream_placeholder = st.empty()
        stream_placeholder.code("⏳ Starting pipeline...", language="text")

    with log_col:
        st.markdown("**📜 Log File** *(`logs/autoapply.log` — last 60 lines)*")
        log_file_placeholder = st.empty()
        log_file_placeholder.code(
            read_log_tail(60) or "— No log entries yet —",
            language="text",
        )

    # ── Stream stdout live into left column ────────────────────────────────────────
    run_pipeline_streaming(cmd_to_run, stream_placeholder)

    # ── After pipeline completes: refresh log file panel ───────────────────────────
    log_file_placeholder.code(read_log_tail(80) or "—", language="text")

    st.success("✅ Pipeline finished! Dashboard will refresh in 2 seconds...")
    time.sleep(2)
    st.rerun()

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# ── 🆕 LATEST JOBS (last 48h) — Always first for highest conversion ───────────────────────
# ══════════════════════════════════════════════════════════════════════════════
latest_jobs_raw = get_latest_jobs(hours=48, limit=50)
if latest_jobs_raw:
    st.subheader(f"🆕 Latest Jobs — Last 48 Hours ({len(latest_jobs_raw)} jobs, newest first)")
    st.caption("🔥 Freshest listings always shown first — early applicants have the highest conversion rate.")

    latest_rows = []
    for j in latest_jobs_raw:
        score_val = f"{j.match_score:.0f}" if j.match_score is not None else "—"
        status_emoji = {
            "discovered": "🔍",
            "scored": "📊",
            "applied": "✅",
            "skipped": "⏭️",
            "failed": "❌",
            "manual_review": "👁️",
        }.get(j.status or "discovered", "📋")
        disc_str = j.discovered_at.strftime("%b %d %H:%M") if j.discovered_at else "—"
        if j.discovered_at:
            hours_ago = (_utcnow() - _naive(j.discovered_at)).total_seconds() / 3600
            if hours_ago < 1:
                freshness = "🔴 < 1h ago"
            elif hours_ago < 6:
                freshness = f"🟠 {int(hours_ago)}h ago"
            elif hours_ago < 24:
                freshness = f"🟡 {int(hours_ago)}h ago"
            else:
                freshness = f"🟢 {int(hours_ago/24)}d ago"
        else:
            freshness = "—"
        latest_rows.append({
            "Freshness": freshness,
            "Discovered": disc_str,
            "Score": score_val,
            "Status": f"{status_emoji} {j.status or 'discovered'}",
            "Company": j.company or "—",
            "Title": (j.title or "—")[:50],
            "Location": j.location or "—",
            "Source": j.source or "—",
            "URL": j.job_url or "",
        })

    latest_df_display = pd.DataFrame(latest_rows)
    st.dataframe(
        latest_df_display,
        use_container_width="stretch",
        column_config={
            "URL": st.column_config.LinkColumn("Job URL", display_text="View →"),
            "Freshness": st.column_config.TextColumn("⏱️ Age", width="small"),
            "Score": st.column_config.TextColumn("Score", width="small"),
            "Status": st.column_config.TextColumn("Status", width="medium"),
        },
        hide_index=True,
    )
    st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# ── 📋 TOP SCORED JOBS (newest first, then score) ────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
if not df.empty:
    # Sort: newest discovered first, then highest score — latest jobs lead to more conversion
    auto_df = df[df["Score"] >= 75].sort_values(
        ["Discovered", "Score"], ascending=[False, False]
    )

    st.subheader(f"✅ Auto-Apply Ready Jobs ({len(auto_df)} total — score ≥ 75, newest first)")

    if auto_df.empty:
        st.info("🔄 No jobs have scored ≥75 yet. Run **Score Only** to score new jobs.")
    else:
        for _, row in auto_df.head(20).iterrows():
            score_val = f"{row['Score']:.0f}" if pd.notna(row["Score"]) else "—"
            ats = row["ATS"]
            ats_badges = {
                "greenhouse": "🏦 Greenhouse",
                "lever": "⚡ Lever",
                "ashby": "🔷 Ashby",
                "linkedin": "💼 LinkedIn",
            }
            ats_badge = ats_badges.get(ats, f"📋 {ats} (manual)")

            india_flag = ""
            try:
                if any(x in row["Location"].lower() for x in ["bangalore", "bengaluru", "india"]) or row["Remote"] == "✓":
                    india_flag = "🇮🇳 "
            except Exception:
                pass

            with st.expander(
                f"🟢 {score_val}/100 — {india_flag}{row['Company']} | {row['Title'][:50]} | {ats_badge} | {row['Status']} | {row['Discovered']}"
            ):
                col_a, col_b, col_c = st.columns(3)
                col_a.markdown(f"**Company:** {row['Company']}")
                col_a.markdown(f"**Location:** {row['Location']}")
                col_b.markdown(f"**ATS Type:** {ats_badge}")
                col_b.markdown(f"**Status:** `{row['Status']}`")
                col_c.markdown(f"**Source:** {row['Source']}")
                col_c.markdown(f"**Discovered:** {row['Discovered']}")

                score_detail_parts = []
                tfidf = row.get("TF-IDF", 0)
                skill_m = row.get("Skill Match", 0)
                seniority = row.get("Seniority", "—")
                posted = row.get("Posted At", "—")
                if tfidf: score_detail_parts.append(f"TF-IDF: {tfidf:.2f}")
                if skill_m: score_detail_parts.append(f"Skill Match: {skill_m:.0%}")
                if seniority != "—": score_detail_parts.append(f"Seniority: {seniority}")
                if posted != "—": score_detail_parts.append(f"Posted: {posted}")
                if score_detail_parts:
                    st.markdown(f"📊 {' | '.join(score_detail_parts)}")
                reasoning = row.get("Score Reasoning", "")
                if reasoning and reasoning != "—":
                    st.markdown(f"💡 *{reasoning}*")

                if row["Resume"]:
                    st.markdown(f"📄 **Resume:** `{row['Resume']}`")
                if row["Cover Letter"]:
                    st.markdown(f"📝 **Cover Letter:** `{row['Cover Letter']}`")

                link_col1, link_col2 = st.columns(2)
                with link_col1:
                    if row.get("URL"):
                        st.link_button("🔗 View Job Posting", row["URL"])
                with link_col2:
                    apply_url = row.get("Apply URL", "")
                    if apply_url and apply_url != row.get("URL", ""):
                        st.link_button("⚡ Apply Direct", apply_url)
                    elif row.get("URL"):
                        st.link_button("⚡ Apply Direct", row["URL"])

                st.markdown("---")
                st.markdown("**⚙️ Update Status:**")
                status_col1, status_col2, status_col3 = st.columns(3)
                with status_col1:
                    if st.button("✅ Mark Applied", key=f"applied_{row['ID']}"):
                        update_job_field(row["ID"], status="applied",
                                        applied_at=_utcnow(),
                                        application_method="manual")
                        st.success("Marked as applied!")
                        st.rerun()
                with status_col2:
                    if st.button("💼 Got Interview", key=f"interview_{row['ID']}"):
                        update_job_field(row["ID"], status="interview",
                                        interview_stage="phone_screen")
                        st.success("Interview stage updated!")
                        st.rerun()
                with status_col3:
                    if st.button("🚫 Skip", key=f"skip_{row['ID']}"):
                        update_job_field(row["ID"], status="skipped")
                        st.info("Skipped.")
                        st.rerun()

    st.divider()

    # ── Review Queue (60-74) ─────────────────────────────────────────────────────────────────────
    # Sort: newest first, then highest score
    review_df_score = df[(df["Score"] >= 60) & (df["Score"] < 75)].sort_values(
        ["Discovered", "Score"], ascending=[False, False]
    )
    if not review_df_score.empty:
        st.subheader(f"🔍 Review Queue ({len(review_df_score)} jobs — score 60–74, newest first)")
        st.info("These jobs are good matches but below auto-apply threshold. Review and apply manually if interested.")

        for _, row in review_df_score.head(15).iterrows():
            score_val = f"{row['Score']:.0f}" if pd.notna(row["Score"]) else "—"
            india_flag = ""
            try:
                if any(x in row["Location"].lower() for x in ["bangalore", "bengaluru", "india"]) or row["Remote"] == "✓":
                    india_flag = "🇮🇳 "
            except Exception:
                pass

            with st.expander(f"🟡 {score_val}/100 — {india_flag}{row['Company']} | {row['Title'][:50]} | {row['Discovered']}"):
                col_a, col_b = st.columns(2)
                col_a.markdown(f"**Company:** {row['Company']}")
                col_a.markdown(f"**Location:** {row['Location']}")
                col_b.markdown(f"**ATS:** {row['ATS']}")
                col_b.markdown(f"**Source:** {row['Source']}")
                if row["URL"]:
                    st.link_button("🔗 Open Job Posting", row["URL"])

                st.markdown("---")
                r_col1, r_col2 = st.columns(2)
                with r_col1:
                    if st.button("✅ Mark Applied", key=f"rev_applied_{row['ID']}"):
                        update_job_field(row["ID"], status="applied",
                                        applied_at=_utcnow(),
                                        application_method="manual")
                        st.success("Marked as applied!")
                        st.rerun()
                with r_col2:
                    if st.button("🚫 Skip", key=f"rev_skip_{row['ID']}"):
                        update_job_field(row["ID"], status="skipped")
                        st.rerun()

        st.divider()

    # ── Manual Review Queue ─────────────────────────────────────────────────────────────────────
    manual_df = df[df["Status"] == "manual_review"]
    if not manual_df.empty:
        st.subheader(f"⚡ Manual Apply Queue ({len(manual_df)} jobs — documents ready)")
        st.success("Documents are already prepared! Just click the link and submit.")
        for _, row in manual_df.iterrows():
            score_val = f"{row['Score']:.0f}" if pd.notna(row["Score"]) else "—"
            with st.expander(f"📌 {row['Company']} — {row['Title']} (Score: {score_val})"):
                col_a, col_b = st.columns(2)
                col_a.markdown(f"**Company:** {row['Company']}")
                col_a.markdown(f"**Location:** {row['Location']}")
                col_b.markdown(f"**ATS:** {row['ATS']}")
                col_b.markdown(f"**Discovered:** {row['Discovered']}")
                if row["URL"]:
                    st.link_button("🔗 Open Job Posting", row["URL"])
                if row["Resume"]:
                    st.markdown(f"📄 **Resume ready:** `{row['Resume']}`")
                if row["Cover Letter"]:
                    st.markdown(f"📝 **Cover letter ready:** `{row['Cover Letter']}`")

                st.markdown("---")
                m_col1, m_col2 = st.columns(2)
                with m_col1:
                    if st.button("✅ Mark Applied", key=f"man_applied_{row['ID']}"):
                        update_job_field(row["ID"], status="applied",
                                        applied_at=_utcnow(),
                                        application_method="manual")
                        st.success("Marked as applied!")
                        st.rerun()
                with m_col2:
                    if st.button("🚫 Skip", key=f"man_skip_{row['ID']}"):
                        update_job_field(row["ID"], status="skipped")
                        st.rerun()
        st.divider()

    # ══════════════════════════════════════════════════════════════════════════════
    # ── 💼 INTERVIEW PIPELINE TRACKER ───────────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════════════
    interview_df = df[df["Status"].isin(["interview", "phone_screen", "technical", "system_design", "hr", "offer"])]
    applied_for_interview = df[df["Status"] == "applied"]

    if not interview_df.empty or not applied_for_interview.empty:
        st.subheader("💼 Interview Pipeline Tracker")

        interview_stages = {
            "applied": "📤 Applied",
            "phone_screen": "📞 Phone Screen",
            "technical": "💻 Technical Round",
            "system_design": "🏗️ System Design",
            "hr": "🤝 HR Round",
            "offer": "🎉 Offer!",
            "interview": "💼 Interview (stage TBD)",
        }

        all_active = pd.concat([interview_df, applied_for_interview.head(10)], ignore_index=True)

        for _, row in all_active.head(20).iterrows():
            stage_display = interview_stages.get(row["Status"], f"📋 {row['Status']}")
            with st.expander(f"{stage_display} — {row['Company']} | {row['Title'][:40]}"):
                i_col1, i_col2 = st.columns([2, 1])
                with i_col1:
                    st.markdown(f"**Company:** {row['Company']}")
                    st.markdown(f"**Applied:** {row['Applied On']}")
                    st.markdown(f"**Score:** {row['Score']:.0f}/100" if pd.notna(row["Score"]) else "**Score:** —")
                    if row["Notes"]:
                        st.markdown(f"**Notes:** {row['Notes']}")
                    if row["URL"]:
                        st.link_button("🔗 Job Posting", row["URL"])

                with i_col2:
                    st.markdown("**Update Stage:**")
                    new_stage = st.selectbox(
                        "Stage",
                        options=["applied", "phone_screen", "technical", "system_design", "hr", "offer", "rejected"],
                        index=list(interview_stages.keys()).index(row["Status"])
                            if row["Status"] in interview_stages else 0,
                        key=f"stage_{row['ID']}",
                        label_visibility="collapsed",
                    )
                    if st.button("Update", key=f"update_stage_{row['ID']}"):
                        update_job_field(row["ID"], status=new_stage)
                        st.success("Stage updated!")
                        st.rerun()

                    notes_input = st.text_input("Notes", value=row["Notes"] or "", key=f"notes_{row['ID']}", placeholder="e.g. Good vibes, salary discussed")
                    if st.button("Save Notes", key=f"save_notes_{row['ID']}"):
                        update_job_field(row["ID"], notes=notes_input)
                        st.success("Notes saved!")
                        st.rerun()

        st.divider()

    # ── All Jobs Filter & Table ───────────────────────────────────────────────────────────────────
    st.subheader("📋 All Jobs")

    filter_col1, filter_col2, filter_col3, filter_col4 = st.columns(4)
    with filter_col1:
        all_statuses = sorted(df["Status"].unique().tolist())
        smart_defaults = [s for s in ["applied", "manual_review", "scored"] if s in all_statuses]
        if not smart_defaults:
            smart_defaults = all_statuses[:3] if len(all_statuses) >= 3 else all_statuses
        status_filter = st.multiselect("Filter by Status", options=all_statuses, default=smart_defaults)
    with filter_col2:
        all_sources = sorted(df["Source"].unique().tolist())
        source_filter = st.multiselect("Filter by Source", options=all_sources)
    with filter_col3:
        all_ats = sorted(df["ATS"].dropna().unique().tolist())
        ats_filter = st.multiselect("Filter by ATS", options=all_ats)
    with filter_col4:
        min_score = st.slider("Minimum Score", 0, 100, 0)

    filtered = df.copy()
    if status_filter:
        filtered = filtered[filtered["Status"].isin(status_filter)]
    if source_filter:
        filtered = filtered[filtered["Source"].isin(source_filter)]
    if ats_filter:
        filtered = filtered[filtered["ATS"].isin(ats_filter)]
    if min_score > 0:
        filtered = filtered[filtered["Score"].fillna(0) >= min_score]

    display_cols = ["Company", "Title", "Score", "Status", "ATS", "Seniority", "Employment",
                    "Location", "Remote", "Posted At", "Applied On", "Discovered", "Interview Stage"]
    display_df = filtered[[c for c in display_cols if c in filtered.columns]].copy()

    url_df = filtered[[c for c in ["Company", "Title", "Score", "Status", "ATS", "Seniority",
                                    "Location", "Posted At", "URL", "Apply URL", "Discovered"] if c in filtered.columns]].copy()

    if display_df["Score"].notna().any():
        try:
            styled = display_df.style.background_gradient(
                subset=["Score"], cmap="RdYlGn", vmin=0, vmax=100
            )
            st.dataframe(styled, use_container_width="stretch")
        except ImportError:
            # matplotlib not available — fall back to plain dataframe
            st.dataframe(display_df, use_container_width="stretch")
    else:
        st.dataframe(display_df, use_container_width="stretch")

    if not url_df.empty and "URL" in url_df.columns:
        st.markdown("**🔗 Job Links:**")
        st.dataframe(
            url_df,
            use_container_width="stretch",
            column_config={
                "URL": st.column_config.LinkColumn("Job URL", display_text="View Posting"),
                "Apply URL": st.column_config.LinkColumn("Apply URL", display_text="Apply Direct"),
            },
            hide_index=True,
        )

    st.caption(f"Showing {len(filtered)} of {len(df)} jobs")
    st.divider()

    # ── Follow-up Reminders ─────────────────────────────────────────────────────────────────────
    if applied > 0:
        applied_df = df[(df["Status"] == "applied") & (df["Follow Up"] != "—")]
        if not applied_df.empty:
            st.subheader("🔔 Follow-up Reminders")
            st.dataframe(
                applied_df[["Company", "Title", "Applied On", "Follow Up", "Status"]],
                use_container_width="stretch",
            )
            st.divider()

    # ── Source Analytics ───────────────────────────────────────────────────────────────────────
    st.subheader("📊 Source Analytics")
    src_col1, src_col2 = st.columns(2)

    with src_col1:
        st.markdown("**Jobs per Source**")
        src_counts = df.groupby("Source").agg(
            Total=("ID", "count"),
            Avg_Score=("Score", "mean"),
            Applied=("Status", lambda x: (x == "applied").sum()),
        ).reset_index().sort_values("Total", ascending=False)
        # Ensure Avg_Score is numeric (float64) before rounding — avoids TypeError when all scores are NaN
        src_counts["Avg_Score"] = pd.to_numeric(src_counts["Avg_Score"], errors="coerce").round(1)
        st.dataframe(src_counts, use_container_width="stretch", hide_index=True)

    with src_col2:
        st.markdown("**Score Distribution by Source**")
        src_scored = df[df["Score"].notna()]
        if not src_scored.empty:
            fig_src = px.box(
                src_scored, x="Source", y="Score",
                color_discrete_sequence=["#60a5fa"],
                points="outliers",
            )
            fig_src.update_layout(height=280, margin=dict(t=10, b=0))
            st.plotly_chart(fig_src, use_container_width="stretch")

    st.divider()

# ── LLM Cache Stats ───────────────────────────────────────────────────────────────────────────────
try:
    from autoapply.scoring.cache import cache_stats
    stats = cache_stats()
    if stats["total_cached"] > 0:
        st.subheader("🧠 LLM Cache Stats")
        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("Cached Scores", stats["active"])
        cc2.metric("Cache Hits", stats["total_hits"], help="LLM calls saved")
        cc3.metric("Expired", stats["expired"])
        cc4.metric("Total Saved", stats["total_cached"])
        st.caption("Cache TTL: 7 days. Same job descriptions won't be re-scored.")
        st.divider()
except Exception:
    pass

# ── Pipeline Run Logs (DB) ─────────────────────────────────────────────────────────────────────
run_logs = get_run_logs()
if run_logs:
    st.subheader("🏃 Recent Pipeline Runs")
    log_rows = []
    for run in run_logs:
        duration = ""
        if run.completed_at and run.started_at:
            secs = (_naive(run.completed_at) - _naive(run.started_at)).seconds
            duration = f"{secs}s"
        log_rows.append({
            "Started": run.started_at.strftime("%b %d %H:%M") if run.started_at else "—",
            "Status": run.status,
            "Discovered": run.jobs_discovered,
            "Scored": run.jobs_scored,
            "Applied": run.jobs_applied,
            "Skipped": run.jobs_skipped,
            "LLM Calls": run.llm_calls_made,
            "Duration": duration,
        })
    st.dataframe(pd.DataFrame(log_rows), use_container_width="stretch")
    st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# ── 📜 LIVE PIPELINE LOGS ──────────────────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("📜 Live Pipeline Logs")
st.caption(
    f"Reading from `logs/autoapply.log` — refreshes every time you open the dashboard or click **🔄 Refresh Logs** below."
)

log_ctrl_col1, log_ctrl_col2, _ = st.columns([1, 1, 4])
with log_ctrl_col1:
    log_lines_n = st.selectbox("Lines to show", [50, 100, 200, 500], index=1, key="log_lines_select")
with log_ctrl_col2:
    if st.button("🔄 Refresh Logs", key="refresh_logs_btn"):
        st.rerun()

log_content = read_log_tail(log_lines_n)

if log_content:
    # ── Quick stats strip ────────────────────────────────────────────────────────────────
    errors       = log_content.count("| ERROR")
    info_count   = log_content.count("| INFO")
    discoveries  = log_content.count("Discovery |")
    scorings     = log_content.count("Scored |")
    applications = log_content.count("Application |")
    successes    = log_content.count("status=SUCCESS")
    failures     = log_content.count("status=FAILED")
    pipeline_starts = log_content.count("Pipeline started")
    pipeline_ends   = log_content.count("Pipeline complete")

    s1, s2, s3, s4, s5, s6, s7 = st.columns(7)
    s1.metric("📝 Log Lines", len(log_content.splitlines()))
    s2.metric("ℹ️ INFO", info_count)
    s3.metric("📊 Scored", scorings)
    s4.metric("🔍 Discovered", discoveries)
    s5.metric("✅ Applied OK", successes)
    s6.metric("❌ Failed", failures,
              delta=f"+{failures}" if failures else None, delta_color="inverse")
    s7.metric("🏁 Runs done", pipeline_ends)

    # ── Full log in scrollable code block ────────────────────────────────────────────
    st.code(log_content, language="text")
else:
    st.info("💤 No log entries yet. Run any pipeline to see live logs here.")

st.markdown("---")
st.caption("AutoAppy • Groq (primary) → Gemini (fallback) → Free tiers only • Ashby + Greenhouse + Lever + LinkedIn")
