# ClickBench pull request: Hardcharger (draft, internal)

Open this **only after Hardcharger 2.0.0 is public** (the repository, its GitHub releases, and the
one-line installer answering without a token). ClickBench's CI clones the PR branch on a fresh EC2
VM and runs `hardcharger/benchmark.sh` there; with a private release, `install` fails in seconds.

## Title

Add Hardcharger

## Description

(ClickBench's PR template text first, as GizmoSQL's PR #745 had it, then:)

> Adds Hardcharger, the successor to GizmoSQL (the `gizmosql` entry): a SQL server on DuckDB 2.0
> that speaks Warp SQL, an HTTP protocol returning Apache Arrow, instead of Arrow Flight SQL.
> Open source, Apache 2.0.
>
> **Source and release**
> - https://github.com/gizmodata/hardcharger, release `v2.0.0` (Linux amd64 and arm64 tarballs,
>   `SHA256SUMS` attached).
> - Installed with the official one-line installer, https://install.hardcharger.gizmodata.com
>   (source: https://github.com/gizmodata/hardcharger-install), pinned to `2.0.0` by
>   `HARDCHARGER_VERSION` in `install`. It checks the download against the release's `SHA256SUMS`.
>
> **Relation to the `gizmosql` entry**
> - Same shape: `install`, `start`/`stop`/`check` around one server process, `load` and `query`
>   through the vendor's CLI client (`hardcharger`, a port of `gizmosql_client`), `create.sql`
>   from the `duckdb` entry, `load.sql` and `queries.sql` unchanged.
> - `query` decides failure from the client's exit status (`--bail`), not by grepping the output,
>   so a result row containing the word "error" cannot turn a timing into `null`.
> - `stop` waits until the process has exited (the next `start` needs the database file's lock).
> - Default configuration: all CPUs, DuckDB's default memory limit, `--storage-version latest`
>   (as the `duckdb` and `gizmosql` entries), no indexes, no tuning; `tuned: no`.
> - Tags as `gizmosql`: `C++`, `column-oriented`, `DuckDB derivative`.
>
> **Reproduction**
> - The committed `results/<date>/c6a.4xlarge.json` comes from `benchmark.sh` on a fresh
>   c6a.4xlarge (Ubuntu 24.04, 500 GB gp2), set up as `cloud-init.sh.in` does, unattended.
>   Load <LOAD> s; the whole run takes about <MIN> minutes.
> - Linux amd64 and arm64 builds are released, so every machine in `machine:all` should work.
>   Could a maintainer add `machine:all`?

## Files

Copy `bench/clickbench/hardcharger/` to the ClickBench checkout as `hardcharger/`, add
`hardcharger/results/<YYYYMMDD>/c6a.4xlarge.json` from the GA run, and nothing else (no change
to `lib/`, the website, or other entries).

## GA-day steps (after `v2.0.0` is public)

1. Confirm the release is anonymous-readable and the installer works without a token, on a machine
   with no `GH_TOKEN` and gh signed out:

   ```bash
   curl -fsSI https://github.com/gizmodata/hardcharger/releases/download/v2.0.0/hardcharger-linux-amd64.tar.gz | head -1
   curl -fsSL https://install.hardcharger.gizmodata.com | HARDCHARGER_INSTALL_TOKEN=none HARDCHARGER_VERSION=2.0.0 sh
   ```

2. Re-run on the released build, through the real one-liner (no mirror), from this directory:

   ```bash
   cd bench/clickbench/dry-run
   ( set -a; . <aws .env>; set +a                  # never printed
     TYPE=c6a.4xlarge LIFETIME_MIN=120 bash provision.sh up
     bash run.sh push 2.0.0
     bash run.sh start
     bash run.sh wait
     bash run.sh pull out-2.0.0
     bash provision.sh down )                     # ends with the teardown proof
   d=$(date -u +%Y%m%d)
   mkdir -p ../hardcharger/results/$d
   python3 make-result.py --log out-2.0.0/ClickBench/hardcharger/log \
     --template ../hardcharger/template.json --date "$(date -u +%F)" > ../hardcharger/results/$d/c6a.4xlarge.json
   python3 compare.py ../hardcharger/results/$d/c6a.4xlarge.json \
     <ClickBench>/gizmosql/results/20260511/c6a.4xlarge.json
   grep -c '"version"' out-2.0.0/ClickBench/hardcharger/log; grep 'Hardcharger 2.0.0' out-2.0.0/ClickBench/hardcharger/log
   ```

   Check: 43 rows, no `null`, load time and totals in line with the dry run, the log's last line
   says `Hardcharger 2.0.0`.

3. Validate with ClickBench's own checker, in a fresh clone of upstream main:

   ```bash
   git clone --depth 1 https://github.com/ClickHouse/ClickBench /tmp/cb && cd /tmp/cb
   cp -R <hardcharger checkout>/bench/clickbench/hardcharger hardcharger
   git clone --depth 1 https://github.com/ClickHouse/ClickBench /tmp/cb-base
   python3 validate-results.py . --base /tmp/cb-base     # no new tags, 43 x 3, valid JSON
   ```

4. Open the PR from the existing public fork `gizmodata/ClickBench` (the one GizmoSQL's PRs came
   from), on a new branch from upstream main:

   ```bash
   cd <checkout of gizmodata/ClickBench, remote upstream = ClickHouse/ClickBench>
   git fetch upstream && git switch -c hardcharger upstream/main
   cp -R <hardcharger checkout>/bench/clickbench/hardcharger hardcharger
   git add hardcharger && git commit -m "Add Hardcharger"
   git push origin HEAD:refs/heads/hardcharger
   git ls-remote origin refs/heads/hardcharger                 # equals the commit
   gh pr create -R ClickHouse/ClickBench --head gizmodata:hardcharger --title "Add Hardcharger" --body-file <body>
   ```

   The CLA assistant asks for a signature on the first PR from a new committer.

5. After the PR is open: ClickBench's `benchmark-pr.yml` runs after a maintainer approves it;
   `machine:<type>` labels choose machines. Because the fork is organization-owned, their
   collector cannot push to it and posts the result files as pastila.nl links: commit those files
   to the PR branch as they are.

## Links that are public at GA

- https://github.com/gizmodata/hardcharger
- https://github.com/gizmodata/hardcharger/releases/tag/v2.0.0
- https://install.hardcharger.gizmodata.com (source: https://github.com/gizmodata/hardcharger-install)
- https://github.com/gizmodata/warp-sql
