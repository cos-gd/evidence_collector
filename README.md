# activity-collector

Scans **Claude Projects**, git repos, and directories for a given time period
and produces a structured Markdown evidence file for the weekly GD CTO PA digest.

---

## Repo structure

```
.
├── activity-collector.py   # the script (no external dependencies)
├── config.example.json     # template — copy to config.json and edit
├── config.json             # your local config (gitignored)
├── .gitignore
└── evidence/
    └── 2026-W25.md         # one file per week, committed after review
```

---

## Quick start

```bash
# 1. Clone and set up
git clone https://github.com/cos-gd/evidence_collector.git
cd evidence_collector
cp config.example.json config.json   # edit paths to match your machine

# 2. Run for last week (Mon–Sun)
python activity-collector.py --config config.json

# 3. Run for a specific range
python activity-collector.py --config config.json \
  --from 2026-06-16 --to 2026-06-22

# 4. Run for the current week so far
python activity-collector.py --config config.json --week current

# 5. Dry-run (preview counts, no file written)
python activity-collector.py --config config.json --dry-run
```

Output is written to `output_dir` in config (default: `~/Documents/weekly-evidence/`).
Move or copy the generated `.md` into `evidence/` before committing.

---

## Config reference

| Key | Default | Description |
|-----|---------|-------------|
| `author` | `null` | Git author filter (name or email fragment). `null` = all authors. |
| `claude_projects_dir` | `~/Claude/Projects` | Root of Claude Code / Cowork projects. |
| `scan_dirs` | `[]` | Additional plain directories to scan for recently modified files. |
| `git_roots` | `[]` | Directories to recursively search for `.git` repos. Claude Projects dir is always included automatically. |
| `output_dir` | `~/Documents/weekly-evidence` | Where to write the generated `.md` files. |
| `file_extensions` | see script | File types to report in directory scans (git tracks all files regardless). |
| `ignore_patterns` | `node_modules`, etc. | Directory / filename patterns to skip inside a scanned directory. |
| `exclude_dirs` | `[]` | Directories to exclude entirely from **all** scans (git, dir, and Claude Projects). Useful for archives, mirrors, or noisy repos. |
| `max_files_per_dir` | `40` | Cap on file listing per directory section. |
| `claude_enrich` | `false` | Enable Claude AI summaries in the report. Requires `pip install anthropic`. |
| `claude_model` | `claude-sonnet-4-6` | Anthropic model used for enrichment. |
| `claude_enrich_files` | `["REPORT.md","README.md","executive_summary.md"]` | Filename patterns (basename, case-insensitive, wildcards supported: `*`, `?`, `[...]`) matched against changed files. |
| `claude_api_key` | `null` | Explicit Anthropic API key. If `null`, uses `ANTHROPIC_API_KEY` environment variable. |

---

## CLI flags

```
--from YYYY-MM-DD   Start date (inclusive)
--to   YYYY-MM-DD   End date (inclusive)
--week last|current Shortcut (default: last full Mon–Sun week)
--config PATH       JSON config file
--output PATH       Override output file path
--author NAME       Override git author filter
--dirs DIR [...]    Extra directories to scan (appended to config)
--repos DIR [...]   Extra git root dirs (appended to config)
--exclude DIR [...] Directories to exclude from all scans (appended to config)
--enrich            Enrich report with Claude AI summaries (requires: pip install anthropic)
--dry-run           Print counts without writing output
```

---

## Weekly workflow

```bash
# Monday morning
python activity-collector.py --config config.json
# → evidence/2026-W25.md is generated

# Open the file, fill in the "Digest Notes" fields under each project
# Then commit:
git add evidence/2026-W25.md
git commit -m "evidence: add W25 activity"
git push
```

The committed `.md` file is then referenced automatically by the
`/gd-technical-director-weekly-digest` skill on the next run
(add the repo to your `evidence-collection.md`).

---

## Requirements

- Python 3.9+
- `git` on your `PATH`
- No pip installs needed for core functionality — stdlib only
- `pip install anthropic` — optional, required only for `--enrich` / `claude_enrich: true`
