# Artifact storage adapters

All storage adapters implement `ArtifactStorageProvider` and pass the same contract suite
(`tests/contract/test_storage_contract.py`). The suite covers round trips, SHA-256
checksums, versioning, prefix listing, delete semantics, corruption detection, unsafe
keys, files larger than 2 MB, empty files and status.

| | `local_fs` | `google_drive` | `s3` | `azure_blob` |
|---|---|---|---|---|
| Intended use | Development and single-host artifacts, experiment outputs, benchmark exports | Model artifacts and experiment outputs shared with Colab notebooks. **Not a serving store** ([ADR-0008](../architecture/adr/0008-google-drive-not-a-serving-store.md)) | Production artifacts, serving cache, experiment outputs | Production artifacts, serving cache, experiment outputs |
| Dependency | none | `pip install 'open-model-fabric[gdrive]'` (google-auth) | `pip install 'open-model-fabric[s3]'` (boto3) | none (REST over httpx) |
| Key → location | `objects/<h[:2]>/<h>/` with `h = sha256(key)`; key kept in `meta.json` | One Drive file per key in a single folder; `name` = key; lookup by `appProperties.omf_kh` | `<prefix><key>` | Block blob `<container>/<prefix><key>` (names URL-encoded) |
| Versions | `v000001.bin`, …; keeps the last `keep_versions` (default 3) | New Drive revision of the same file; `omf_version` incremented | `omf-version` metadata incremented; enable bucket versioning to retain old content | `omf_version` metadata incremented; enable blob versioning to retain old content |
| Checksums | SHA-256 in `meta.json`; verified on every `get` | `appProperties.omf_sha256`; download hashed and verified | `x-amz-meta-omf-sha256`; download hashed and verified | `x-ms-meta-omf_sha256`; download hashed and verified. User metadata is one base64 JSON value (`omf_user`), because Azure metadata names cannot contain `-` |
| Large files | Streamed, staged in `root/tmp`, fsync + atomic `os.replace` | Spooled to a temp file, then a **resumable upload** in chunks (`chunk_size_mb`, rounded to 256 KiB), resuming after 5xx/429 with a status query | boto3 managed (multipart) transfer | Spooled to a temp file; blobs larger than `block_size_mb` (default 8) are uploaded as blocks plus a block list |
| Delete | Removes all versions | Moves the file to the **Drive trash** (recoverable) | `DeleteObject` | `Delete Blob` with snapshots |
| Auth | Filesystem permissions | Service-account JSON from `credentials_ref` (secret reference). Default scope `drive.file`. Share the folder with the service account. | Default AWS credential chain, or `access_key_id_ref` / `secret_access_key_ref` | Exactly one of: account key (`account_key_ref`, Shared Key signing), SAS token (`sas_token_ref`), or a service principal (`tenant_id` + `client_id` + `client_secret_ref`, needs *Storage Blob Data Contributor*) |
| Status | Writable root + free space | Folder accessible and `canAddChildren` | `HeadBucket` | `Get Container Properties` |
| Tested against | Real filesystem | In-memory fake of the Drive v3 endpoints (`tests/contract/fake_drive.py`), including an injected chunk failure | moto's in-process S3 emulation, and **MinIO** (`bitnamilegacy/minio:2025.7.23-debian-12-r5`) in CI | **Azurite 3.37.0** (Microsoft's emulator, which verifies Shared Key and SAS signatures), with the full contract suite, an account SAS and the HTTP API; plus an in-memory fake (`tests/contract/fake_azure_blob.py`) |

**Real servers in CI:** the `Storage backends` workflow
(`.github/workflows/storage-backends.yml`) runs the contract suite against Azurite (for
`azure_blob`) and MinIO (for `s3`). It runs weekly, on demand, and when storage code
changes.

**Not tested in this repository:** real Google Drive, real AWS S3 and real Azure Storage
accounts. Azurite and MinIO implement the same APIs, but run a smoke test against your
own account before relying on an adapter.

## Configuration examples

```yaml
storage:
  - id: local
    adapter: local_fs
    default: true
    config: {root: ./var/artifacts, keep_versions: 3}

  - id: gdrive
    adapter: google_drive
    config:
      folder_id: "${GDRIVE_FOLDER_ID}"
      credentials_ref: env:GDRIVE_SERVICE_ACCOUNT_JSON   # the JSON key's content
      namespace: omf              # separates OMF files from other files in the folder
      chunk_size_mb: 8

  - id: artifacts
    adapter: s3
    config:
      bucket: my-omf-bucket
      prefix: omf/
      region: us-east-1
      # endpoint_url: https://minio.internal.example.com
      # force_path_style: true

  - id: azure
    adapter: azure_blob
    config:
      account_name: omfartifacts
      container: models
      prefix: omf/
      account_key_ref: env:AZURE_STORAGE_KEY   # or sas_token_ref, or a service principal
      # endpoint_url: http://127.0.0.1:10000/devstoreaccount1   # Azurite
```

`configs/storage-backends.example.yaml` has a complete profile with every storage
adapter, including MinIO and the three Azure authentication methods.

## Limitations

- **`google_drive`**
  - All files live in one folder and are found by appProperty queries. Listing scans
    the namespace, with pagination.
  - Drive API quotas apply.
  - User metadata entries must fit Drive's 124-byte appProperty limit.
- **`s3`**
  - The version counter is read and then written, so concurrent writers to the same
    key from different processes can produce duplicate version numbers. Content
    integrity is unaffected.
  - `list` issues a `HeadObject` per key to read OMF metadata.
- **`azure_blob`**
  - Same read-then-write version counter as `s3`.
  - User metadata is limited by Azure's 8 KB metadata total.
  - The adapter does not create containers. Create the container first.
  - Shared Key signing follows the format implemented by azure-sdk-for-python
    (`SharedKeyCredentialPolicy`). The header collation tables are ported from it (MIT).
    Microsoft's documentation site was not reachable from the development sandbox.
- **`local_fs`**
  - Per-key locking is in-process only. Do not point several API processes at the
    same root.
- **All adapters**
  - If a storage provider cannot be initialised (for example, missing credentials), it
    is registered as unavailable: status `down`, and operations raise `storage_error`.
    The rest of the platform keeps running.
