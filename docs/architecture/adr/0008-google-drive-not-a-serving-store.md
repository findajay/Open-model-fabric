# ADR-0008: Google Drive stores artifacts, not a serving store

**Status:** Accepted

## Context
Colab users already keep files in Google Drive, so Drive is a natural place to share
artifacts and experiment results between notebooks and the platform. Drive is not
built for high-throughput model loading: it has API rate limits, per-user quotas, no
range-optimised serving path, and it is a consumer-grade API.

## Decision
- The `google_drive` adapter speaks the Drive v3 REST API through httpx. `google-auth`
  is used only to mint service-account tokens (optional extra `gdrive`).
- All files live in one configured folder. They are tagged with `appProperties`:
  - `omf_ns`: namespace;
  - `omf_kh`: SHA-256 of the key, used for exact lookup, because appProperty values are
    limited to about 124 bytes;
  - `omf_sha256`;
  - `omf_version`;
  - `meta_*`: user metadata.

  The file name is the full key.
- Uploads use the resumable protocol in chunks (a multiple of 256 KiB), with status
  queries to resume after 5xx errors. Re-putting a key creates a new Drive revision and
  increments `omf_version`. Downloads are hashed and verified. `delete` moves the file to
  the trash (recoverable).
- The default scope is `drive.file` (least privilege).
- `StorageStatus.intended_use` and the docs state that Drive holds artifacts, experiment
  outputs and benchmark exports, not production serving.

## Consequences
- Production profiles use `s3` (or local disk) for weights.
- The flat folder layout keeps lookups to a single query, but very large namespaces
  need paginated listing, which the adapter does.
- The adapter was tested against an in-memory fake of the Drive endpoints
  (`tests/contract/fake_drive.py`), including resumable-upload recovery. It has **not**
  been exercised against real Google Drive in this repository's CI.

## Alternatives considered
- `google-api-python-client`: a heavy dependency, and its blocking I/O would need
  wrapping.
- Mirroring keys as nested Drive folders: more API calls per operation.
