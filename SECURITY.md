# Security

Report vulnerabilities privately through the repository's GitHub security advisory feature when enabled. Do not include tokens, cookies, profile archives or private user data in public issues.

## Security model

- Local stdio transport only; no public HTTP listener, session manager port, or remote browser debugging endpoint.
- Official requests target only `https://open.douyin.com` with fixed paths. Redirects are rejected and environment proxy settings are ignored. Tokens remain request headers or local OAuth bodies.
- The optional browser only navigates generated Douyin search URLs and validated canonical video URLs. HTTP requests use an HTTPS hostname allowlist and reject hosts resolving to nonpublic IPs. WebSockets are blocked. Browser service workers and downloads are disabled during reads. DNS is checked before requests but cannot completely prevent DNS rebinding between validation and the browser's own resolution; use a trusted network and outbound firewall for a hostile-network deployment.
- Browser pages/comments are untrusted content. Login and CAPTCHA are completed manually. No private API signing, hidden-state extraction, or access-control circumvention is included.
- Writes require server configuration and explicit per-call execution. A persistent SQLite idempotency ledger blocks duplicate and ambiguous retries; it is not a platform transaction mechanism. Keep the ledger private and persistent. Ledger results can contain private metadata/post text.
- Media uploads accept only approved local directory contents, supported extensions and bounded sizes. No URL-to-upload fetcher exists. Directory contents must remain trusted while a write runs.
- Tokens and session cookies stay in the local state directory or process environment. Secrets and token patterns are removed from MCP output; arbitrary upstream error strings and HTTP exception bodies are not returned. Keep debug logging disabled at the HTTP library level.
- Credential JSON files are plaintext, mode 0600 on Unix. On Windows, choose a private user directory with restrictive ACLs; chmod does not implement Windows ACL isolation. The optional browser profile contains authenticated session state and needs the same protection.
- Separate apps/accounts need separate state directories. Cached credentials with conflicting stored app/account identity are rejected.

## Release limits

0.1.0 has mock-transport and local protocol/security tests. It has not undergone an independent full audit, authenticated Douyin API validation, or a live-browser compatibility test. Douyin capabilities can be restricted, deprecated or denied by the platform. Upstream URLs in result metadata are displayed as data and never automatically fetched by the server.

The operator controls authorization grants, browser login, media selection and write enablement. Do not enable unrestricted writes for an untrusted MCP client. Delete operations permanently remove content.
