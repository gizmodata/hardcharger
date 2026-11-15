# Named administrators: `SYSTEM ADMIN`

Design note for the System Admin capability (2.14.0). It started as an assessment of named
administrative accounts and delegated account management; the first is built as described
here, the second is not (see "Not built").

## What it is

Hardcharger has one privileged principal that is not in the access-control store: the
administrator the server is started with (`admin_username` and its password, a sign-in
token whose role is `admin`, or a certificate in `mtls_admin_identities`). `SYSTEM ADMIN`
lets named store users do what it does, attributed to them:

```sql
GRANT SYSTEM ADMIN TO alice;      -- or TO ROLE dba
SET ROLE SYSTEM ADMIN;            -- alice's session acts as the administrator
RESET ROLE;                       -- and is alice again
REVOKE SYSTEM ADMIN FROM alice;
```

The owner's decisions, which the rest follows from:

1. A session acting as System Admin is the full equivalent of the administrator: no lesser
   "operations" variant hides behind the name.
2. Holding it is not enough. A session starts as its user and asks (`SET ROLE SYSTEM
   ADMIN`), per session, and can drop it again; nothing carries it to another session.
3. It is granted explicitly, to a user or a role, only by the administrator or a session
   acting as System Admin; never part of `ALL PRIVILEGES`, never delegated.
4. It is asked of the grants on every request. Nothing remembers that a session "is admin".
5. It never widens what a session is limited to.
6. The administrator stays independent of the store: it is the way back in.
7. No statement a non-administrator may run reaches it indirectly.
8. Everything about it is in the audit chain, under the user's own name and id.
9. It is visible: who holds it, through what, and which sessions act as one now.

## Syntax, and why two words

`SYSTEM ADMIN` is written as the system privileges are (`READ FILESYSTEM`, `MANAGE
CATALOGS`): a keyword phrase, not an identifier. That is what makes it safe to add:

- No role is named by two unquoted words, so no statement a 2.13 server accepted changes
  its meaning. `GRANT SYSTEM ADMIN TO x` and `SET ROLE SYSTEM ADMIN` were syntax errors.
- A store an earlier version made may hold a role called `sysadmin` (people coming from
  Snowflake create one), with members and with admin-option holders. Had the capability
  been the single word `SYSADMIN`, then after the upgrade `GRANT sysadmin TO bob`, in a
  script that meant that ordinary role, would have handed bob the server. With two words
  the role stays what it was, `SET ROLE sysadmin` still selects it, and nothing is reserved
  but the name `SYSTEM ADMIN` itself for new users and roles.
- A role that an earlier store does call `"SYSTEM ADMIN"` (quoted, with the space) is
  written quoted and is a role. No decision reads the active role's *name*: the session's
  state carries a reserved id (`kSystemAdminRoleId`, no role's id), and audit records say
  `acting_as` only from that state.

Asking fits `SET ROLE` because it is the same act: choosing what the session acts with.
`RESET ROLE`, `SET ROLE ALL`, `SET ROLE NONE` and `SET ROLE <role>` all leave it, so there
is one way in and no second vocabulary for the way out.

Alternative considered: `GRANT SYSADMIN` / `SET ROLE SYSADMIN` as one reserved word (shorter,
and familiar). Rejected for the upgrade hazard above; it would also have needed a rule for
stores that already have that role.

## How it works

**In the store.** One table, `system_admins (principal_kind, principal_id, granted_by)`,
created `IF NOT EXISTS` when a 2.14 server opens a store. The schema version is unchanged
(as for `storage_privileges` in 2.2), so a 2.13 server still opens the store and simply does
not read the table. Records are by id, so a dropped and recreated name holds nothing.
`granted_by` is the id of the System Admin who granted it, for `SHOW GRANTS`; the grant
stands without them.

**Who holds it** (`Snapshot::SystemAdminSources`, `model.cc`). An enabled, system-scope
store user holds it by a record of its own, or through a role with a record that the user
holds *through memberships whose `granted_by` is empty* (the administrator's, which a
System Admin's are too) or through an identity-provider group mapping (which only they
create). A membership that a role's admin-option holder or a tenant admin granted never
carries it. That one rule is what closes delegation, in every order of events and for
every server version writing to the store: the capability can only be reached through
rows that an administrator wrote. Records that name a tenant's principal, a built-in role
(`PUBLIC`), or nothing that exists are dropped when the snapshot is built.

**What a session is** (`RbacSessionState::principal()`, `enforcement.cc`). `SET ROLE SYSTEM
ADMIN` sets the session's active role to the reserved id. Every time anything asks who the
session is, the state reads the snapshot the engine holds at that moment: if the user holds
`SYSTEM ADMIN` there, the answer is a superuser principal in exactly the administrator's
shape (no user id, so what it creates is system-owned and what it grants has no grantor)
that also names the user (`acting_user_id`); if not, it is the user with the reserved
active role, which `Snapshot::RolesOf` gives *no role at all*, not even `PUBLIC`. Failing
to hold it therefore fails closed everywhere, including in code that never heard of the
feature; the explicit checks only make the message clear (`user alice no longer holds
SYSTEM ADMIN, which SET ROLE made active. Run RESET ROLE to go on as alice`). Nothing is
cached: the answer is computed from the current snapshot on every call.

Everything in the server keyed on "this is an administrator" (`Session::admin()`,
`full_admin()`, `SessionGuard::unrestricted()`, `RbacSessionState::unchecked()`) asks that
same question each time, so KILL SESSION, `SET GLOBAL`, the SQL monitor, the server's own
catalogs, the statement-queue bypass and the plan check all follow, and all stop following
at the same instant.

**Changes to the store** by a session acting as System Admin are checked a second time
inside the change's own store transaction (`RequireStillSystemAdmin`, `engine.cc`), which
reads the store after every writer before it. A revoke committed on another replica stops
the change even if this replica has not polled yet.

**Through an identity-provider group** the holding is as current as the session's sign-in
token: dropping the mapping, or revoking `SYSTEM ADMIN` from the role, ends it at the next
statement like any other change to the store; taking the user out of the group at the
identity provider ends it when a token that lists the group stops being accepted, as for
every role a group gives. (A session belongs to the authority of the token that opened it,
groups included, so a later token without the group cannot use it.)

**Asking** reads the store, not the replica's copy (`RbacEngine::Refresh` before the
check), so a grant is usable at once on every replica, a revoke refuses at once, and a
store that does not answer elevates nobody.

## Decisions, with the alternative each time

Each took the safer side. Any can be overruled.

1. **Two words** (`SYSTEM ADMIN`), not `SYSADMIN`. Above.
2. **A role carries it only through administrator-granted memberships.** Alternative: every
   member of a role that holds it, with a rule that only administrators may then change the
   role's members. That rule is also enforced (`role dba holds SYSTEM ADMIN; only the
   administrator, or a user acting as System Admin (SET ROLE SYSTEM ADMIN) grants or revokes
   it`), but by itself it depends on every writer applying it: a member an admin-option
   holder added *before* the role got `SYSTEM ADMIN`, or added through a 2.13 replica on
   the same store, would have been an administrator. The administrator's own `GRANT role TO
   user` of such a membership takes it over.
3. **Losing it refuses the session's statements until `RESET ROLE`**; it does not fall back
   to the user silently. This is what a revoked `SET ROLE` role already does. Alternative:
   continue as the user. Rejected because a script that was reading every row would go on,
   without an error, reading only the rows the user's policies allow, and writing from them.
4. **What the session did before stands.** A revoke reaches the next statement; an open
   transaction's earlier writes are the user's to commit or roll back after `RESET ROLE`
   (as after any revoke in Hardcharger). Alternative: roll the transaction back, or refuse
   its commit. Not taken: the writes were authorized when made, autocommit would have made
   them durable already, and the SDK's commit path does not go through the statement check.
   A statement still *running* is interrupted.
5. **Its grants stand when it loses `SYSTEM ADMIN`** (they are recorded as the
   administrator's, with no grantor), including `SYSTEM ADMIN` it granted to others.
   Alternative: cascade, as delegated grants do. Rejected: it is not delegation (decision
   3 of the owner's list), and a cascade would silently remove users', roles' and
   policies' grants made by an administrator who has since left.
6. **Refusal, not restriction, for limited sessions.** A scoped token, a token with
   `catalog_access` rules, a `read_only` connection, the `readonly` sign-in role and a
   tenant's user cannot ask, and the refusal names which. Alternative: allow it and keep
   the restriction. Rejected: "administrator, but only on these catalogs" is the lesser
   variant the name must not hide, and each such combination would be a new code path.
   A session acting as System Admin that turns `read_only` stops acting as one.
7. **A tenant's users and roles never hold it**, and the grant is refused. Tenant admins
   manage their tenant's users (passwords, enable, drop); a holder inside a tenant would be
   theirs to take over.
8. **Asking needs the store to answer**, and a session acting as System Admin is a store
   user for `rbac_max_snapshot_age_seconds` (refused past it, unlike the administrator).
   Alternative: let it ride the last snapshot like a sign-in does. Rejected: the
   administrator is the designed way in during a store outage, and "the store's last word"
   is too weak a basis for unrestricted power.
9. **The external policy service is asked when a session asks** (action `system_admin`),
   and not for the statements that follow (as for the administrator). No answer means no.
   Alternative: never ask it. Rejected: a deployment that puts a policy service in front of
   every statement would otherwise have it bypassed by a grant.
10. **Every statement run as System Admin is an audit record**, with its SQL as the log
    shows SQL (credential statements whole, credential values masked), whatever
    `print_queries` says. The administrator's own statements are still not audited one by
    one. Alternative: only access-control statements. Rejected: attribution is the point.
11. **A damaged `system_admins` table is not read.** The server starts, logs it, nobody
    holds `SYSTEM ADMIN` and it cannot be granted until the table is repaired or dropped (a
    start creates it). Alternative: fail startup (the store's other tables do). Rejected by
    owner's decision 6. Alternative: repair it automatically. Rejected: it would destroy
    the evidence of what happened to it.
12. **No cache, not even one keyed by the snapshot's version.** It would be correct (a
    snapshot is immutable) and would save a walk over the memberships per call for sessions
    acting as System Admin; it is left out so that "asked every time" needs no argument.

## What a non-administrator can reach

Every statement that changes users, roles, memberships, mappings or tokens, and who may
run it against a holder:

| Statement | Non-administrator |
|---|---|
| `GRANT` / `REVOKE SYSTEM ADMIN` | Refused. No option, owner or tenant admin stands in. |
| `CREATE` / `ALTER` / `DROP USER` (password, enable, disable, attributes) | Refused for system-scope users, as before (`only administrators manage users and roles`). A tenant admin reaches its tenant's users, none of whom can hold it. |
| `CREATE` / `DROP ROLE` | Same. A new role holds nothing; a recreated name holds nothing. |
| `GRANT` / `REVOKE role` where the role holds it (itself or through roles) | Refused, whatever admin option. Elsewhere unchanged, and what an admin-option holder grants does not carry it. |
| `GRANT other TO <role that holds it>` | Allowed: it gives the role's holders more, and no one `SYSTEM ADMIN`. |
| `CREATE` / `DROP GROUP MAPPING` | Refused, as before. |
| `CREATE SCOPED TOKEN … FOR USER other` | Refused, as before; and no scoped token ever acts as System Admin. |
| `SET ROLE` | Only roles the user holds. There is no statement that makes a session another user. |
| Tenants, policies, `ALTER … OWNER TO` | Do not affect who holds it. |
| Sign-in tokens | The identity provider decides groups; a group reaches it only through a mapping an administrator made. |

The administrator's credentials are configuration. `block_sensitive_paths` (on by default)
keeps the config file, the init SQL file and `/proc` out of every statement's reach,
a System Admin's included, and DuckDB's `getenv` is not available in the server.

## Audit

Records are in the existing chain (`audit.cc` is unchanged: each record lists the fields
its digest covers, so new fields change nothing for old records, and a 2.13 chain verifies).
All access-control records now carry `user_id` and `session_id`; access-control statements
carry `target` / `target_ids` and `privilege`, `membership` / `membership_ids`, `object`,
`option`, `change` as they apply (`audit_fields.cc`, derived from the parsed statement, so
refused statements are described too and no field can come from the SQL text). New:
`System Admin elevation`, `… refused`, `… ended` (once per elevation, whatever ends it),
`statement run as System Admin`, `administrative command run as System Admin`, and
`acting_as` on everything done that way.

As before, audit records follow the log level: with `log_level` above `info` the
successful ones are not written. And a local chain cannot prove that its newest records
were not cut off; ship the log elsewhere as it is written if that matters.

## Mixed versions

A 2.13 replica on the same store does not read `system_admins`: nobody holds `SYSTEM ADMIN`
there and `SET ROLE SYSTEM ADMIN` is a syntax error. It also does not apply the refusals
above, but nothing it lets a non-administrator write confers the capability (decision 2).
What it does not do is show or audit it, so grant it once every replica runs 2.14.

## Clients

Asking and dropping are plain SQL, so the `hardcharger` client and the web UI's SQL editor
need nothing (tested for the client). Neither replays `SET ROLE` to a session it opens to
replace a lost one; both refuse to re-run a statement after losing a session in which a
`SET` ran. What does not follow it, because it reads the sign-in's role rather than the
session:

- the client's `.show` prints `role: user`, and its completions keep the list they had
  until `.refresh`;
- the web UI decides once, at sign-in, whether to offer the SQL Monitor, Server
  Configuration, "End session" and the "SQL details" link, shows the role `user`, and runs
  those pages and the schema browser in sessions of its own, which are not the editor
  tab's. An elevated tab can query the same functions by hand.

Making the UI offer a "System Admin" switch that sends the statement on its own sessions
is a UI change of its own.

## Not built

- Distinct system privileges for account management (`CREATE USER`, `ALTER USER`, `DROP
  USER`, `CREATE ROLE`, `DROP ROLE`) with a target boundary, for delegation below a full
  administrator. The owner chose the full equivalent; these remain possible beside it.
- Export of the audit chain to an independent append-only destination.
