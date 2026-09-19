"""
GitHub Actions workflow trigger utility.

Allows the Streamlit dashboard (running on Streamlit Cloud, where subprocess
is restricted) to trigger GitHub Actions workflows via the GitHub REST API.

Required secret: GH_PAT (Personal Access Token with `repo` + `actions` scope)
Required env:    GITHUB_REPO = "owner/repo"  (e.g. "Tamilarasan20225/autoapply")

If GH_PAT is not set, falls back to subprocess (local mode).
"""

import os
import requests
from typing import Optional


def _get_github_config() -> tuple[str | None, str | None]:
    """Return (token, repo) from environment."""
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO", "").strip()
    return token, repo


def can_trigger_github() -> bool:
    """True if GH_PAT and GITHUB_REPO are both configured."""
    token, repo = _get_github_config()
    return bool(token and repo and "/" in repo)


def trigger_workflow(
    workflow_file: str,
    inputs: dict | None = None,
    ref: str = "main",
) -> tuple[bool, str]:
    """
    Trigger a GitHub Actions workflow_dispatch event.

    Args:
        workflow_file: Filename of the workflow (e.g. "daily-pipeline.yml")
        inputs: Optional dict of workflow inputs (must match workflow's on.workflow_dispatch.inputs)
        ref: Branch/tag to run on (default: main)

    Returns:
        (success: bool, message: str)
    """
    token, repo = _get_github_config()
    if not token or not repo:
        return False, "GH_PAT and GITHUB_REPO environment variables are required to trigger workflows remotely."

    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"
    payload: dict = {"ref": ref}
    if inputs:
        payload["inputs"] = inputs

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )
        if resp.status_code == 204:
            return True, f"✅ Workflow `{workflow_file}` triggered on `{ref}`. Check GitHub Actions for progress."
        elif resp.status_code == 422:
            return False, f"❌ Workflow not found or ref `{ref}` invalid. Make sure the workflow has `workflow_dispatch:` enabled."
        elif resp.status_code == 401:
            return False, "❌ GH_PAT is invalid or expired. Generate a new token with `repo` + `actions` scope."
        elif resp.status_code == 403:
            return False, "❌ GH_PAT lacks required permissions. Enable `repo` and `actions` write scopes."
        else:
            return False, f"❌ GitHub API returned {resp.status_code}: {resp.text[:200]}"
    except requests.exceptions.ConnectionError:
        return False, "❌ Could not reach GitHub API — check your internet connection."
    except requests.exceptions.Timeout:
        return False, "❌ GitHub API request timed out."
    except Exception as e:
        return False, f"❌ Unexpected error: {e}"


def get_workflow_runs(workflow_file: str, limit: int = 5) -> list[dict]:
    """
    Fetch recent runs for a workflow — used to show status in the dashboard.

    Returns list of dicts: [{"id", "status", "conclusion", "created_at", "html_url"}]
    """
    token, repo = _get_github_config()
    if not token or not repo:
        return []

    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs"
    try:
        resp = requests.get(
            url,
            params={"per_page": limit},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        runs = resp.json().get("workflow_runs", [])
        return [
            {
                "id": r["id"],
                "status": r["status"],         # queued | in_progress | completed
                "conclusion": r.get("conclusion"),  # success | failure | cancelled | None
                "created_at": r["created_at"],
                "html_url": r["html_url"],
                "name": r.get("display_title", r.get("name", "")),
            }
            for r in runs
        ]
    except Exception:
        return []


def get_schedule_enabled(config: dict) -> bool:
    """Return whether the daily schedule is enabled in config.yaml."""
    return config.get("scheduler", {}).get("enabled", False)


def set_schedule_enabled(enabled: bool, config_path: str = "config.yaml") -> bool:
    """
    Toggle scheduler.enabled in config.yaml.
    Returns True on success.
    """
    try:
        import yaml
        from pathlib import Path
        p = Path(config_path)
        with open(p) as f:
            cfg = yaml.safe_load(f)
        if "scheduler" not in cfg:
            cfg["scheduler"] = {}
        cfg["scheduler"]["enabled"] = enabled
        with open(p, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return True
    except Exception:
        return False
