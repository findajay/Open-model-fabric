# ADR-0010: Secrets by reference; egress (SSRF) policy

**Status:** Accepted

## Context
Profiles and runtime provider definitions must be shareable and auditable without
leaking credentials. Admins can also configure arbitrary endpoint URLs. Unchecked, that
lets a request reach cloud metadata services or internal networks (SSRF).

## Decision
- **Secrets by reference.** Credentials appear only as `*_ref` fields
  (`env:NAME` or `file:/run/secrets/name`).
  - The loader rejects credential-looking keys that hold literal values.
  - `file:` refs are confined to `security.secret_dirs`.
  - Secrets are resolved on the backend at adapter construction and wrapped in
    `SecretValue`, whose `repr` is redacted.
  - APIs return references and their availability, never values.
  - A missing secret degrades the provider instead of blocking startup.
- **Egress policy** (`omf.core.security.egress.EgressPolicy`). It is checked when a
  configuration is saved, including DNS resolution, and again before connecting (cached
  for 60 s). Rules:
  - only http(s);
  - no credentials in URLs;
  - cloud metadata hosts and IPs are always blocked (169.254.169.254, fd00:ec2::254,
    metadata.google.internal, …), and so are link-local, multicast, reserved and
    unspecified addresses;
  - loopback and private networks require explicit opt-in;
  - optional `allowed_hosts` allowlist.
- HTTP clients never follow redirects to inference endpoints.

## Consequences
- Development profiles opt into loopback and private networks for local Ollama. The
  production example allows only private networks plus an allowlist.
- Known gap: DNS rebinding between the check and the connection. Mitigations: pin IP
  literals, use an egress proxy, or use network policies.
- The Google Drive and S3 endpoint URLs are only checked syntactically. They are not
  re-resolved at connect time.

## Alternatives considered
- Plain-text secrets in a database: rejected.
- Resolving secrets per request: more lookups with no security gain for env/file
  sources.
