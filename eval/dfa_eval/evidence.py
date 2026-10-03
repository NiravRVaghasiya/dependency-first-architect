"""The README's evidence block, generated from the committed runs, so the README can only quote
numbers that committed records produce.

    python eval/run.py evidence           rewrite the block in README.md
    python eval/run.py evidence --check   write nothing; exit 1 if the block is stale

Input: every eval/results/<run>/summary.json (plan-scoring runs) and outcomes-summary.json
(outcome runs). Output: the text between README.md's BEGIN/END markers. Each contrast row gives
the mean difference (treatment − control) with its 95% interval: across prompts (Student t) for
plan scores, a bootstrap over attempts for outcomes. No ranking language, and no row for a run that
is not committed.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = "README.md"
BEGIN = "<!-- BEGIN GENERATED evidence: eval/dfa_eval/evidence.py -->"
END = "<!-- END GENERATED evidence -->"

PLAN_COLUMNS = [  # (metric, header, decimals)
    ("adherence_total", "Adherence /20", 1),
    ("eq_total", "EQ /44", 1),
    ("eq_core", "EQ-core /36", 1),
    ("checklist_coverage", "Checklist 0–1", 2),
    ("words", "Words", 0),
    ("eq_per_1k_tokens", "EQ per 1k tokens", 2),
]
OUTCOME_COLUMNS = [
    ("round1_pass_rate", "Round-1 pass rate", 2),
    ("change_request_pass_rate", "Change-request pass rate", 2),
    ("regression_pass_rate", "Regression pass rate", 2),
    ("rework_lines", "Rework lines", 0),
]


def _num(value, decimals, signed=True):
    if value is None:
        return "n/a"
    text = f"{value:+.{decimals}f}" if signed else f"{value:.{decimals}f}"
    return text.replace("-", "−")


def _cell(mean, interval, decimals):
    if mean is None:
        return "n/a"
    text = _num(mean, decimals)
    if interval:
        text += f" [{_num(interval[0], decimals)}, {_num(interval[1], decimals)}]"
    return text


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _runs(results_dir):
    """Committed runs only, the way `check` sees them: an outcome run is a directory with
    outcomes/config.json, quoted from outcomes-summary.json; a plan run has manifest.json and
    summary.json. Anything else is not quoted (and `check` reports it)."""
    out = []
    folder = Path(results_dir)
    for path in sorted(folder.iterdir(), key=lambda p: p.name) if folder.is_dir() else []:
        if (path / "outcomes" / "config.json").is_file():
            if (path / "outcomes-summary.json").is_file():
                out.append(("outcomes", path, _read(path / "outcomes-summary.json")))
        elif (path / "manifest.json").is_file() and (path / "summary.json").is_file():
            out.append(("plans", path, _read(path / "summary.json")))
    return out


def _caveats(metrics):
    """Each distinct note on the row's quoted metrics, with the columns it applies to."""
    where = {}
    for metric, header, _ in PLAN_COLUMNS:
        for note in (metrics.get(metric) or {}).get("notes") or []:
            where.setdefault(note, []).append(header.split(" /")[0].split(" 0–1")[0])
    return [f"{note} ({', '.join(cols)})" for note, cols in where.items()]


def _plan_block(path, summary):
    run = summary.get("run", {})
    gens = ", ".join(f"{g.get('model_requested') or g.get('id') or 'unknown'} "
                     f"(effort {g.get('effort') or 'default'})" for g in run.get("generators", []))
    judges = ", ".join(g.get("model_requested") or g.get("id") or "unknown"
                       for g in run.get("judges", []))
    lines = [f"**[`{path.name}`]({path.as_posix()}/REPORT.md)** — config `{run.get('config_name')}`; "
             f"generator {gens}; judges {judges}; {run.get('runs_per_cell')} run(s) per cell "
             "configured.", ""]
    by_pair = {}
    for c in summary.get("contrasts", []):
        by_pair.setdefault((c["treatment"], c["control"], c["generator"]), {})[c["metric"]] = c
    if not by_pair:
        return lines + ["No contrasts.", ""]
    headers = [h for _, h, _ in PLAN_COLUMNS]
    lines.append("| Treatment − control | Generator | n prompts | Plans | " + " | ".join(headers)
                 + " |")
    lines.append("|---|---|---:|---|" + "---|" * len(headers))
    caveats = []
    for (treatment, control, generator), metrics in by_pair.items():
        n = max((c.get("n_prompts") or 0) for c in metrics.values())
        quoted = [metrics[m] for m, _, _ in PLAN_COLUMNS if m in metrics]
        plans = (f"{max(c.get('n_treatment') or 0 for c in quoted)} vs "
                 f"{max(c.get('n_control') or 0 for c in quoted)}") if quoted else "—"
        cells = []
        for metric, _, decimals in PLAN_COLUMNS:
            c = metrics.get(metric)
            cells.append("—" if c is None else
                         _cell(c.get("mean_diff"), (c.get("diff") or {}).get("ci95"), decimals))
        lines.append(f"| {treatment} − {control} | {generator} | {n} | {plans} | "
                     + " | ".join(cells) + " |")
        row_caveats = _caveats(metrics)
        if row_caveats:
            caveats.append(f"caveats, {treatment} − {control} ({generator}): "
                           + "; ".join(row_caveats))
    extras = list(caveats)
    extras += [f"flag: {flag}" for flag in summary.get("flags", [])]
    for (treatment, control, generator), metrics in by_pair.items():
        per_judge = [f"{metric.partition('@')[2]} "
                     f"{_cell(c.get('mean_diff'), (c.get('diff') or {}).get('ci95'), 1)}"
                     for metric, c in sorted(metrics.items()) if metric.startswith("eq_total@")]
        if len(per_judge) > 1:
            extras.append(f"EQ by judge, {treatment} − {control} ({generator}): "
                          + "; ".join(per_judge))
    for entry in summary.get("leakage", []):
        aucs = [f"{c['treatment']} vs {c['control']} {_num(c.get('auc'), 2, False)}"
                for c in entry.get("contrasts", []) if c.get("auc") is not None]
        if aucs:
            whose = f", {entry['generator']} plans" if entry.get("generator") else ""
            extras.append(f"leakage-probe AUC (judge {entry['judge']}{whose}): " + ", ".join(aucs))
    for rel in summary.get("judge_reliability", []):
        if rel.get("alpha_interval") is not None:
            text = (f"judge agreement on {rel['rubric']} (Krippendorff α, "
                    f"{rel['n_coders']} coders): {_num(rel['alpha_interval'], 2, False)}")
            ranks = [r for r in rel.get("rank_consistency", []) if r.get("spearman") is not None]
            if ranks:  # mean_offset is the second coder's total minus the first's
                text += "; rank agreement (Spearman) " + ", ".join(
                    f"{_num(r['spearman'], 2, False)}, with {r['coders'][1]} scoring "
                    f"{_num(r.get('mean_offset'), 1)} points relative to {r['coders'][0]}"
                    for r in ranks)
            extras.append(text)
    cost = (summary.get("cost") or {}).get("total", {}).get("cost_usd")
    if cost is not None:
        extras.append(f"recorded cost ${cost:.2f}")
    if extras:
        lines += [""] + [f"- {extra}" for extra in extras]
    return lines + [""]


def _outcome_block(path, summary):
    lines = [f"**[`{path.name}`]({path.as_posix()}/OUTCOMES.md)** — task `{summary.get('task')}`; "
             f"planner {summary['planner'].get('model')}; implementer "
             f"{summary['implementer'].get('model')}; {summary['attempts']['ok']} complete attempts "
             f"of {summary['attempts'].get('planned', 'n/a')} planned.",
             ""]
    by_pair = {}
    for c in summary.get("contrasts", []):
        by_pair.setdefault((c["treatment"], c["control"]), {})[c["metric"]] = c
    if not by_pair:
        return lines + ["No contrasts.", ""]
    headers = [h for _, h, _ in OUTCOME_COLUMNS]
    lines.append("| Treatment − control | n | " + " | ".join(headers) + " |")
    lines.append("|---|---|" + "---|" * len(headers))
    extras = []
    for (treatment, control), metrics in by_pair.items():
        some = next(iter(metrics.values()))
        cells = []
        for metric, _, decimals in OUTCOME_COLUMNS:
            c = metrics.get(metric)
            cell = "—" if c is None else _cell(c.get("mean_diff"), c.get("bootstrap_ci95"),
                                                decimals)
            if c is not None and (c["n_treatment"], c["n_control"]) != (some["n_treatment"],
                                                                         some["n_control"]):
                cell += f" ({c['n_treatment']} vs {c['n_control']})"  # rounds measured differ
            cells.append(cell)
        lines.append(f"| {treatment} − {control} | {some['n_treatment']} vs {some['n_control']} | "
                     + " | ".join(cells) + " |")
        notes = []
        for metric, _, _ in OUTCOME_COLUMNS:
            for note in (metrics.get(metric) or {}).get("notes") or []:
                if note not in notes:
                    notes.append(note)
        if notes:
            extras.append(f"caveats, {treatment} − {control}: " + "; ".join(notes))
    extras += [f"flag: {flag}" for flag in summary.get("flags", [])]
    cost = (summary.get("cost") or {}).get("total_usd")
    if cost is not None:
        extras.append(f"recorded cost ${cost:.2f}")
    if extras:
        lines += [""] + [f"- {extra}" for extra in extras]
    return lines + [""]


def render(repo_root=None):
    root = Path(repo_root) if repo_root else ROOT
    runs = _runs(root / "eval" / "results")
    out = ["", "_Generated from the committed runs by `eval/dfa_eval/evidence.py`; `python eval/run.py "
           "check` fails if this block and the records disagree. Cells: mean difference, "
           "treatment − control, with its 95% interval (t across prompts for plans; bootstrap over "
           "attempts for outcomes)._", ""]
    if not runs:
        return "\n".join(out + ["No committed runs yet.", ""])
    for kind, path, summary in runs:
        rel = path.relative_to(root)
        out += _plan_block(rel, summary) if kind == "plans" else _outcome_block(rel, summary)
    return "\n".join(out)


def splice(text, block):
    head, found, rest = text.partition(BEGIN)
    _, found_end, tail = rest.partition(END)
    if not found or not found_end:
        raise ValueError(f"{README} is missing its evidence markers ({BEGIN} ... {END})")
    return f"{head}{BEGIN}\n{block}{END}{tail}"


def write_or_check(repo_root=None, check=False):
    """(ok, message). With check, compare only; otherwise rewrite the block if it changed."""
    root = Path(repo_root) if repo_root else ROOT
    path = root / README
    current = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    updated = splice(current, render(root))
    if updated == current:
        return True, f"{README} evidence block is up to date"
    if check:
        return False, f"{README} evidence block differs from the committed runs; run `python eval/run.py evidence`"
    path.write_bytes(updated.encode("utf-8"))
    return True, f"updated the evidence block in {README}"
