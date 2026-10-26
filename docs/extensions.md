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
