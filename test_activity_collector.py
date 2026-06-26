#!/usr/bin/env python3
"""Tests for activity-collector.py"""
import importlib.util
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Load hyphenated module ────────────────────────────────────────────────────

_spec = importlib.util.spec_from_file_location(
    "activity_collector",
    Path(__file__).parent / "activity-collector.py",
)
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)


# ── Date utilities ────────────────────────────────────────────────────────────

class TestLastMonday:
    def test_from_monday(self):
        assert ac._last_monday(date(2026, 6, 29)) == date(2026, 6, 22)  # Mon → prev Mon

    def test_from_wednesday(self):
        assert ac._last_monday(date(2026, 6, 25)) == date(2026, 6, 15)

    def test_from_sunday(self):
        assert ac._last_monday(date(2026, 6, 28)) == date(2026, 6, 15)


class TestLastSunday:
    def test_is_six_days_after_last_monday(self):
        ref = date(2026, 6, 25)
        assert ac._last_sunday(ref) == ac._last_monday(ref) + timedelta(days=6)


class TestThisMonday:
    def test_from_monday(self):
        assert ac._this_monday(date(2026, 6, 29)) == date(2026, 6, 29)

    def test_from_thursday(self):
        # June 25 is Thursday; this-Monday is June 22
        assert ac._this_monday(date(2026, 6, 25)) == date(2026, 6, 22)


class TestParseDate:
    def test_iso(self):
        assert ac._parse_date("2026-06-25") == date(2026, 6, 25)

    def test_dmy(self):
        assert ac._parse_date("25/06/2026") == date(2026, 6, 25)

    def test_mdy(self):
        assert ac._parse_date("06/25/2026") == date(2026, 6, 25)

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Cannot parse date"):
            ac._parse_date("not-a-date")


class TestWeekLabel:
    def test_contains_dates(self):
        label = ac._week_label(date(2026, 6, 22), date(2026, 6, 28))
        assert "Jun 22" in label
        assert "Jun 28" in label
        assert "2026" in label

    def test_contains_week_number(self):
        label = ac._week_label(date(2026, 6, 22), date(2026, 6, 28))
        assert "W" in label


class TestIsoWeekId:
    def test_format(self):
        wid = ac._iso_week_id(date(2026, 6, 22))
        assert wid.startswith("2026-W")

    def test_week_number(self):
        # June 22, 2026 is ISO week 26
        assert ac._iso_week_id(date(2026, 6, 22)) == "2026-W26"


class TestFmtDate:
    def test_valid(self):
        assert ac._fmt_date("2026-06-25") == "Jun 25"

    def test_with_time_suffix(self):
        assert ac._fmt_date("2026-06-25 14:30") == "Jun 25"

    def test_invalid_passthrough(self):
        assert ac._fmt_date("bad") == "bad"


# ── Config ────────────────────────────────────────────────────────────────────

class TestExpand:
    def test_expands_tilde_in_claude_projects_dir(self):
        cfg = dict(ac.DEFAULT_CONFIG)
        cfg["claude_projects_dir"] = "~/Claude/Projects"
        cfg["scan_dirs"] = []
        cfg["git_roots"] = []
        cfg["exclude_dirs"] = []
        expanded = ac._expand(cfg)
        assert "~" not in expanded["claude_projects_dir"]

    def test_expands_tilde_in_scan_dirs(self):
        cfg = dict(ac.DEFAULT_CONFIG)
        cfg["scan_dirs"] = ["~/work/project"]
        cfg["git_roots"] = []
        cfg["exclude_dirs"] = []
        expanded = ac._expand(cfg)
        assert all("~" not in d for d in expanded["scan_dirs"])

    def test_resolves_exclude_dirs(self, tmp_path):
        cfg = dict(ac.DEFAULT_CONFIG)
        cfg["scan_dirs"] = []
        cfg["git_roots"] = []
        cfg["exclude_dirs"] = [str(tmp_path)]
        expanded = ac._expand(cfg)
        assert all(os.path.isabs(d) for d in expanded["exclude_dirs"])


# ── _is_excluded ─────────────────────────────────────────────────────────────

class TestIsExcluded:
    def test_exact_match(self, tmp_path):
        d = tmp_path / "foo"
        d.mkdir()
        assert ac._is_excluded(d, [str(d.resolve())])

    def test_prefix_match(self, tmp_path):
        parent = tmp_path / "foo"
        parent.mkdir()
        child = parent / "bar"
        child.mkdir()
        assert ac._is_excluded(child, [str(parent.resolve())])

    def test_no_match(self, tmp_path):
        d = tmp_path / "foo"
        d.mkdir()
        assert not ac._is_excluded(d, [str((tmp_path / "bar").resolve())])

    def test_partial_name_not_matched(self, tmp_path):
        d = tmp_path / "foobar"
        d.mkdir()
        assert not ac._is_excluded(d, [str((tmp_path / "foo").resolve())])


# ── collect_dir_activity ──────────────────────────────────────────────────────

def _touch_now(path: Path):
    path.write_text("x")
    now = time.time()
    os.utime(str(path), (now, now))


def _make_cfg(**overrides):
    cfg = {
        "file_extensions": [".py", ".md"],
        "ignore_patterns": [".venv", "venv", "node_modules", "__pycache__", ".git"],
        "max_files_per_dir": 40,
    }
    cfg.update(overrides)
    return cfg


class TestCollectDirActivity:
    def test_nonexistent_dir_returns_error(self):
        result = ac.collect_dir_activity("/no/such/dir", date.today(), date.today(), _make_cfg())
        assert result["error"] == "not found"
        assert result["files"] == []

    def test_finds_file_modified_today(self, tmp_path):
        f = tmp_path / "hello.py"
        _touch_now(f)
        result = ac.collect_dir_activity(str(tmp_path), date.today(), date.today(), _make_cfg())
        assert any("hello.py" in item["path"] for item in result["files"])

    def test_skips_file_with_wrong_extension(self, tmp_path):
        f = tmp_path / "readme.txt"
        _touch_now(f)
        result = ac.collect_dir_activity(str(tmp_path), date.today(), date.today(), _make_cfg())
        assert result["files"] == []

    def test_skips_old_file(self, tmp_path):
        f = tmp_path / "old.py"
        f.write_text("old")
        old_ts = datetime(2020, 1, 1).timestamp()
        os.utime(str(f), (old_ts, old_ts))
        result = ac.collect_dir_activity(str(tmp_path), date.today(), date.today(), _make_cfg())
        assert result["files"] == []

    def test_venv_at_top_level_excluded(self, tmp_path):
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        _touch_now(venv / "stuff.py")
        result = ac.collect_dir_activity(str(tmp_path), date.today(), date.today(), _make_cfg())
        assert result["files"] == []

    def test_venv_nested_deep_excluded(self, tmp_path):
        """Regression: .venv inside a subdirectory must not be scanned."""
        nested = tmp_path / "labs" / "lab-01" / ".venv" / "lib"
        nested.mkdir(parents=True)
        _touch_now(nested / "evil.py")
        good = tmp_path / "main.py"
        _touch_now(good)
        result = ac.collect_dir_activity(str(tmp_path), date.today(), date.today(), _make_cfg())
        paths = [item["path"] for item in result["files"]]
        assert any("main.py" in p for p in paths)
        assert not any(".venv" in p for p in paths)

    def test_node_modules_excluded(self, tmp_path):
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        _touch_now(nm / "index.py")
        result = ac.collect_dir_activity(str(tmp_path), date.today(), date.today(), _make_cfg())
        assert result["files"] == []

    def test_truncates_at_max_files(self, tmp_path):
        for i in range(5):
            _touch_now(tmp_path / f"f{i}.py")
        result = ac.collect_dir_activity(str(tmp_path), date.today(), date.today(), _make_cfg(**{"max_files_per_dir": 3}))
        assert len(result["files"]) == 3
        assert result["total_found"] == 5
        assert result["truncated"] is True

    def test_sorted_newest_first(self, tmp_path):
        t_old = time.time() - 3600
        t_new = time.time()
        for name, ts in [("old.py", t_old), ("new.py", t_new)]:
            p = tmp_path / name
            p.write_text("x")
            os.utime(str(p), (ts, ts))
        result = ac.collect_dir_activity(str(tmp_path), date.today(), date.today(), _make_cfg())
        assert result["files"][0]["path"].endswith("new.py")

    def test_no_extension_filter_when_exts_empty(self, tmp_path):
        _touch_now(tmp_path / "notes.txt")
        _touch_now(tmp_path / "data.csv")
        result = ac.collect_dir_activity(str(tmp_path), date.today(), date.today(), _make_cfg(**{"file_extensions": []}))
        assert len(result["files"]) == 2


# ── find_git_repos ────────────────────────────────────────────────────────────

class TestFindGitRepos:
    def test_nonexistent_root_returns_empty(self):
        assert ac.find_git_repos(["/no/such/path"]) == []

    def test_finds_repo_at_root(self, tmp_path):
        (tmp_path / ".git").mkdir()
        result = ac.find_git_repos([str(tmp_path)])
        assert tmp_path in result

    def test_finds_nested_repo(self, tmp_path):
        repo = tmp_path / "project"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = ac.find_git_repos([str(tmp_path)])
        assert repo in result

    def test_excludes_excluded_dir(self, tmp_path):
        repo = tmp_path / "project"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = ac.find_git_repos([str(tmp_path)], exclude_dirs=[str(repo.resolve())])
        assert repo not in result

    def test_ignores_venv_at_top(self, tmp_path):
        """Regression: .venv/.git must not be discovered."""
        venv_repo = tmp_path / ".venv" / "src"
        venv_repo.mkdir(parents=True)
        (venv_repo / ".git").mkdir()
        result = ac.find_git_repos([str(tmp_path)], ignore_patterns=[".venv"])
        assert not any(".venv" in str(r) for r in result)

    def test_ignores_venv_nested(self, tmp_path):
        """Regression: .venv nested deep inside root must be ignored."""
        nested = tmp_path / "labs" / "lab-01" / ".venv" / "src"
        nested.mkdir(parents=True)
        (nested / ".git").mkdir()
        result = ac.find_git_repos([str(tmp_path)], ignore_patterns=[".venv"])
        assert result == []

    def test_venv_ignored_but_sibling_repo_found(self, tmp_path):
        venv_repo = tmp_path / ".venv"
        venv_repo.mkdir()
        (venv_repo / ".git").mkdir()
        real_repo = tmp_path / "project"
        real_repo.mkdir()
        (real_repo / ".git").mkdir()
        result = ac.find_git_repos([str(tmp_path)], ignore_patterns=[".venv"])
        assert real_repo in result
        assert not any(".venv" in str(r) for r in result)

    def test_no_duplicates(self, tmp_path):
        (tmp_path / ".git").mkdir()
        result = ac.find_git_repos([str(tmp_path), str(tmp_path)])
        assert len(result) == 1

    def test_returns_sorted(self, tmp_path):
        for name in ["b_proj", "a_proj"]:
            d = tmp_path / name
            d.mkdir()
            (d / ".git").mkdir()
        result = ac.find_git_repos([str(tmp_path)])
        assert result == sorted(result)


# ── _read_claude_md ───────────────────────────────────────────────────────────

class TestReadClaudeMd:
    def test_reads_claude_md(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Title\n\nThis is the description.")
        assert "This is the description" in ac._read_claude_md(tmp_path)

    def test_falls_back_to_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("# Project\n\nFallback text here.")
        assert "Fallback text here" in ac._read_claude_md(tmp_path)

    def test_prefers_claude_md_over_readme(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Title\n\nFrom CLAUDE.md.")
        (tmp_path / "README.md").write_text("# Title\n\nFrom README.md.")
        desc = ac._read_claude_md(tmp_path)
        assert "From CLAUDE.md" in desc
        assert "From README.md" not in desc

    def test_missing_files_returns_empty(self, tmp_path):
        assert ac._read_claude_md(tmp_path) == ""

    def test_truncates_at_320(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Title\n\n" + "word " * 200)
        assert len(ac._read_claude_md(tmp_path)) <= 320

    def test_skips_fence_markers(self, tmp_path):
        # Only lines STARTING with ``` are skipped; content lines inside fences are included
        (tmp_path / "CLAUDE.md").write_text("# Title\n\n```python\n\nReal desc.")
        desc = ac._read_claude_md(tmp_path)
        assert "```" not in desc


# ── _project_type ─────────────────────────────────────────────────────────────

class TestProjectType:
    def test_claude_code_git(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("")
        (tmp_path / ".git").mkdir()
        assert ac._project_type(tmp_path) == "claude-code-git"

    def test_claude_code_only(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("")
        assert ac._project_type(tmp_path) == "claude-code"

    def test_git_only(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert ac._project_type(tmp_path) == "git"

    def test_plain_directory(self, tmp_path):
        assert ac._project_type(tmp_path) == "directory"


# ── _find_target_files ────────────────────────────────────────────────────────

class TestFindTargetFiles:
    def test_exact_match(self):
        result = ac._find_target_files(["src/main.py", "REPORT.md"], ["REPORT.md"])
        assert result == ["REPORT.md"]

    def test_case_insensitive(self):
        result = ac._find_target_files(["report.md"], ["REPORT.MD"])
        assert "report.md" in result

    def test_wildcard(self):
        result = ac._find_target_files(["executive_summary.md", "notes.txt"], ["executive_*.md"])
        assert "executive_summary.md" in result

    def test_no_match(self):
        result = ac._find_target_files(["foo.py", "bar.ts"], ["README.md"])
        assert result == []

    def test_nested_path_matched_by_basename(self):
        result = ac._find_target_files(["docs/sub/README.md"], ["README.md"])
        assert "docs/sub/README.md" in result


# ── collect_git_activity ──────────────────────────────────────────────────────

class TestCollectGitActivity:
    def _make_repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        return tmp_path

    def test_returns_empty_on_no_git_output(self, tmp_path):
        repo = self._make_repo(tmp_path)
        with patch.object(ac, "_run", return_value=""):
            result = ac.collect_git_activity(repo, date(2026, 6, 22), date(2026, 6, 28))
        assert result["commits"] == []
        assert result["stats"]["files_changed"] == []

    def test_parses_commits(self, tmp_path):
        repo = self._make_repo(tmp_path)
        log_line = "abc1234|2026-06-25|Alice|alice@example.com|Fix the bug"
        def fake_run(cmd, cwd=None):
            if "--format=%h" in " ".join(cmd):
                return log_line
            if "--numstat" in cmd:
                return "5\t2\tsrc/foo.py"
            return ""
        with patch.object(ac, "_run", side_effect=fake_run):
            result = ac.collect_git_activity(repo, date(2026, 6, 22), date(2026, 6, 28), author="Alice")
        assert len(result["commits"]) == 1
        c = result["commits"][0]
        assert c["hash"] == "abc1234"
        assert c["subject"] == "Fix the bug"
        assert c["author_name"] == "Alice"

    def test_parses_numstat(self, tmp_path):
        repo = self._make_repo(tmp_path)
        def fake_run(cmd, cwd=None):
            if "--format=%h" in " ".join(cmd):
                return "abc1234|2026-06-25|Alice|alice@example.com|Fix"
            if "--numstat" in cmd:
                return "10\t3\tsrc/main.py\n5\t1\tsrc/utils.py"
            return ""
        with patch.object(ac, "_run", side_effect=fake_run):
            result = ac.collect_git_activity(repo, date(2026, 6, 22), date(2026, 6, 28))
        assert result["stats"]["total_additions"] == 15
        assert result["stats"]["total_deletions"] == 4
        assert "src/main.py" in result["stats"]["files_changed"]

    def test_date_range_uses_one_day_buffer(self, tmp_path):
        """since-1 and until+1 are passed to git to make boundaries inclusive."""
        repo = self._make_repo(tmp_path)
        captured = {}
        def fake_run(cmd, cwd=None):
            if "--format=%h" in " ".join(cmd):
                captured["cmd"] = cmd
            return ""
        with patch.object(ac, "_run", side_effect=fake_run):
            ac.collect_git_activity(repo, date(2026, 6, 22), date(2026, 6, 28))
        assert "--after=2026-06-21" in captured["cmd"]
        assert "--before=2026-06-29" in captured["cmd"]


# ── render_markdown ───────────────────────────────────────────────────────────

class TestRenderMarkdown:
    def _base_cfg(self):
        return {"author": "Test User", "claude_projects_dir": "/tmp/projects"}

    def test_header_present(self):
        md = ac.render_markdown([], [], [], self._base_cfg(), date(2026, 6, 22), date(2026, 6, 28))
        assert "Weekly Activity Evidence" in md
        assert "2026-06-22" in md
        assert "2026-06-28" in md

    def test_no_sections_when_empty(self):
        md = ac.render_markdown([], [], [], self._base_cfg(), date(2026, 6, 22), date(2026, 6, 28))
        assert "AI-Native Development" not in md
        assert "Other Git Repositories" not in md

    def test_claude_project_section(self):
        project = {
            "name": "My Project",
            "path": "/tmp/my-project",
            "type": "claude-code-git",
            "description": "Cool project",
            "git": {
                "commits": [{"hash": "abc", "date": "2026-06-25", "author_name": "Alice",
                             "author_email": "a@a.com", "subject": "Add feature"}],
                "remote": "https://github.com/foo/bar",
                "stats": {"files_changed": ["main.py"], "total_additions": 5, "total_deletions": 1},
            },
            "files": {"files": [], "total_found": 0},
            "enrichment": None,
        }
        md = ac.render_markdown([project], [], [], self._base_cfg(), date(2026, 6, 22), date(2026, 6, 28))
        assert "My Project" in md
        assert "Add feature" in md
        assert "AI-Native Development" in md

    def test_summary_table_present(self):
        md = ac.render_markdown([], [], [], self._base_cfg(), date(2026, 6, 22), date(2026, 6, 28))
        assert "## Summary" in md

    def test_enrichment_section_rendered(self):
        project = {
            "name": "AI Project",
            "path": "/tmp/ai-proj",
            "type": "claude-code",
            "description": "",
            "git": None,
            "files": {"files": [{"path": "notes.md", "modified": "2026-06-25 10:00"}], "total_found": 1},
            "enrichment": "This week the team shipped the auth refactor.",
        }
        md = ac.render_markdown([project], [], [], self._base_cfg(), date(2026, 6, 22), date(2026, 6, 28))
        assert "AI Summary" in md
        assert "auth refactor" in md

    def test_output_has_no_trailing_spaces(self):
        md = ac.render_markdown([], [], [], self._base_cfg(), date(2026, 6, 22), date(2026, 6, 28))
        for line in md.splitlines():
            assert line == line.rstrip(), f"trailing space: {line!r}"
