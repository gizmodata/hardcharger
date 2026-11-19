// On an admin page opened by a user (not the administrator) while one of their editor tabs
// acts as System Admin: says so, and how the page got there.

/** Shown on an admin page whose own session acts as System Admin. */
export function SystemAdminNotice() {
  return (
    <p className="results-notice" role="status" data-testid="system-admin-notice">
      Acting as System Admin: an editor tab said SET ROLE SYSTEM ADMIN, and this page&apos;s own session does too. It
      closes when no tab acts as System Admin (RESET ROLE, or SYSTEM ADMIN revoked).
    </p>
  );
}

/** The second line of an admin page's "only administrators" message. */
export function SystemAdminHint() {
  return (
    <p className="page-sub" data-testid="system-admin-hint">
      A user granted SYSTEM ADMIN opens it by running SET ROLE SYSTEM ADMIN in an editor tab.
    </p>
  );
}
