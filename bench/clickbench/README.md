# ClickBench submission (internal until Hardcharger 2.0.0 is public)

- [`hardcharger/`](hardcharger/) is the entry, exactly as it goes into
  [ClickHouse/ClickBench](https://github.com/ClickHouse/ClickBench) as `hardcharger/` (copy the
  directory as is; `results/` is added by the GA run).
- [`PR.md`](PR.md) is the pull request draft and the GA-day steps.
- [`dry-run/`](dry-run/) is our tooling for running the entry on an AWS VM the way ClickBench's
  `run-benchmark.sh` + `cloud-init.sh.in` do, without sending anything to ClickBench's results sink,
  and the dry-run results. **The dry-run results are from 2.0.0-rc.1 and are not for submission.**

```bash
cd bench/clickbench/dry-run
set -a; . <aws .env>; set +a                       # in a subshell; never printed
TYPE=c6a.4xlarge LIFETIME_MIN=120 bash provision.sh up
bash run.sh push 2.0.0                             # public release: the VM runs the real one-liner
# (an unreleased build: bash run.sh push 2.0.0-rc.1 <dir with v2.0.0-rc.1/{tarball,SHA256SUMS}>)
bash run.sh start && bash run.sh wait
bash run.sh pull out/
python3 make-result.py --log out/ClickBench/hardcharger/log --template ../hardcharger/template.json \
  --date "$(date -u +%F)" > c6a.4xlarge.json
python3 compare.py c6a.4xlarge.json <ClickBench>/gizmosql/results/20260511/c6a.4xlarge.json
bash provision.sh down                             # ends with `check`: the teardown proof
```
