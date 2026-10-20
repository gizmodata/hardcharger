# Security policy

## Reporting a vulnerability

Please don't report security problems in public issues or pull requests.

- Preferred: [report a vulnerability privately on GitHub](../../security/advisories/new)
  (Security tab, "Report a vulnerability"). Only the maintainers see it.
- Or email **security@gizmodata.com**. Include the affected version, how to reproduce, and the
  impact you see. Ask for a key in a first message if you want to encrypt the details.

We acknowledge reports within 3 business days, keep you informed while we work on a fix, and
credit you in the release notes if you wish. Please give us a reasonable time to ship a fix
before any public disclosure.

## Supported versions

Security fixes go into the latest minor release of Hardcharger. Older releases don't receive fixes;
upgrade to the latest release.

## Scope

In scope: this repository's code and its published artifacts (packages, images, binaries).
Out of scope: GizmoData's hosted services and websites (report those to security@gizmodata.com
as well; they are handled separately), and vulnerabilities in third-party dependencies that
don't affect this project's use of them (report those upstream).
