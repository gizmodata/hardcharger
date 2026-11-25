# DuckDB extensions

**Available in v2.1.0+.** Linux, macOS and Windows builds include DuckDB's standard
`core_functions`, `parquet`, `json` and `icu` extensions. Optional extensions—including
`httpfs`, `postgres` (`postgres_scanner`), `ducklake`, `tpch` and `autocomplete`—are runtime
modules. Customers can select compatible modules without rebuilding Hardcharger.

iOS retains its built-in optional extensions and disables downloading/loading executable
extensions. The CLI and web SQL editor use their own SQL completion; they do not require
DuckDB's `autocomplete` extension. Install it only when using its SQL functions directly.

## Automatic installation and loading

Non-iOS servers enable DuckDB's `autoinstall_known_extensions` and
`autoload_known_extensions` by default, matching GizmoSQL. Using a feature of a known
extension can install and load it on demand. For example, `CALL dbgen(sf = 0.01)` uses
`tpch` without a preceding `INSTALL` or `LOAD`.

Downloads and installed modules belong to the **server computer**, under its configured
extension directory, not to the client running the SQL. First use needs access to the
repository and a writable cache. The default uses DuckDB's release repository for the
server's exact DuckDB version and platform. Hardcharger's version number is separate.
Signature verification remains enabled; enabling automatic loading does not allow
unsigned or ABI-incompatible modules.

Explicit SQL remains useful for predictable startup or extensions DuckDB cannot discover
automatically. For a DuckLake/PostgreSQL/S3 deployment, put this before your secret and
ATTACH statements in `--init-sql-commands-file`:

```sql
INSTALL ducklake; INSTALL postgres; INSTALL httpfs;
LOAD ducklake; LOAD postgres; LOAD httpfs;
```

An installed module survives a restart. Loaded modules belong to the running server
instance; they are loaded again in the next instance. With access control, explicit
extension-management commands and changes to extension settings require the server's
existing extension-management authority.

## Choose or replace a module

An administrator can use a compatible repository:

```sql
FORCE INSTALL ducklake FROM 'https://extensions.example.com';
LOAD ducklake;
```

The repository must serve a signed module compatible with the server's DuckDB version,
platform and extension ABI. For automatic installation, configure
`autoinstall_extension_repository` in startup SQL. `custom_extension_repository` selects
the repository for ordinary explicit installations. A preinstalled compatible module can
also be loaded by its path. Changing a cached module does not unload an already-loaded
copy: restart the server before loading the replacement. Preserve the cache directory
across container replacements when you want to reuse installed modules.

## Offline or administrator-controlled installations

Preinstall the required compatible modules into a persistent extension directory. To
require explicit installation and loading, set both options in startup SQL:

```sql
SET GLOBAL autoinstall_known_extensions = false;
SET GLOBAL autoload_known_extensions = false;
LOAD ducklake;
```

To allow loading cached modules while preventing automatic downloads, disable only
`autoinstall_known_extensions`. Missing modules then fail with an extension error.
Dependencies need installation too; for example, `iceberg` needs `avro`. Explicit startup
SQL can run `INSTALL iceberg; INSTALL avro; LOAD iceberg;` before disabling downloads.
Configuring a PostgreSQL access-control store has its own existing setup step that
installs its required scanner if missing.

## DuckLake metadata compatibility

Hardcharger no longer carries its DuckLake metadata storage-version patch. Native modules
and iOS's built-in DuckLake use upstream behavior. When DuckLake attaches a DuckDB
metadata file with the latest storage version, writes/checkpoints can upgrade the file;
an older DuckDB may then refuse to open it. Back up metadata before changing versions.

`--storage-version` controls plain DuckDB databases; it does not override the metadata
version DuckLake explicitly chooses. DuckLake's catalog format and automatic catalog
migration are separate from the metadata file's storage format. Use the compatibility
controls documented for your chosen upstream DuckLake version. Hardcharger does not
promise that an older DuckDB can reopen the metadata after newer-server writes.
PostgreSQL metadata does not have a DuckDB file storage version.

## NVIDIA Sirius (GPU execution)

[Sirius](https://duckdb.org/docs/2.0/core_extensions/sirius) runs DuckDB plans on an NVIDIA GPU
(`LOAD sirius`, usually in the startup SQL). The server changes four things when it loads:

- **S3 stays DuckDB's.** Sirius registers a file system for every `s3://` path that is read-only and
  serves only its GPU scans, ahead of httpfs. The server takes it out as soon as Sirius loads, so
  httpfs serves `s3://`: `COPY ... TO 's3://...'`, DuckLake and Iceberg writes to S3, and every CPU
  read of S3 work as without Sirius. GPU scans of S3 Parquet read with Sirius's own client, as
  before. Sirius reads S3 only with a static `TYPE s3` (`PROVIDER config`) or `TYPE sirius_s3`
  secret. Sirius never replays a statement that reads S3 on the CPU: a statement that reads S3
  through a scan Sirius doesn't run on the GPU (`read_csv`, `read_json`, a DuckLake table's scan)
  runs on the CPU from the start, with `gpu_execution` off for that statement only; a GPU plan
  over S3 Parquet that fails at run time still fails (Sirius's rule).
- **Functions that run SQL or take a table by name are the administrator's.** `gpu_execution('<sql>')`
  runs its SQL on a connection of its own, where access control doesn't apply; `pin_table`,
  `unpin_table`, `sirius_knn_search` and the ANN-index functions take a table by name;
  `sirius_stream_source`, `reset_sirius_cache` and the profiler act on the whole server. Under
  access control they are refused to every store user, and `GRANT EXECUTE` on them is refused. A
  store user's queries run on the GPU without them, under every grant, row policy and mask.
- **`sirius_read_parquet()`** (Sirius's internal rewrite of `read_parquet('s3://...')`) reads with
  its own I/O, past the server's file checks: it takes one `s3://` literal only, for everyone.
- **Settings.** Every Sirius setting (`gpu_execution`, `enable_duckdb_fallback`, `spill_compression`,
  ...) needs MANAGE CONFIG in any scope, as `SET GLOBAL` does: several act on the whole server
  whatever the scope. `sirius_log_backend`, `sirius_log_dir`, `sirius_log_level`,
  `pin_table_input_compression_plan_dir` and the `sirius_exchange_*` settings make Sirius open files
  or network connections of its own: set them in the startup SQL (or Sirius's environment and
  `sirius.yaml`); after startup they are refused to everyone, the administrator included.
