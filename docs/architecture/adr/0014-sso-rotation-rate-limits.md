# ADR-0014: SSO by OIDC token validation, rotatable keys, in-process rate limits, shared cancellation

**Status:** Accepted

## Context
Static API keys, no limits and per-process cancellation were acceptable for one team on
one replica. They are not acceptable for several teams or several replicas.

## Decision
- **OIDC as a resource server.** OMF validates bearer JWTs from a configured issuer: the
  JWKS signature, `iss`, `aud` and `exp`, and an algorithm allowlist (no `none`, no
  HMAC). OMF runs no login flow and stores no sessions. Clients and proxies obtain tokens
  from the provider, which keeps OMF stateless and works with any compliant provider.
  Roles map from a configurable claim path. Names are prefixed `sso:` so they cannot
  collide with API keys.
- **API keys stay** for services:
  - `expires_at` gives a rotation deadline;
  - several entries may share a name, for overlap during rotation;
  - secret files are re-read every `token_reload_s`, so rotation needs no restart.
- **Rate limits in process.** Token buckets per principal, one for all calls and one
  for inference. They are simple, dependency-free and effective on one replica. Across
  replicas they are approximate, so a gateway should add global limits. A shared store
  such as Redis was rejected for now: it would be a new required dependency.
- **Shared cancellation through the metadata database** (`server.cancellation:
  shared`). Replicas register in-flight requests. A cancel that misses the local process
  is recorded after an ownership check. Each replica polls for cancels of its own
  requests every 0.5 s. This reuses PostgreSQL, which multi-replica deployments already
  need, and adds no new dependency. The default stays `local` (no extra writes).

## Consequences
- **Revocation:** revoked OIDC tokens stay valid until they expire. Keep access-token
  lifetimes short.
- **Rate limits** are approximate across replicas.
- **Shared mode cost:** two small writes per request, plus one indexed query per replica
  every 0.5 s while it has requests in flight. Rows left by a crashed replica are pruned
  after `max(1 h, 2 × request_timeout_s)`.

## Alternatives considered
- **Session-based SSO** (OMF as an OIDC client with cookies): more UI convenience, but it
  needs CSRF protection and session storage, and it duplicates what oauth2-proxy-style
  gateways already do.
- **Redis for limits and cancellation:** precise and fast, but it would be a new
  infrastructure dependency for every deployment.
