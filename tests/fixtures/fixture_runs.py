"""Fixture runs shared by the analysis tests: compact specs written by runbuilder.make_run.

contrast_run   3 prompts x (dfa, baseline) x 2 runs, one judge; numbers chosen so the EQ contrast,
               the probe AUC and the condition means can be worked out by hand (test_aggregate).
rich_run       2 prompts x 5 conditions x 2 runs, two judges x two repeats: failed generations, an
               invalid and an error judgment, an error probe, a missing arm, length caps, unknown
               costs, and a judge on the generator's own model.
small_run      2 prompts x (dfa, baseline) x 1 run: cheap, for tests of file handling.
"""

import runbuilder as rb


def all4(value):
    """The same score from both judges, both repeats."""
    return {"j1": value, "j1#2": value, "j2": value, "j2#2": value}


EQ_TREATMENT = {"P1": (30, 34), "P2": (28, 26), "P3": (36, 38)}
EQ_CONTROL = {"P1": (20, 22), "P2": (24, 20), "P3": (30, 28)}
P_TREATMENT = {"P1": (0.9, 0.6), "P2": (0.7, 0.4), "P3": (0.8, 0.5)}
P_CONTROL = {"P1": (0.3, 0.6), "P2": (0.2, 0.5), "P3": (0.1, 0.45)}


def contrast_run(root):
    gens = []
    for p in ("P1", "P2", "P3"):
        for r in (1, 2):
            gens.append(rb.gen(p, "dfa", r, adherence={"j1": 20}, eq={"j1": EQ_TREATMENT[p][r - 1]},
                               probes={"j1": P_TREATMENT[p][r - 1]}, lint=(16, 16), cost=0.10,
                               words=10 * EQ_TREATMENT[p][r - 1]))
            gens.append(rb.gen(p, "baseline", r, adherence={"j1": 12},
                               eq={"j1": EQ_CONTROL[p][r - 1]}, probes={"j1": P_CONTROL[p][r - 1]},
                               lint=(4, 16), cost=0.05, words=10 * EQ_CONTROL[p][r - 1]))
    return rb.make_run(root, gens, run_id="contrast-run", prompts=("P1", "P2", "P3"),
                       probe_judges=["j1"], seed=11)


def small_run(root):
    """Two prompts, one run per cell: cheap to aggregate, for the file-level tests."""
    gens = [rb.gen(p, c, 1, adherence={"j1": a}, eq={"j1": e})
            for p, c, a, e in (("P1", "dfa", 20, 30), ("P1", "baseline", 12, 21),
                               ("P2", "dfa", 19, 33), ("P2", "baseline", 10, 25))]
    return rb.make_run(root, gens, run_id="small-run", runs_per_cell=1)


BASELINE_P1_R1 = {
    "j1": [2, 2, 2, 2, 2, 1, 1, 1, 0, 0],  # 13
    "j1#2": [2, 2, 1, 1, 1, 1, 1, 0, 0, 0],  # 9
    "j2": [2, 1, 1, 1, 1, 1, 1, 1, 1, 0],  # 10
    "j2#2": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1],  # 10
}


def rich_run(root):
    g = rb.gen
    gens = [
        g("P1", "dfa", 1, adherence=all4(20), eq={"j1": 40, "j1#2": 38, "j2": 42, "j2#2": 40},
          probes={"j1": "error"}, words=900, cost=0.20),
        g("P1", "dfa", 2, adherence=all4(20), eq={"j1": 36, "j1#2": 36, "j2": 38, "j2#2": 38},
          probes={"j1": 0.9}, words=1000, cost=None),
        g("P1", "baseline", 1, adherence=BASELINE_P1_R1,
          eq={"j1": 24, "j1#2": "invalid", "j2": 26, "j2#2": 22}, probes={"j1": 0.2}, words=600,
          cost=0.05),
        g("P1", "baseline", 2, adherence={"j1": "error", "j1#2": 12, "j2": 12, "j2#2": 12},
          eq=all4(20), probes={"j1": 0.3}, words=700, cost=0.05),
        g("P1", "generic-control", 1, adherence=all4(14), eq=all4(30), probes={"j1": 0.6},
          words=800),
        g("P1", "generic-control", 2, adherence=all4(16), eq=all4(32), probes={"j1": 0.7},
          words=850),
        g("P1", "baseline-capped", 1, adherence=all4(10), eq=all4(18), probes={"j1": 0.2},
          words=90),
        g("P1", "baseline-capped", 2, adherence=all4(10), eq=all4(18), probes={"j1": 0.1},
          words=120),
        g("P1", "dfa-capped", 1, adherence=all4(18), eq=all4(30), probes={"j1": 0.8}, words=100),
        g("P1", "dfa-capped", 2, adherence=all4(19), eq=all4(30), probes={"j1": 0.7}, words=110),
        g("P2", "dfa", 1, adherence=all4(20), eq=all4(34), probes={"j1": 0.85}, words=950),
        g("P2", "dfa", 2, status="error", cost=None),
        g("P2", "baseline", 1, adherence=all4(9),
          eq={"j1": 21, "j1#2": 21, "j2": {"total": 21, "cost": None}, "j2#2": 21},
          probes={"j1": 0.4}, words=650),
        g("P2", "baseline", 2, adherence=all4(11), eq=all4(23), probes={"j1": 0.35}, words=600),
        g("P2", "generic-control", 1, status="error"),
        g("P2", "generic-control", 2, status="error"),
        g("P2", "baseline-capped", 1, adherence=all4(9), eq=all4(19), probes={"j1": 0.2},
          words=95),
        g("P2", "baseline-capped", 2, adherence=all4(9), eq=all4(19), probes={"j1": 0.25},
          words=115),
        g("P2", "dfa-capped", 1, adherence=all4(19), eq=all4(31), probes={"j1": 0.75}, words=111),
        g("P2", "dfa-capped", 2, adherence=all4(20), eq=all4(29), probes={"j1": 0.8}, words=90),
    ]
    return rb.make_run(
        root, gens, run_id="rich-run", prompts=("P1", "P2"),
        conditions=("baseline", "dfa", "generic-control", "baseline-capped", "dfa-capped"),
        judges=[{"id": "j1", "provider": "fake", "model": "judge-a"},
                {"id": "j2", "provider": "fake", "model": "gen-model"}],
        judge_repeats=2, probe_judges=["j1"],
        contrasts=(("dfa", "baseline"), ("dfa", "generic-control"),
                   ("dfa-capped", "baseline-capped"), ("generic-control", "baseline")))
