# Artifact storage adapters

All storage adapters implement `ArtifactStorageProvider` and pass the same contract suite
(`tests/contract/test_storage_contract.py`). The suite covers round trips, SHA-256
checksums, versioning, prefix listing, delete semantics, corruption detection, unsafe
keys, files larger than 2 MB, empty files and status.

| | `local_fs` | `google_drive` | `s3` |
|---|---|---|---|
| Intended use | Development and single-host artifacts, experiment outputs, benchmark exports | Model artifacts and experiment outputs shared with Colab notebooks. **Not a serving store** ([ADR-0008](../architecture/adr/0008-google-drive-not-a-serving-store.md)) | Production artifacts, serving cache, experiment outputs |
| Dependency | none | `pip install 'open-model-fabric[gdrive]'` (google-auth) | `pip install 'open-model-fabric[s3]'` (boto3) |
| Key → location | `objects/<h[:2]>/<h>/` with `h = sha256(key)`; key kept in `meta.json` | One Drive file per key in a single folder; `name` = key; lookup by `appProperties.omf_kh` | `<prefix><key>` |
| Versions | `v000001.bin`, …; keeps the last `keep_versions` (default 3) | New Drive revision of the same file; `omf_version` incremented | `omf-version` metadata incremented; enable bucket versioning to retain old content |
| Checksums | SHA-256 in `meta.json`; verified on every `get` | `appProperties.omf_sha256`; download hashed and verified | `x-amz-meta-omf-sha256`; download hashed and verified |
| Large files | Streamed, staged in `root/tmp`, fsync + atomic `os.replace` | Spooled to a temp file, then a **resumable upload** in chunks (`chunk_size_mb`, rounded to 256 KiB), resuming after 5xx/429 with a status query | boto3 managed (multipart) transfer |
| Delete | Removes all versions | Moves the file to the **Drive trash** (recoverable) | `DeleteObject` |
| Auth | Filesystem permissions | Service-account JSON from `credentials_ref` (secret reference). Default scope `drive.file`. Share the folder with the service account. | Default AWS credential chain, or `access_key_id_ref` / `secret_access_key_ref` |
| Status | Writable root + free space | Folder accessible and `canAddChildren` | `HeadBucket` |
| Tested against | Real filesystem | In-memory fake of the Drive v3 endpoints (`tests/contract/fake_drive.py`), including an injected chunk failure | moto's in-process S3 emulation |

**Not tested in this repository:** real Google Drive, real AWS S3, MinIO. The adapters
follow the public APIs, but run a manual smoke test against your own account before
relying on them.

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
```

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
- **`local_fs`**
  - Per-key locking is in-process only. Do not point several API processes at the
    same root.
- **All adapters**
  - If a storage provider cannot be initialised (for example, missing credentials), it
    is registered as unavailable: status `down`, and operations raise `storage_error`.
    The rest of the platform keeps running.
