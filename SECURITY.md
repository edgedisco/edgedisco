# Security and privacy model

## Trust boundaries

- The endpoint is untrusted input. The server authenticates the device and validates the report shape.
- Device credentials are unique after enrollment and stored only as SHA-256 hashes on the server.
- Administrator and enrollment credentials are separate from device credentials.
- Transport security is delegated to an enterprise HTTPS ingress.

## Data minimization

Raw command lines and file paths never leave the endpoint. Sensitive command argument values are redacted before the remaining command structure is hashed. MCP environment variables, arguments, headers, and URLs are not collected. Runtime adapters use an explicit allowlist and discard prompts, responses, source code, tool input and output, transcripts, credentials, email addresses, and raw workspace paths before spooling. The server independently rejects fields outside the normalized event schema. Content inspection is outside this utility's scope.

## Known gaps before production

- Add OIDC/SAML authentication and scoped roles instead of the MVP administrator token.
- Store data in managed PostgreSQL with encryption, backups, retention, and tenant isolation.
- Add device credential revocation, enrollment-token expiry, and certificate-based device identity.
- Rate-limit all endpoints at ingress and cap body sizes there as well as in the application.
- Code-sign/notarize endpoint packages and publish checksums through endpoint management.
- Add append-only audit events for administrator views, exports, and configuration changes.
- Perform legal review for employee monitoring rules in every deployment jurisdiction.

Report vulnerabilities privately to the project owner. Do not include secrets or production data in reports.
