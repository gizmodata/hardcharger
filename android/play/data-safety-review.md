# Data safety review draft

Owner review is required before entering declarations in Play Console. This inventory
describes the current source; it is not a legal determination or a blanket “no collection”
answer. Recheck whenever dependencies, telemetry, authentication or hosting change.

| Flow | Data and destination | Current behavior |
| --- | --- | --- |
| On-device database | Imported files, SQL, results, logs and settings in app storage | No automatic GizmoData upload; query logs may contain statement values. |
| Saved connections | Server addresses and credentials in app storage | Passwords use Android Keystore encryption; database/log files are not separately encrypted. |
| Remote server | Credentials/tokens, queries, uploaded files and results | User-selected endpoint; remote operator controls retention. Evaluate the exact transfer against Play's collection/sharing definitions and exceptions. |
| Browser sign-in | Authentication data to the selected server's identity provider | Optional; browser and identity-provider policies apply. Local features do not require an account. |
| SQL network access | Data/credentials supplied to bundled HTTP, DuckLake or PostgreSQL features | User-directed network operations; may use services outside GizmoData. |
| File picker/share | Selected documents and exported files to the chosen provider/app | Explicit user action; external copies outlive app removal. |
| Local sharing | Service discovery and database access to authenticated LAN clients | Off by default; TLS required; user can turn it off. |
| Website link | Browser connection to GizmoData website | User opens link; website processing needs separate owner review. |
| Maps | Bundled Natural Earth assets | No third-party map tile service identified. |
| Ads/analytics/crash uploads | None identified in the app's current dependencies or application code | No built-in GizmoData advertising, analytics or automatic crash-upload service. |

App backup/device transfer is disabled. Clear storage/uninstall removes private app data;
it does not remove exported copies or data on remote servers. No GizmoData account is
created in-app. Do not confuse optional third-party sign-in with account creation by this
application; answer any account-deletion declarations using the current Console wording.

Check the merged release manifest/dependency report, approved privacy policy, actual
release behavior and Google's current form before submission. Confirm that the policy's
entity name and support email are correct and that its hosted URL is accessible publicly.
