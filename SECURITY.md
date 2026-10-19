# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report privately through
GitHub's *Report a vulnerability* (Security → Advisories) on this repository. Include
affected versions, a description, reproduction steps and impact. We aim to acknowledge
reports within 5 working days and to agree a disclosure timeline with you.

## Supported versions

Only the latest release receives security fixes while the project is pre-1.0.

## Scope

In scope: the API (`apps/api`), core and adapters (`python/omf`), the web app
(`apps/web`), the SDK, the container image and the deployment manifests. Out of scope:
vulnerabilities in inference engines, models or cloud services that Open Model Fabric
connects to (report those upstream), and deployments that ignore the
[production checklist](docs/deployment/README.md#production-checklist), such as running
`auth.mode: none` behind a public proxy.

## Security design

The threat model, the implemented controls (secret references, API-key roles, SSRF egress
policy, request limits, audit log, no prompt text in telemetry by default) and the known
limitations are documented in
[docs/security/threat-model.md](docs/security/threat-model.md). Dependencies are
scanned with `pip-audit` and `pnpm audit` (`make audit`, and the nightly `security`
workflow).
