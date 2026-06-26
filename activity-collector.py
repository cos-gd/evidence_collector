#!/usr/bin/env python3
"""
activity-collector.py
─────────────────────
Scans Claude Projects, git repos, and directories for a given time
period, and produces a structured Markdown evidence file ready to
commit to your GitHub evidence repo.

USAGE
    # Last full Mon–Sun week (default)
    python activity-collector.py

    # Specific date range
    python activity-collector.py --from 2026-06-16 --to 2026-06-22

    # Current week so far
    python activity-collector.py --week current

    # Custom config + output path
    python activity-collector.py --config ~/my-config.json --output ~/evidence/week.md

    # Add extra dirs/repos without editing config
    python activity-collector.py --dirs ~/code/project-x --repos ~/src/my-repo

    # Exclude specific directories from all scans
    python activity-collector.py --exclude ~/code/old-project ~/code/archive

    # Enrich report with Claude AI summaries (requires: pip install anthropic)
    python activity-collector.py --enrich

REQUIREMENTS
    Python 3.9+  ·  No external dependencies  ·  git must be on PATH
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Default configuration ────────────────────────────────────────────────────
# Override any of these in config.json (see config.example.json)

DEFAULT_CONFIG: dict = {
    # Filter git commits by author name or email fragment; None = include all
    "author": None,

    # Root of Claude Code / Cowork projects
    "claude_projects_dir": "~/Claude/Projects",

    # Additional plain directories to scan for recently modified files
    "scan_dirs": [],

    # Directories to recursively search for git repos (.git subdirectories)
    # Claude Projects dir is always included automatically
    "git_roots": [],

    # Directories to exclude entirely from all scans (git, dir, and Claude Projects)
    "exclude_dirs": [],

    # Where to write the output .md files (one per week)
    "output_dir": "~/Documents/weekly-evidence",

    # File extensions to report in directory scans (git tracks all files regardless)
    "file_extensions": [
        ".py", ".md", ".ts", ".tsx", ".js", ".jsx",
        ".sql", ".yaml", ".yml", ".json", ".toml",
        ".sh", ".bash", ".tf", ".go", ".rs", ".rb",
        ".ipynb", ".txt",
    ],

    # Directory / file patterns to skip
    "ignore_patterns": [
        ".git", "node_modules", "__pycache__", ".DS_Store",
        ".venv", "venv", "dist", "build", ".next", ".cache",
        "*.pyc", "*.pyo",
    ],

    # Max files to list per directory section (git sections are always complete)
    "max_files_per_dir": 40,

    # Claude API enrichment — generates short AI summaries for each active project
    # Requires: pip install anthropic  (disabled by default; enable via --enrich or config)
    "claude_enrich": False,
    "claude_model": "claude-sonnet-4-6",
    # Filenames (basename, case-insensitive) whose content is fed to Claude when changed
    "claude_enrich_files": ["REPORT.md", "README.md", "executive_summary.md"],
    # Explicit API key; None = use ANTHROPIC_API_KEY env var
    "claude_api_key": None,
}


# ── Date utilities ───────────────────────────────────────────────────────────

def _last_monday(ref: date | None = None) -> date:
    ref = ref or date.today()
    return ref - timedelta(days=ref.weekday() + 7)


def _last_sunday(ref: date | None = None) -> date:
    return _last_monday(ref) + timedelta(days=6)


def _this_monday(ref: date | None = None) -> date:
    ref = ref or date.today()
    return ref - timedelta(days=ref.weekday())


def _parse_date(s: str) -> date:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Cannot parse date '{s}'. Use YYYY-MM-DD.")


def _week_label(d_from: date, d_to: date) -> str:
    iso = d_from.isocalendar()
    return f"{d_from.strftime('%b %d')}–{d_to.strftime('%b %d, %Y')} (W{iso[1]:02d})"


def _iso_week_id(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _fmt_date(iso_date: str) -> str:
    try:
        return datetime.strptime(iso_date[:10], "%Y-%m-%d").strftime("%b %d")
    except Exception:
        return iso_date


# ── Config ───────────────────────────────────────────────────────────────────

def load_config(config_path: Optional[str]) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if config_path:
        p = Path(config_path).expanduser()
        if p.exists():
            with open(p, encoding="utf-8") as f:
                cfg.update(json.load(f))
            print(f"[config] Loaded: {p}", file=sys.stderr)
        else:
            print(f"[warn] Config file not found: {p} — using defaults", file=sys.stderr)
    return cfg


def _expand(cfg: dict) -> dict:
    """Expand ~ in all path values."""
    for key in ("claude_projects_dir", "output_dir"):
        if cfg.get(key):
            cfg[key] = str(Path(cfg[key]).expanduser())
    cfg["scan_dirs"]   = [str(Path(d).expanduser()) for d in cfg.get("scan_dirs",   [])]
    cfg["git_roots"]   = [str(Path(d).expanduser()) for d in cfg.get("git_roots",   [])]
    cfg["exclude_dirs"] = [str(Path(d).expanduser().resolve()) for d in cfg.get("exclude_dirs", [])]
    return cfg


# ── Git helpers ──────────────────────────────────────────────────────────────

def _run(cmd: list[str], cwd: str | None = None) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=30)
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _is_excluded(path: Path, exclude_dirs: list[str]) -> bool:
    resolved = str(path.resolve())
    return any(resolved == ex or resolved.startswith(ex + os.sep) for ex in exclude_dirs)


# ── Claude API enrichment ────────────────────────────────────────────────────

def _find_target_files(files_changed: list[str], target_names: list[str]) -> list[str]:
    """Return changed files whose basename matches any pattern in target_names (case-insensitive, wildcards ok)."""
    patterns = [n.lower() for n in target_names]
    return [
        f for f in files_changed
        if any(fnmatch.fnmatch(Path(f).name.lower(), p) for p in patterns)
    ]


def enrich_with_claude(
    repo_path: Path,
    commits: list[dict],
    files_changed: list[str],
    cfg: dict,
) -> Optional[str]:
    """Return a short AI-generated activity summary, or None if not applicable/available."""
    try:
        import anthropic as _anthropic  # noqa: PLC0415
    except ImportError:
        print(
            "[warn] 'anthropic' package not installed; skipping enrichment "
            "(run: pip install anthropic)",
            file=sys.stderr,
        )
        return None

    target_names = cfg.get("claude_enrich_files", ["REPORT.md", "README.md", "executive_summary.md"])
    matched = _find_target_files(files_changed, target_names)
    if not matched:
        return None

    file_sections: list[str] = []
    for rel_path in matched:
        try:
            content = (repo_path / rel_path).read_text(errors="replace")[:4000]
            file_sections.append(f"### {rel_path}\n\n{content}")
        except OSError:
            pass

    if not file_sections:
        return None

    commit_lines = "\n".join(
        f"- {c['date']} ({c['author_name']}): {c['subject']}"
        for c in commits[:20]
    )
    user_content = (
        f"Commits this week:\n{commit_lines}\n\n"
        f"Changed documentation files:\n\n"
        + "\n\n".join(file_sections)
    )

    model   = cfg.get("claude_model", "claude-sonnet-4-6")
    api_key = cfg.get("claude_api_key")

    try:
        client = _anthropic.Anthropic(api_key=api_key) if api_key else _anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=[{
                "type": "text",
                "text": (
                    "You are a technical writing assistant that produces concise "
                    "activity summaries for engineering weekly reports. "
                    "Focus on outcomes and impact, not implementation details. Be specific."
                ),
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
        )
        return next((b.text for b in response.content if b.type == "text"), None)
    except Exception as exc:
        print(f"[warn] Claude enrichment failed for {repo_path.name}: {exc}", file=sys.stderr)
        return None


def _preflight_claude(cfg: dict) -> bool:
    """Validate that the Anthropic client and API key work. Returns False and prints an error on failure."""
    try:
        import anthropic as _anthropic  # noqa: PLC0415
    except ImportError:
        print(
            "[error] --enrich requires the 'anthropic' package: pip install anthropic",
            file=sys.stderr,
        )
        return False

    api_key = cfg.get("claude_api_key")
    try:
        client = _anthropic.Anthropic(api_key=api_key) if api_key else _anthropic.Anthropic()
        client.models.list()
        print("[activity-collector] Claude API key validated.", file=sys.stderr)
        return True
    except Exception as exc:
        print(f"[error] Claude API pre-flight check failed: {exc}", file=sys.stderr)
        return False


def find_git_repos(roots: list[str], exclude_dirs: list[str] | None = None, ignore_patterns: list[str] | None = None) -> list[Path]:
    """Recursively find all git repos under the given root directories."""
    exclude_dirs = exclude_dirs or []
    ignores = set(ignore_patterns or [])
    repos, seen = [], set()
    for root in roots:
        p = Path(root)
        if not p.exists() or _is_excluded(p, exclude_dirs):
            continue
        if (p / ".git").exists():
            key = str(p.resolve())
            if key not in seen:
                repos.append(p)
                seen.add(key)
        for git_dir in p.rglob(".git"):
            if git_dir.is_dir():
                repo = git_dir.parent
                if _is_excluded(repo, exclude_dirs):
                    continue
                if ignores and any(part in ignores for part in repo.parts):
                    continue
                key = str(repo.resolve())
                if key not in seen:
                    repos.append(repo)
                    seen.add(key)
    return sorted(repos)


def collect_git_activity(
    repo: Path,
    since: date,
    until: date,
    author: str | None = None,
) -> dict:
    """Return commit list + file stats for the given period."""
    # git --after is exclusive (commits strictly after midnight of that date),
    # so subtract one day to make since inclusive. --before gets +1 for the same reason.
    after_str = (since - timedelta(days=1)).strftime("%Y-%m-%d")
    until_str = (until + timedelta(days=1)).strftime("%Y-%m-%d")

    log_cmd = [
        "git", "log",
        f"--after={after_str}",
        f"--before={until_str}",
        "--format=%h|%ad|%an|%ae|%s",
        "--date=short",
    ]
    if author:
        log_cmd += [f"--author={author}"]

    raw = _run(log_cmd, cwd=str(repo))
    commits = []
    for line in raw.splitlines():
        parts = line.split("|", 4)
        if len(parts) == 5:
            commits.append({
                "hash":         parts[0],
                "date":         parts[1],
                "author_name":  parts[2],
                "author_email": parts[3],
                "subject":      parts[4],
            })

    # Numstat for file-level detail
    files_changed: set[str] = set()
    total_add = total_del = 0
    if commits:
        ns_cmd = [
            "git", "log",
            f"--after={after_str}",
            f"--before={until_str}",
            "--numstat", "--format=",
        ]
        if author:
            ns_cmd += [f"--author={author}"]
        for line in _run(ns_cmd, cwd=str(repo)).splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                try:
                    total_add += int(parts[0]) if parts[0] != "-" else 0
                    total_del += int(parts[1]) if parts[1] != "-" else 0
                    files_changed.add(parts[2])
                except ValueError:
                    pass

    remote = _run(["git", "remote", "get-url", "origin"], cwd=str(repo))

    return {
        "repo":   repo,
        "remote": remote,
        "commits": commits,
        "stats": {
            "files_changed":   sorted(files_changed),
            "total_additions": total_add,
            "total_deletions": total_del,
        },
    }


# ── Directory scan ───────────────────────────────────────────────────────────

def collect_dir_activity(
    dir_path: str,
    since: date,
    until: date,
    cfg: dict,
) -> dict:
    p = Path(dir_path)
    if not p.exists():
        return {"path": dir_path, "files": [], "total_found": 0, "error": "not found"}

    since_ts = datetime.combine(since, datetime.min.time()).timestamp()
    until_ts = datetime.combine(until + timedelta(days=1), datetime.min.time()).timestamp()
    exts     = set(cfg.get("file_extensions", []))
    ignores  = set(cfg.get("ignore_patterns", []))

    def skip(path: Path) -> bool:
        return any(part in ignores for part in path.parts)

    found = []
    try:
        for dirpath, dirs, files in os.walk(str(p)):
            dirs[:] = [d for d in dirs if d not in ignores]
            for fname in files:
                f = Path(dirpath) / fname
                if exts and f.suffix not in exts:
                    continue
                try:
                    mtime = f.stat().st_mtime
                    if since_ts <= mtime < until_ts:
                        found.append({
                            "path":     str(f.relative_to(p)),
                            "modified": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
                        })
                except OSError:
                    pass
    except PermissionError:
        return {"path": dir_path, "files": [], "total_found": 0, "error": "permission denied"}

    found.sort(key=lambda x: x["modified"], reverse=True)
    limit = cfg.get("max_files_per_dir", 40)
    return {
        "path":        dir_path,
        "files":       found[:limit],
        "total_found": len(found),
        "truncated":   len(found) > limit,
    }


# ── Claude Projects ──────────────────────────────────────────────────────────

def _read_claude_md(project_dir: Path) -> str:
    """Extract a short description from CLAUDE.md."""
    claude_md = project_dir / "CLAUDE.md"
    if not claude_md.exists():
        # Fall back to README.md
        claude_md = project_dir / "README.md"
        if not claude_md.exists():
            return ""
    try:
        lines, desc, in_body = claude_md.read_text(errors="replace").splitlines(), [], False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                if desc:
                    break
                in_body = True
                continue
            if (in_body or not stripped.startswith("#")) and stripped:
                if not stripped.startswith(("```", "---", "<!--")):
                    desc.append(stripped)
                    if len(desc) >= 3:
                        break
        return " ".join(desc)[:320]
    except Exception:
        return ""


def _project_type(path: Path) -> str:
    has_claude = (path / "CLAUDE.md").exists()
    has_git    = (path / ".git").exists()
    if has_claude and has_git: return "claude-code-git"
    if has_claude:             return "claude-code"
    if has_git:                return "git"
    return "directory"


def collect_claude_projects(
    claude_dir: str,
    since: date,
    until: date,
    cfg: dict,
) -> list[dict]:
    p = Path(claude_dir)
    if not p.exists():
        print(f"[warn] Claude Projects dir not found: {p}", file=sys.stderr)
        return []

    exclude_dirs = cfg.get("exclude_dirs", [])
    projects = []
    for child in sorted(p.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if _is_excluded(child, exclude_dirs):
            continue

        ptype       = _project_type(child)
        description = _read_claude_md(child)
        git_data    = collect_git_activity(child, since, until, cfg.get("author")) \
                      if (child / ".git").exists() else None
        file_data   = collect_dir_activity(str(child), since, until, cfg)

        has_git   = git_data and git_data.get("commits")
        has_files = file_data and file_data.get("files")
        if not has_git and not has_files:
            continue

        enrichment = None
        if cfg.get("claude_enrich") and has_git:
            fc = git_data.get("stats", {}).get("files_changed", [])
            enrichment = enrich_with_claude(child, git_data["commits"], fc, cfg)
            if enrichment:
                print(f"[activity-collector]   [AI] enriched {child.name}", file=sys.stderr)

        projects.append({
            "name":        child.name,
            "path":        str(child),
            "type":        ptype,
            "description": description,
            "git":         git_data,
            "files":       file_data,
            "enrichment":  enrichment,
        })

    return projects


# ── Markdown renderer ────────────────────────────────────────────────────────

def _commit_table(commits: list[dict]) -> list[str]:
    lines = ["| Date | Hash | Message |", "|------|------|---------|"]
    for c in commits:
        lines.append(f"| {_fmt_date(c['date'])} | `{c['hash']}` | {c['subject']} |")
    return lines


def _file_list(files: list[dict], cap: int = 20) -> list[str]:
    lines = [f"- `{f['path']}` — {f['modified']}" for f in files[:cap]]
    if len(files) > cap:
        lines.append(f"- *(+{len(files)-cap} more files)*")
    return lines


def _project_section(proj: dict) -> list[str]:
    commits  = (proj.get("git") or {}).get("commits", [])
    stats    = (proj.get("git") or {}).get("stats", {})
    files    = (proj.get("files") or {}).get("files", [])
    remote   = (proj.get("git") or {}).get("remote", "")

    type_label = {
        "claude-code-git": "Claude Code · git-backed",
        "claude-code":     "Claude Code",
        "git":             "git",
        "directory":       "directory",
    }.get(proj["type"], proj["type"])

    lines = [f"### {proj['name']}", ""]
    if proj.get("description"):
        lines += [f"> {proj['description']}", ""]

    lines.append(f"**Type:** {type_label}<br>")
    if remote:
        lines.append(f"**Remote:** {remote}<br>")
    if commits:
        dates = sorted(c["date"] for c in commits)
        lines.append(f"**Active:** {_fmt_date(dates[0])} → {_fmt_date(dates[-1])}<br>")
        lines.append(f"**Commits:** {len(commits)}<br>")
    fc = stats.get("files_changed", [])
    if fc:
        lines.append(f"**Files changed:** {len(fc)}"
                     f" (+{stats.get('total_additions',0)} / −{stats.get('total_deletions',0)} lines)<br>")
    elif files:
        lines.append(f"**Files modified:** {len(files)}<br>")
    lines.append("")

    if commits:
        lines += ["#### Commits", ""] + _commit_table(commits) + [""]
    if fc:
        lines += ["#### Files Changed", ""] + [f"- `{f}`" for f in fc[:25]]
        if len(fc) > 25:
            lines.append(f"- *(+{len(fc)-25} more)*")
        lines.append("")
    elif files:
        lines += ["#### Files Modified", ""] + _file_list(files) + [""]

    if proj.get("enrichment"):
        lines += ["#### AI Summary", "", proj["enrichment"], ""]

    lines += [
        "#### Digest Notes",
        "",
        "*Add context: what problem, what outcome, next steps.*",
        "",
        "---",
        "",
    ]
    return lines


def render_markdown(
    claude_projects: list[dict],
    other_dirs: list[dict],
    other_repos: list[dict],
    cfg: dict,
    date_from: date,
    date_to: date,
) -> str:
    week_id   = _iso_week_id(date_from)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    author    = cfg.get("author") or "all"
    lines: list[str] = []

    # ── Header ─────────────────────────────────────────────────────────────
    lines += [
        f"# Weekly Activity Evidence — {_week_label(date_from, date_to)}",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Period | {date_from} → {date_to} |",
        f"| ISO Week | {week_id} |",
        f"| Author filter | `{author}` |",
        f"| Generated | {generated} |",
        f"| Claude Projects | `{cfg.get('claude_projects_dir', 'n/a')}` |",
        "",
        "> **How to use:** review each section, fill in the *Digest Notes* fields,",
        "> then commit this file to your evidence repo. Reference it in the next",
        "> weekly digest run.",
        "",
        "---",
        "",
    ]

    # ── AI-Native Development ───────────────────────────────────────────────
    if claude_projects:
        lines += ["## AI-Native Development (Claude Code / Cowork)", ""]
        for proj in claude_projects:
            lines += _project_section(proj)

    # ── Other git repos ─────────────────────────────────────────────────────
    active_repos = [r for r in other_repos if r.get("commits")]
    if active_repos:
        lines += ["## Other Git Repositories", ""]
        for rd in active_repos:
            repo    = rd["repo"]
            commits = rd.get("commits", [])
            stats   = rd.get("stats", {})
            remote  = rd.get("remote", "")
            dates   = sorted(c["date"] for c in commits)
            fc      = stats.get("files_changed", [])

            lines += [f"### {repo.name}", ""]
            lines.append(f"**Path:** `{repo}`<br>")
            if remote:
                lines.append(f"**Remote:** {remote}<br>")
            lines += [
                f"**Active:** {_fmt_date(dates[0])} → {_fmt_date(dates[-1])}<br>",
                f"**Commits:** {len(commits)}<br>",
                "",
            ]
            lines += _commit_table(commits) + [""]
            if fc:
                lines += ["#### Files Changed", ""] + [f"- `{f}`" for f in fc[:20]]
                if len(fc) > 20:
                    lines.append(f"- *(+{len(fc)-20} more)*")
                lines.append("")
            if rd.get("enrichment"):
                lines += ["#### AI Summary", "", rd["enrichment"], ""]
            lines += ["#### Digest Notes", "", "*Add context here.*", "", "---", ""]

    # ── Other directories ────────────────────────────────────────────────────
    active_dirs = [d for d in other_dirs if d.get("files")]
    if active_dirs:
        lines += ["## Other Directories", ""]
        for dd in active_dirs:
            name  = Path(dd["path"]).name
            files = dd.get("files", [])
            lines += [
                f"### {name}", "",
                f"**Path:** `{dd['path']}`<br>",
                f"**Files modified:** {dd['total_found']}<br>",
                "",
            ]
            lines += _file_list(files) + [""]
            lines += ["#### Digest Notes", "", "*Add context here.*", "", "---", ""]

    # ── Summary table ────────────────────────────────────────────────────────
    lines += ["## Summary", ""]
    lines += [
        "| Project | Type | Commits | Files | First Active | Last Active |",
        "|---------|------|---------|-------|-------------|------------|",
    ]

    def _summary_row(name, ptype, commits, file_count, files_or_dates):
        if commits:
            dates = sorted(c["date"] for c in commits)
            first, last = _fmt_date(dates[0]), _fmt_date(dates[-1])
        elif files_or_dates:
            dates = sorted(f["modified"][:10] for f in files_or_dates)
            first, last = _fmt_date(dates[0]), _fmt_date(dates[-1])
        else:
            first = last = "—"
        return f"| {name} | {ptype} | {len(commits)} | {file_count} | {first} | {last} |"

    type_short = {
        "claude-code-git": "Claude Code",
        "claude-code":     "Claude Code",
        "git":             "git",
        "directory":       "dir",
    }
    for proj in claude_projects:
        commits = (proj.get("git") or {}).get("commits", [])
        fc      = (proj.get("git") or {}).get("stats", {}).get("files_changed", [])
        files   = (proj.get("files") or {}).get("files", [])
        lines.append(_summary_row(
            proj["name"],
            type_short.get(proj["type"], proj["type"]),
            commits,
            len(fc) or len(files),
            files,
        ))
    for rd in active_repos:
        commits = rd.get("commits", [])
        fc      = rd.get("stats", {}).get("files_changed", [])
        lines.append(_summary_row(rd["repo"].name, "git", commits, len(fc), []))
    for dd in active_dirs:
        files = dd.get("files", [])
        lines.append(_summary_row(Path(dd["path"]).name, "dir", [], dd["total_found"], files))

    lines += [
        "",
        "---",
        "",
        f"*Generated by `activity-collector.py` — {generated}*",
        "",
    ]

    return "\n".join(line.rstrip() for line in lines)


# ── CLI entry point ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Collect weekly activity evidence from Claude Projects, "
                    "git repos, and directories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD",
                   help="Start date (inclusive). Default: last Monday.")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD",
                   help="End date (inclusive). Default: last Sunday.")
    p.add_argument("--week", choices=["last", "current"], default="last",
                   help="Shortcut for date range (default: last).")
    p.add_argument("--config", metavar="PATH",
                   help="JSON config file path. Defaults to config.json next to this script.")
    p.add_argument("--output", metavar="PATH",
                   help="Output .md file path (overrides config output_dir).")
    p.add_argument("--author", metavar="NAME_OR_EMAIL",
                   help="Filter git commits by author name or email.")
    p.add_argument("--dirs", nargs="+", metavar="DIR",
                   help="Extra directories to scan for modified files.")
    p.add_argument("--repos", nargs="+", metavar="DIR",
                   help="Extra directories to recursively search for git repos.")
    p.add_argument("--exclude", nargs="+", metavar="DIR",
                   help="Directories to exclude from all scans.")
    p.add_argument("--enrich", action="store_true",
                   help="Enrich report with Claude AI summaries (requires: pip install anthropic).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print summary without writing the output file.")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    # Date range
    if args.date_from and args.date_to:
        date_from = _parse_date(args.date_from)
        date_to   = _parse_date(args.date_to)
    elif args.week == "current":
        date_from = _this_monday()
        date_to   = date.today()
    else:
        date_from = _last_monday()
        date_to   = _last_sunday()

    print(f"[activity-collector] Period : {date_from} → {date_to}", file=sys.stderr)

    # Config — fall back to config.json next to this script if --config not given
    config_path = args.config or str(Path(__file__).parent / "config.json")
    cfg = _expand(load_config(config_path))
    if args.author:  cfg["author"] = args.author
    if args.dirs:    cfg["scan_dirs"]    += [str(Path(d).expanduser()) for d in args.dirs]
    if args.repos:   cfg["git_roots"]    += [str(Path(d).expanduser()) for d in args.repos]
    if args.exclude: cfg["exclude_dirs"] += [str(Path(d).expanduser().resolve()) for d in args.exclude]
    if args.enrich:  cfg["claude_enrich"] = True

    if cfg.get("claude_enrich") and not _preflight_claude(cfg):
        sys.exit(1)

    # Claude Projects
    claude_dir = cfg.get("claude_projects_dir", "")
    print(f"[activity-collector] Claude : {claude_dir}", file=sys.stderr)
    claude_projects = collect_claude_projects(claude_dir, date_from, date_to, cfg) \
                      if claude_dir else []
    print(f"[activity-collector]   → {len(claude_projects)} active project(s)", file=sys.stderr)

    # Other git repos (exclude Claude dir to avoid double-counting)
    claude_abs  = str(Path(claude_dir).resolve()) if claude_dir else None
    other_roots = [r for r in cfg.get("git_roots", [])
                   if str(Path(r).resolve()) != claude_abs]
    all_repos   = find_git_repos(other_roots, cfg.get("exclude_dirs", []), cfg.get("ignore_patterns", []))
    print(f"[activity-collector] Repos  : {len(all_repos)} found", file=sys.stderr)
    other_repos = [collect_git_activity(r, date_from, date_to, cfg.get("author"))
                   for r in all_repos]
    active_cnt  = sum(1 for r in other_repos if r.get("commits"))
    print(f"[activity-collector]   → {active_cnt} with activity", file=sys.stderr)

    if cfg.get("claude_enrich"):
        for rd in other_repos:
            if rd.get("commits"):
                fc = rd.get("stats", {}).get("files_changed", [])
                rd["enrichment"] = enrich_with_claude(rd["repo"], rd["commits"], fc, cfg)
                if rd.get("enrichment"):
                    print(f"[activity-collector]   [AI] enriched {rd['repo'].name}", file=sys.stderr)

    # Other directories
    other_dirs = []
    exclude_dirs = cfg.get("exclude_dirs", [])
    for d in cfg.get("scan_dirs", []):
        if claude_abs and str(Path(d).resolve()).startswith(claude_abs):
            continue
        if _is_excluded(Path(d), exclude_dirs):
            continue
        print(f"[activity-collector] Dir    : {d}", file=sys.stderr)
        other_dirs.append(collect_dir_activity(d, date_from, date_to, cfg))

    # Render
    md = render_markdown(claude_projects, other_dirs, other_repos,
                         cfg, date_from, date_to)

    # Output path
    if args.output:
        out_path = Path(args.output).expanduser()
    else:
        out_dir = Path(cfg.get("output_dir", ".")).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"activity-{_iso_week_id(date_from)}.md"

    if args.dry_run:
        print(f"\n[dry-run] Would write  : {out_path}")
        print(f"[dry-run] Claude projects : {len(claude_projects)}")
        print(f"[dry-run] Other repos     : {active_cnt}")
        print(f"[dry-run] Other dirs      : {sum(1 for d in other_dirs if d.get('files'))}")
        return

    out_path.write_text(md, encoding="utf-8")
    print(f"\n[activity-collector] ✓ Written: {out_path}", file=sys.stderr)
    # Print path to stdout so it can be captured: $(python activity-collector.py)
    print(out_path)


if __name__ == "__main__":
    main()
