"""Offline tests of the suite's checking and statistics (no server):
    python -m pytest bench/regress/test_regress.py"""

import copy
import datetime
import decimal
import json
import pathlib
import sys

import pyarrow as pa
import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import compare
import stats
import workloads

THRESHOLDS = json.loads((HERE / "thresholds.json").read_text())


def table(rows: list[dict]) -> pa.Table:
    return pa.Table.from_pylist(mapping=rows)


# ---- answers -----------------------------------------------------------------------------------

EXPECTED = [["A", "F", "380456.00", "25.575154611454693", "1995-03-05"],
            ["N", "O", "8971.00", "25.778735632183906", "1996-01-01"]]


def good_rows() -> list[dict]:
    return [{"f": "A", "s": "F", "q": decimal.Decimal("380456.00"), "a": 25.575154611454693, "d": datetime.date(1995, 3, 5)},
            {"f": "N", "s": "O", "q": decimal.Decimal("8971.00"), "a": 25.778735632183906, "d": datetime.date(1996, 1, 1)}]


def test_right_answer_passes():
    assert workloads.check_answer(table=table(rows=good_rows()), expected=EXPECTED) is None


def test_float_noise_within_tolerance_passes():
    rows = good_rows()
    rows[0]["a"] = 25.575154611454700  # parallel float sums differ in the last digits
    assert workloads.check_answer(table=table(rows=rows), expected=EXPECTED) is None


def test_wrong_value_fails_naming_the_row():
    rows = good_rows()
    rows[1]["q"] = decimal.Decimal("8972.00")
    problem = workloads.check_answer(table=table(rows=rows), expected=EXPECTED)
    assert problem is not None and problem.startswith("row 1:")


def test_missing_row_fails_with_counts():
    assert workloads.check_answer(table=table(rows=good_rows()[:1]), expected=EXPECTED) == "1 rows, expected 2"


def test_rows_tied_on_order_by_may_come_in_either_order():
    assert workloads.check_answer(table=table(rows=list(reversed(good_rows()))), expected=EXPECTED) is None


def test_null_answer_line_is_one_null_row(tmp_path):
    # tpch_answers() prints q17's single NULL at scale 0.01 as an empty line.
    directory = workloads.tpch_dir(data_root=tmp_path, scale=0.01)
    directory.mkdir(parents=True)
    (directory / "answers.json").write_text(json.dumps({"17": "avg_yearly\n\n", "1": "a|b\n1|x\n"}))
    expected = workloads.tpch_expected(data_root=tmp_path, scale=0.01)
    assert expected == {"q17": [[""]], "q01": [["1", "x"]]}
    assert workloads.check_answer(table=pa.table({"avg_yearly": pa.array([None], type=pa.float64())}),
                                  expected=expected["q17"]) is None
    assert workloads.check_answer(table=pa.table({"avg_yearly": [1.5]}), expected=expected["q17"]) is not None


def test_many_pipelines_expected_answers_have_their_shape():
    expected = workloads.mp_expected(scale=0.01)
    assert len(expected["union30"]) == len(workloads.PARTS)
    assert len(expected["scalar30"]) == 1 and len(expected["scalar30"][0]) == len(workloads.PARTS) + 1
    totals = [row[1] for row in expected["union30"]]
    assert totals == sorted(totals, reverse=True)
    assert expected["scalar30"][0][-1] == 10  # i % 3 = 0 reaches every third of the 30 tables


# ---- statistics --------------------------------------------------------------------------------

def test_percentile_is_nearest_rank():
    values = list(range(1, 51))
    assert stats.percentile(values=values, fraction=0.5) == 25
    assert stats.percentile(values=values, fraction=0.9) == 45
    assert stats.percentile(values=values, fraction=0.99) == 50


def test_clear_slowdown_is_a_regression():
    base = [[1.00, 1.01, 0.99]] * 5
    slow = [[1.30, 1.31, 1.29]] * 5
    point, low, high = stats.ratio_interval(candidate=slow, baseline=base)
    assert low > 1.25 and high < 1.35
    assert stats.judge(point=point, low=low, high=high, threshold=0.10, higher_is_better=False) == "regression"


def test_noise_is_not_flagged():
    base = [[1.0, 1.4, 0.8], [1.2, 0.9, 1.1], [1.0, 1.3, 0.7]]
    same = [[1.1, 0.8, 1.3], [0.9, 1.2, 1.0], [1.4, 0.9, 1.0]]
    point, low, high = stats.ratio_interval(candidate=same, baseline=base)
    assert stats.judge(point=point, low=low, high=high, threshold=0.10, higher_is_better=False) == "ok"


def test_tiny_absolute_change_is_not_flagged():
    assert stats.judge(point=1.5, low=1.4, high=1.6, threshold=0.10, higher_is_better=False,
                       absolute_change=0.0002, minimum_change=0.001) == "ok"


def test_throughput_drop_is_a_regression_and_rise_an_improvement():
    assert stats.judge(point=0.7, low=0.65, high=0.75, threshold=0.10, higher_is_better=True) == "regression"
    assert stats.judge(point=1.4, low=1.3, high=1.5, threshold=0.10, higher_is_better=True) == "improvement"


# ---- report ------------------------------------------------------------------------------------

def fake_run(slow_factor: float) -> dict:
    def rep_record(label: str, rep: int, factor: float) -> dict:
        warm = [{"state": "warm", "suite_s": 1.0 * factor, "statements_s": {"q01": 0.05 * factor, "q02": 0.02}}
                for _ in range(3)]
        return {"rep": rep, "label": label, "position": 0, "server_start_s": 0.2, "load_average": [1.0, 1.0, 1.0],
                "tpch": [{"state": "cold", "suite_s": 1.2, "statements_s": {"q01": 0.06, "q02": 0.03}}] + warm,
                "many-pipelines": {"union30": [0.010 + i * 1e-5 for i in range(50)]},
                "concurrent": {"4": {"clients": 4, "passes": 2, "statements": 176, "errors": [], "wall_s": 2.0 * factor,
                                     "throughput_qps": 88 / factor, "latencies_s": [0.04 * factor] * 176}},
                "resources": {"tpch": {"server_cpu_s": 4.0, "wall_s": 4.0}}, "peak_rss_bytes": 500 * 2**20}
    return {"started_at": "2026-11-19T00:00:00+00:00", "finished_at": "2026-11-19T01:00:00+00:00",
            "host": {"host_class": "test", "cpu_model": "cpu", "logical_cpus": 4, "memory_bytes": 8 * 2**30,
                     "system": "Linux", "release": "x"},
            "config": {"scale": 1.0, "threads": 2, "memory_limit": "4GiB", "server_cores": None, "client_cores": None,
                       "repetitions": 5, "warm_passes": 3, "mp_runs": 50, "clients": [4], "concurrent_passes": 2,
                       "drop_caches": False, "transport": "http"},
            "client": {"python": "3", "httpx": "0", "pyarrow": "0"},
            "servers": {label: {"version": label, "duckdb_version": "v2", "sha256": "0" * 64, "source": "test"}
                        for label in ("old", "new")},
            "order": ["old", "new"], "candidate": "new", "load": {}, "answers": "published",
            "repetitions": [rep_record(label=label, rep=rep, factor=slow_factor if label == "new" else 1.0)
                            for rep in range(5) for label in ("old", "new")],
            "correctness": {"checks": 10, "checks_by_label": {"old": 5, "new": 5}, "failures": []}}


def test_report_flags_a_planted_slowdown_and_nothing_without_it():
    report, verdict = compare.build_report(runs=[fake_run(slow_factor=1.5)], thresholds=THRESHOLDS)
    flagged = {r["metric"] for r in verdict["regressions"]}
    assert {"tpch/q01", "tpch/suite-warm", "concurrent/4/throughput", "concurrent/4/p50"} <= flagged
    assert "tpch/q02" not in flagged
    assert "**REGRESSION**" in report
    _, verdict = compare.build_report(runs=[fake_run(slow_factor=1.0)], thresholds=THRESHOLDS)
    assert verdict["regressions"] == [] and verdict["possible"] == []


def test_report_puts_wrong_answers_first():
    run = fake_run(slow_factor=1.0)
    run["correctness"]["failures"].append({"label": "new", "rep": 0, "workload": "tpch", "name": "q01",
                                           "detail": "row 0: [1] expected [2]"})
    report, verdict = compare.build_report(runs=[run], thresholds=THRESHOLDS)
    assert verdict["correctness_failures"] == 1
    assert report.index("row 0: [1] expected [2]") < report.index("## Flagged changes")


def test_stored_baseline_round_trips_and_must_match_settings(tmp_path):
    run = fake_run(slow_factor=1.5)
    paths = compare.save_baselines(run=run, directory=tmp_path)
    assert sorted(p.name for p in paths) == ["new.json", "old.json"]
    stored_old = json.loads((tmp_path / "test" / "sf1-t2" / "old.json").read_text())
    current = copy.deepcopy(run)
    current["servers"].pop("old")
    current["repetitions"] = [r for r in current["repetitions"] if r["label"] == "new"]
    _, verdict = compare.build_report(runs=[current, stored_old], thresholds=THRESHOLDS)
    assert "tpch/q01" in {r["metric"] for r in verdict["regressions"]}
    stored_old["config"]["threads"] = 8
    with pytest.raises(SystemExit, match="threads"):
        compare.build_report(runs=[current, stored_old], thresholds=THRESHOLDS)


def test_one_repetition_is_never_judged():
    run = fake_run(slow_factor=1.5)
    run["repetitions"] = [r for r in run["repetitions"] if r["rep"] == 0]
    report, verdict = compare.build_report(runs=[run], thresholds=THRESHOLDS)
    assert verdict["regressions"] == [] and verdict["possible"] == []
    assert "fewer than 3 repetitions" in report


@pytest.mark.parametrize(("factor", "blocked"), [(1.04, False), (1.06, True)])
def test_release_gate_upstream_budget_and_replay(tmp_path, factor, blocked):
    import subprocess

    run = fake_run(slow_factor=1)
    run["config"].update(release_gate=True, warm_passes=1, mp_runs=2)

    def measurements(value):
        return {"tpch": [{"state": "cold", "suite_s": value, "statements_s": {}},
                         {"state": "warm", "suite_s": value,
                          "statements_s": {f"q{i:02}": value for i in range(1, 23)}}],
                "many-pipelines": {name: [value, value] for name in workloads.MP_QUERIES}}

    for record in run["repetitions"]:
        record.update(measurements(1))
        if record["label"] == "new":
            record["upstream_duckdb"] = measurements(1 / factor)
    report, verdict = compare.build_report([run], THRESHOLDS)
    assert "Upstream DuckDB" in report
    assert all(metric["over_budget"] == blocked for metric in verdict["upstream_duckdb"]["metrics"].values())
    path = tmp_path / "results.json"
    path.write_text(json.dumps(run))
    replay = subprocess.run([sys.executable, str(HERE / "compare.py"), str(path), "--fail-on-regression"],
                            capture_output=True, text=True)
    assert replay.returncode == int(blocked), replay.stderr
    assert replay.stdout.strip() == report.strip()
    del run["repetitions"][-1]["upstream_duckdb"]["tpch"][1]["statements_s"]["q22"]
    with pytest.raises(ValueError, match="incomplete warm TPC-H"):
        compare.build_report([run], THRESHOLDS)


@pytest.mark.parametrize("problem", ["regressions", "correctness_failures", "errors"])
def test_release_gate_rejects_prior_regression_and_errors(problem):
    verdict = dict(regressions=[], correctness_failures=0, errors=0)
    verdict[problem] = [{"metric": "slow"}] if problem == "regressions" else 1
    assert compare.exit_status(verdict, fail_on_regression=True) == 1
    assert compare.exit_status(verdict, fail_on_regression=False) == int(problem != "regressions")


def test_select_previous_published_stable_and_anchors():
    from previous_release import baselines

    published = ["v2.14.1", "v2.14.0", "v2.14.0-rc.1", "v2.13.10", "v2.13.9", "v2.4.1", "python-v2.15.0"]
    for tag in ("v2.14.0", "v2.14.0-rc.2"):
        assert baselines(tag, published, ["2.4.1", "2.13.10"]) == ["2.4.1", "2.13.10"]
        assert baselines(tag, published, ["2.4.1"]) == ["2.4.1", "2.13.10"]
    with pytest.raises(ValueError, match="no published stable predecessor"):
        baselines("v2.4.0", published, [])
