# Named administrators (`SYSTEM ADMIN`) and delegated account management

Design note for the System Admin capability and the account privileges (`CREATE USER`, `ALTER
USER`, `DROP USER`, `CREATE ROLE`, `DROP ROLE`), both 2.14.0. It started as an assessment of
named administrative accounts and delegated account management; both are built as described
here (the account privileges in "Delegated account management" below).

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
that also names the user (`acting_user_id`); if not (or if the store did not answer when
this request asked it, below), it is the user with the reserved
active role, which `Snapshot::RolesOf` gives *no role at all*, not even `PUBLIC`. Failing
to hold it therefore fails closed everywhere, including in code that never heard of the
feature; the explicit checks only make the message clear (`user alice no longer holds
SYSTEM ADMIN, which SET ROLE made active. Run RESET ROLE to go on as alice`). Nothing is
cached: the answer is computed from the current snapshot on every call.

**The snapshot is the store's own word, for these sessions.** Every other check in
Hardcharger reads the snapshot the server last loaded, which a poll refreshes about once a
second and which keeps being served while the store is away. That is not good enough for
unrestricted power: a revoke on another replica would count a second late, and on a
replica that cannot reach the store, never. So before every request of a session that
asked for `SYSTEM ADMIN` (`SessionGuard::VerifySystemAdmin`, called from
`Session::CheckAlive` and again when the statement leaves the statement queue), the server
reads the store's data version, loads the store if it moved, and, if the store did not
answer, marks the session unverified: it then acts as nobody until the store answers or it
says `RESET ROLE`. The cost is one small store read per statement, for these sessions only.
A statement that is already *running* when the grant goes is stopped when its server hears
of it (at once on the replica where the change was made, at the next poll on the others):
"the next statement" is the guarantee, and the interrupt is on top of it.

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

**Asking** reads the store the same way (`RbacEngine::Refresh` before the check), so a
grant is usable at once on every replica and a store that does not answer elevates nobody.

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
8. **Acting as System Admin needs the store to answer, at every statement**: to start, and
   to go on. Alternative: let such a session ride the last snapshot, as a sign-in and every
   other grant does (bounded only by `rbac_max_snapshot_age_seconds`, which is off by
   default). That was the first implementation, and a review of it showed the consequence:
   923 administrator-only statements ran on a second replica in the 0.8 s after a revoke
   had committed on the first, and a replica cut off from the store kept a revoked,
   disabled user as System Admin for as long as it was cut off. The administrator is the
   designed way in during a store outage; a System Admin waits for the store.
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
13. **A session that asked and does not act as one runs nothing**, the session's own
    commands (`SET hardcharger.*`, `KILL SESSION`) included, and metadata requests list
    nothing. Only `SET ROLE` / `RESET ROLE` and ending the transaction through the protocol
    get it out.

## What a non-administrator can reach

Every statement that changes users, roles, memberships, mappings or tokens, and who may
run it against a holder:

| Statement | Non-administrator |
|---|---|
| `GRANT` / `REVOKE SYSTEM ADMIN` | Refused. No option, owner or tenant admin stands in. |
| `CREATE` / `ALTER` / `DROP USER` (password, enable, disable, attributes) | Refused for a user who holds it, whatever account privilege the actor holds (`user sam holds SYSTEM ADMIN; only the administrator, or a user acting as System Admin … changes it`). A tenant admin reaches its tenant's users, none of whom can hold it. |
| `CREATE` / `DROP ROLE` | `DROP ROLE` of a role that carries it: refused the same way. A new role holds nothing; a recreated name holds nothing. |
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
  until `.refresh`.

The web UI follows the session (2.14.0). Each editor tab knows whether its session acts as
System Admin only from the session's own answer to `HARDCHARGER_IS_SYSTEM_ADMIN()`, which it
asks after anything that may change it: a `SET`, `RESET` or `EXECUTE` in the tab, a
statement refused while the tab acted as one (a revoke shows up as a refusal), a change of
role or read-only mode, and a refusal as System Admin on one of the admin pages. While the
session is refused as System Admin (it asked and does not act as one), the tab asks at every
read, since the server may undo that (a grant again, the store back) until `RESET ROLE`. A
statement that succeeds needs no question: a session that asked and does not act as one runs
nothing. A new or replacement session is never one, and the tab never says `SET ROLE SYSTEM
ADMIN` on its own (the Run as picker quotes role names, so it never names it either).

While at least one tab on the server acts as System Admin, the UI offers the Server
Configuration and SQL Monitor pages and the Sessions page's view of every session with End.
Each of those pages runs in a session of its own (never inside an editor tab's transaction or
queue), and for a user that session says `SET ROLE SYSTEM ADMIN` itself and confirms it with
`HARDCHARGER_IS_SYSTEM_ADMIN()` before anything else, so the server asks the grants and the
store for every statement those pages send, as for the tab. That is a second elevation,
audited like the first; it is made only while the user has one of their own in a tab, and it
ends (the page's session is closed) as soon as no tab acts as one. A refusal as System Admin
on such a page has every tab ask its session again and counts them as not acting as one until
they answer.

Still following the sign-in, not the session: the role shown next to the user (`user`), the
"SQL details" link to a recorded statement (it opens a window of its own, with no editor tab
to follow), and the schema browser and completions, which list what the user's own sessions
see (an elevated tab still runs anything on any catalog; it just isn't listed). Listing as
System Admin would need a long-lived elevated listing session beside the tab's.

## Delegated account management

Below `SYSTEM ADMIN`, five system privileges let a help desk manage accounts without reading
data: `CREATE USER`, `ALTER USER`, `DROP USER`, `CREATE ROLE`, `DROP ROLE`, one statement each,
granted to roles with the existing machinery (`GRANT CREATE USER, ALTER USER TO helpdesk`).
The administrator, a session acting as System Admin and a tenant admin in its tenant are
unaffected by them. Account management is a takeover path (a new password for an
administrator is the administrator), so the design is mostly the target boundary.

The owner's requirements:

1. No altering, dropping, disabling or resetting the password of a user who holds `SYSTEM
   ADMIN` (or the server administrator), nor of a user who holds a role the actor doesn't hold.
2. Only roles the actor holds `WITH ADMIN OPTION` can be granted (membership delegation keeps
   its admin option); creating a role confers no right to grant arbitrary roles.
3. `CREATE ROLE` never hands out privileges the actor lacks.
4. Changing policy attributes, enabling a privileged account or altering a more powerful role
   are refused the same way.
5. Tenant delegates stay inside their tenant; scoped and catalog-restricted tokens never use
   these privileges.
6. They are granted only by an unrestricted administrator, with no admin option.
7. Passwords set through them obey the 2.14.0 password policy.
8. Every account change and every denial is a structured audit record, never with a password,
   hash or token.
9. A revoke takes effect on the next statement on every replica.

### How it works

**Who may** (`AuthorizeAccountStatement`, `engine.cc`). An unrestricted administrator
(`Principal::is_unrestricted_administrator()`), a tenant's `tenant_admin` for a name in its
tenant (as before), or a holder of the statement's account privilege for a system-scope
name. Anyone else gets the old refusal, now naming the privilege (`only administrators manage
users and roles on this server (and, for system-scope users and roles, a role granted the
CREATE USER privilege)`).

**Holding one** (`Snapshot::AccountPrivilegeRoles`, `model.cc`). Of the roles the session
acts with (`RolesOf`, which `SET ROLE` narrows), those the user reaches through memberships
whose `granted_by` is empty (the administrator's, which a System Admin's are too) and through
identity-provider group mappings (which only they create); the same rule as `SYSTEM ADMIN`'s
(`AdministratorGrantedRolesOf`, shared). Never for a scoped token, a token with
`catalog_access` rules (`Principal::catalog_access_token`, new: such rules grant a store user
nothing under access control, but they say the sign-in was meant to be limited), a rules
principal, a role principal, a disabled user or a tenant's user.

**Granting one.** `GRANT CREATE USER …` is refused unless the actor is an unrestricted
administrator, refused `WITH ADMIN OPTION` (even for the administrator), refused to `PUBLIC`
and built-in roles, and refused to a tenant's role (as every system privilege is).
`CanGrantSystemPrivilege` answers no for any non-administrator, and `FallenDelegations` never
lets a delegated record stand, so a hand-edited row passes nothing on. A non-administrator's
`GRANT role TO …` of a role that carries one (itself or through roles it inherits) is refused,
whatever admin option it holds (`role helpdesk holds the account privilege CREATE USER; only
an administrator grants membership in it`); revoking such a membership stays its grantor's.
The administrator's own `GRANT role TO user` of a membership an admin-option holder made
takes it over, as for `SYSTEM ADMIN`.

**The target boundary for users** (`RequireUserWithinReach`). `ALTER USER` and `DROP USER`
reach a user only when it is no more than the session:

- not a holder of `SYSTEM ADMIN` (the existing refusal) and not the server administrator or a
  certificate administrator (`admin is the server administrator, which is set in the server's
  configuration and never managed with SQL`);
- every role it holds (`EffectiveRoleIds`, transitively) is in the session's `RolesOf`;
- every role it holds `WITH ADMIN OPTION` is one the session could grant (`CanAdminRole`);
- every session attribute it has, the actor has with the same value (attributes are what row
  and masking policies read: signing in as the user would see its rows);
- for `ALTER USER`, it owns nothing (signing in as an owner acts as the owner; `DROP USER` of an
  owner was refused already).

This one rule covers a new password, removing it, `ENABLE` (a disabled account that holds more
than the session stays disabled), `DISABLE` and `DROP`. `ALTER USER … SET / UNSET ATTRIBUTE`
is refused to holders outright: setting an attribute is granting data.

**The target boundary for roles** (`RequireRoleWithinReach`). `DROP ROLE` reaches a role
when everything it inherits is in the session's roles, it is itself one of them or holds no
object grant or system privilege of its own, and no row policy names it and no masking policy
exempts it (dropping it would change what those policies decide; a restrictive policy left
with no roles would even apply to everyone). Built-in roles and roles carrying `SYSTEM ADMIN`
are refused as before. `CREATE ROLE` makes an empty role and gives its creator nothing, so the
only way to fill it is with privileges the actor could already grant anywhere.

**Tenants.** A holder reaches system-scope names only (`acme/tom belongs to tenant acme, and
the ALTER USER privilege reaches system-scope users and roles only`); tenant roles cannot hold
the privileges; tenant admins keep their own path, unchanged.

**The store's own word.** Each statement is authorized twice: first against this replica's
snapshot (refreshing once from the store before a refusal, so a grant made elsewhere counts at
once), before any password is hashed; then, authoritatively, inside the change's own store
transaction, against the snapshot that transaction reads after every writer before it. A
revoke committed on any replica therefore stops the very next statement, with no poll to wait
for; and no session state remembers a holding, so there is nothing to invalidate.

**Storage and mixed versions.** The five privileges are written to `storage_privileges`
(location `''`, no admin option), not `system_privileges`: a 2.13 server refuses to load a
store whose `system_privileges` names a privilege it does not know ("corrupt store"), and
skips unknown names in `storage_privileges`. 2.13's `DROP ROLE` already deletes a role's rows
there. No table, no schema version bump; on a 2.13 replica nobody holds them, so it refuses
those statements to everyone but the administrator (checked by hand with a 2.13.1 binary:
it opens such a store, refuses the holder, and its `DROP ROLE` removes the role's rows). A
record with a location, an admin option, a built-in or tenant role or a role that is gone
counts for nothing when the snapshot is built.

**Passwords.** `CheckNewPassword` in `engine.cc` is the one place a password set by `CREATE
USER` or `ALTER USER … WITH PASSWORD` is checked, whoever sets it (after authorization;
`CREATE USER IF NOT EXISTS` of an existing user checks nothing). The 2.14.0 password policy
(`CheckPasswordPolicy`, from the login-guard track) replaces its body when the two meet.

**Audit.** Every access-control record now carries `statement_id` (the statement's Chargewire
id, the one its log lines and `sql_statements` row carry) and, under `SET ROLE`, `active_role`,
refusals included. The five statements name the privilege they need (`privilege`). An account
change (the five statements, a membership `GRANT` / `REVOKE`, a `GRANT` / `REVOKE` of an
account privilege) adds `authorized_by` (`administrator`, `System Admin`, `tenant_admin of
tenant acme`, `ADMIN OPTION on role clerks`, `ALTER USER privilege, through role helpdesk`)
and `before` / `after` (`AccountChangeValues`, `audit_fields.cc`), computed from the store as
the change's own transaction read and left it: a user as `enabled; password login; roles:
clerks WITH ADMIN OPTION; attributes: region`, a role as `members: 2; inherits: none;
privileges: 3 object grants, CREATE USER`, a membership as `user bob in role clerks: yes, WITH
ADMIN OPTION`. A password shows only as `password login` (`(new password)` when the hash
changed, compared and never written), an attribute by its name only.

### Decisions, with the alternative each time

Each took the safer side. Any can be overruled; the open ones are in the track's QUESTIONS.

1. **Five privileges, one statement each**, not one `MANAGE USERS`. Alternative: one
   privilege. Rejected: the assessment asked for distinct ones, and "create but never drop" is
   a real help-desk shape.
2. **Two-word names as `GRANT` writes them** (`CREATE USER`). No role is named by two unquoted
   words, so no statement 2.13 accepted changes meaning.
3. **Held only through the administrator's memberships**, and membership in a carrying role is
   the administrator's to grant. Alternative: let a role's admin option pass membership on.
   Rejected by requirement 6: an admin option on a carrying role is an admin option on the
   privilege.
4. **The target boundary compares with the session, not the user** (`SET ROLE helpdesk`
   narrows what amy reaches). Alternative: every role the user holds. Rejected: a session is
   what acts.
5. **Attributes are a subset check, and setting them is the administrator's.** Alternative:
   ignore attributes. Rejected: they are policy inputs, and a password reset of a user with
   `region = 'eu'` would read the EU rows.
6. **Owners are out of reach for `ALTER USER`.** Alternative: compare ownerships too. Rejected
   as more machinery than the case is worth; the administrator can transfer ownership first.
7. **A new role gives its creator no admin option.** Alternative: PostgreSQL's (the creator
   may grant it). Rejected: an empty role the administrator fills later would stay in the help
   desk's hands.
8. **Not to `PUBLIC`, even by the administrator.** Alternative: allow it. Rejected: every
   user would be a help desk, which no one means.
9. **Tenant roles cannot hold them**; tenant admins are the tenant's delegation.
10. **Checked in the store transaction**, not by a per-statement store read beforehand (as
    `SYSTEM ADMIN` does): the statements change the store anyway, so the transaction is the
    store's word at no extra cost.

### Temporary passwords and self-service changes

A password reset is the ability to sign in as the user, and a password chosen for someone else
is known to whoever chose it. So a password someone else chose is temporary, and the user
replaces it before doing anything else (the owner's decision, 2026-11-18).

**Which passwords are temporary.** Every password a non-administrator sets for another user:
a delegate's `ALTER USER … WITH PASSWORD` and `CREATE USER … WITH PASSWORD`, and a tenant
admin's. An unrestricted administrator's only when it says so: `WITH PASSWORD '…' EXPIRE` on
`ALTER USER` or `CREATE USER` (the Oracle word, after the password it qualifies), or `ALTER USER
name PASSWORD EXPIRE` for the password the user has now (a delegate may do that too, within its
reach). An administrator's password without `EXPIRE`, `PASSWORD NULL` and the user's own change
end it.

**How it is kept.** `password_resets (user_id, password_fingerprint, set_by, set_at_ms)`, a
table of its own created `IF NOT EXISTS` (no schema bump; a 2.13 server never reads it). The
record names the password it was made for by `PasswordFingerprint` (the first 16 bytes of the
SHA-256 of the stored hash, as an access token's `credential` claim does), and
`Snapshot::PasswordMustChange` honours it only while the user's password is still that one. So
a password set later by anyone, a 2.13 replica included, leaves it standing for nothing, and no
code path needs to remember to clear it. `DROP USER` deletes it.

**What such a session may do.** A session is held when it signed in with that very password
(`Identity::credential` equals the record's fingerprint): a sign-in that used no password (an
identity provider's token, a certificate) is not. `SessionGuard::RefuseUntilPasswordChanged`
runs at the start of every request that can run anything, `Session::Execute` (every statement,
access-control ones included), `Prepare`, `ExecutePrepared`, `Metadata` and `Ingest`, and lets
through only `ALTER USER CURRENT_USER | <own name> WITH PASSWORD '…' REPLACE '…'`. It is asked
of the current snapshot at every request, so `ALTER USER x PASSWORD EXPIRE` holds open
sessions from their next statement. Each refusal is an audit record (`statement refused until
the password is changed`, with the request and the SQL as the log shows it). Alternative: allow
harmless statements such as `SELECT 1`. Rejected: every client tried worked without (the web
UI recognizes the refusal and shows its change form; the `hardcharger` client runs the change
as typed), and "harmless" is a list that would grow.

**Self-service.** `ALTER USER CURRENT_USER WITH PASSWORD '<new>' REPLACE '<current>'` (also with
the user's own name) needs no privilege and checks the current password with the password
check sign-in uses, against this replica's snapshot, then once more after a refresh, and the
store transaction confirms the hash is still the one verified. Without `REPLACE`, a
non-administrator's change of its own password is refused. The alternative was a fresh sign-in
check (a token newer than N minutes). Rejected: a stolen access token is fresh, and a reauthentication
step would need protocol support in every client; the current password is what proves the
person. A wrong current password fails the statement and is audited. The change ends the user's
sessions, the one that ran it included (its access token names the old password), as every
password change does, and the client signs in again; the web UI does that by itself.

**Sign-ins before a session.** The token endpoint and `--verify-credentials` treat a temporary
password as valid: it is the user's credential, and GizmoData Cloud's auto-resume should wake a
stopped cluster for it so the user can change it there. The verifier's answer says
`"password_must_change": true`; the session the cluster then opens is held as above.

**Audit.** `password`: `temporary password set`, `password changed by the user` (with
`authorized_by`: `the user's own current password`), `password set`, `password expired`,
`password removed`; the account's `before` / `after` state ends in `; password must be
changed` while it must. `hardcharger_users()` has `password_must_change` for administrators and
the user's own row (NULL in other rows).

**Mixed versions.** A 2.13 replica does not hold such sessions. Use temporary passwords once
every replica runs 2.14, as for `SYSTEM ADMIN`.

## Not built

- Export of the audit chain to an independent append-only destination.
