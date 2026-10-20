# Third-party software in the Hardcharger app

The Hardcharger app for iPhone and iPad contains the Hardcharger server (`hardcharger-server`),
the command-line client's library (its console) and the web UI, with the third-party
software listed after this part, and also:

## Mozilla's CA certificate list

- File: `cacert.pem` in the app, the CA certificates extracted from Mozilla's root store by
  the curl project (<https://curl.se/docs/caextract.html>), release of 2026-09-25
  (`ios/scripts/update-ca-bundle.sh` pins its date and SHA-256)
- Licence: MPL-2.0 (Mozilla Public License, version 2.0, <https://mozilla.org/MPL/2.0/>)
- Use: the certificate authorities the server's and the console's own TLS connections
  (OpenSSL) trust; iOS has no such file. The app's web views use iOS's own trust store.

The file is distributed unmodified and is its own source form; it is also available from
<https://curl.se/ca/cacert-2026-09-25.pem>.

---

