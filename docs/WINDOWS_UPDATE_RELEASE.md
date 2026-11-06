# Windows update release retention and verification

Use this procedure for a reviewed Windows Velopack release. The updater remains in development: the repository pin is unverified, installed trust keys are empty, and automatic updates are disabled. Candidate workflow success does not satisfy release acceptance.

`python -m scripts.windows_update_release` prepares and verifies release material. It does not upload files, change a GitHub release, advertise an update, modify installed trust, or configure a storage service. Public distribution requires separate authorization. Run these commands in the reviewed release virtual environment.

## Establish signing and acceptance

Keep metadata-signing private keys outside the repository, build payload, release directory and retained source. Maintain an independently reviewed JSON map of key IDs to base64-encoded raw Ed25519 **public** keys. Use the same exact public-key file bytes when building the repair companion and certified application configuration; native evidence records its SHA-256. Review key IDs and fingerprints through a trusted channel before creating the first installer. The feed cannot introduce its own trust anchor.

Windows executable signing and Ed25519 metadata signing are separate decisions. Retain the Windows signature verification report for the exact installer, updater and repair hashes, including the intended publisher and certificate/timestamp results. Resolve any unsigned-distribution decision explicitly before publication. The release-set verifier checks signed records, artifact hashes and evidence bindings; Windows Authenticode validation and actual application/hardware acceptance require their own reviewed evidence.

The client accepts up to sixteen signatures over the same canonical release payload. `scripts.package_windows_updates` supports a primary signer and explicit additional signers:

```powershell
python -m scripts.package_windows_updates <existing release arguments> --trusted-keys <reviewed-public-keys.json> --additional-signing-key next-key=<next-private-key.pem>
```

Every co-signer, including the primary signer, must match that explicit public map. Additional signatures do not change the signed payload, asset identities or sequence. Private keys are read only for signing and are not copied into output.

For rotation, first certify an application and companion that contain the reviewed overlap of old/new public keys. Co-sign metadata during the transition so clients with either trust map can authenticate it. The current client validates every record in an index: keep the keys needed by older records in the overlap map, or remove unsupported records from the active index while retaining their offline release sets. Test the complete old-client-to-bridge-to-new-client path before cutting over. Do not retire a signer while clients that depend on it must still update through the shared feed. A compromised key or unsupported old client may require a separately verified manual installer; metadata expiration or changing a GitHub flag does not replace installed trust.

Native evidence and package evidence follow [the candidate packaging contract](../installer/velopack/README.md#prepare-a-candidate-bundle). A final acceptance record must describe the actual version/source and exact `installer`, `repair`, `updater` and `full_package` SHA-256 identities. Its `schema_version` is 1, and `release_certified` can be true only after review of the applicable gates.

The `checks` map requires each of these names to be true, with retained raw evidence:

- `fresh_install`, `legacy_cpu_migration`, `two_successive_releases`, `skipped_release`, `delta_full_fallback`.
- `interrupted_replacement`, `offline_recovery`, `state_preservation`, `uninstall_repair`, `concurrent_process_refusal`.
- `hardware_shutdown`, `optional_package_transition`, `code_signing_review`, `trust_bootstrap_and_rotation`.
- `source_regressions`, `repository_privacy`, `dependency_sources`, `frozen_runtime`, `interface_review`.
- For a release advertising NVIDIA support: `legacy_nvidia_migration` and `matching_worker_inference`.

Each name has an `evidence` entry containing the raw report's `sha256`, and a corresponding `report_<name>` artifact input. The record also includes `version`, `source_commit`, `artifact_sha256` with the four identities above, and `code_signing` with a reviewed `mode` (`authenticode` or `unsigned-approved`) and `decision_reference`. Writing assertions does not establish that the tests occurred. The candidate contract's `release_certified: false`, same-source replacements, simulated damage and mocked GPU checks cannot substitute for these gates.

## Prepare an immutable release set

Create an artifact-input JSON object mapping these roles to absolute file paths:

| Role | Exact input |
| --- | --- |
| `installer` | The accepted Velopack setup executable. |
| `repair`, `updater`, `patch` | Certified companion, production updater and exact native patch. |
| `native_certification`, `certification` | Reviewed native evidence and package-bound evidence. |
| `acceptance` | Reviewed full-app acceptance record described above. |
| `provenance`, `verification_contract` | Matching build provenance and frozen verification contract. |
| `dependency_sources` | Exact CPU dependency index bundled under `_internal/third-party-sources`. |
| `report_<check>` | Raw evidence matching each required acceptance digest. Additional report roles may retain supporting evidence. |

Prepare a local cache of the reviewed upstream dependency source archives using [the source-retention tools](PACKAGING_AND_DISTRIBUTION.md#matching-source). Include both CPU and NVIDIA source indexes/archives when applicable. The release-set command checks those bytes against the corresponding lockfiles at the signed TEMPO commit; a URL-only source index is insufficient.

Keep the exact full package and deltas from `scripts.package_windows_updates` in `<package-directory>`. For NVIDIA support, include the original `nvidia-runtime.json` and every signed runtime part there. For each advertised delta, `<base-directory>` must contain its exact previous public full package and original signed `tempo-update-v<base-version>.json`. Rebuilt old packages are rejected. The previous index authenticates base identities; historical base metadata may be expired for this offline retention purpose.

Use a checkout containing the matching TEMPO commit and an upstream Velopack checkout containing the pinned commit. The tool uses `git archive` at those exact commits, excluding untracked local files. It retains the native patch and pin alongside the upstream source so the native implementation can be reconstructed.

```powershell
python -m scripts.windows_update_release prepare --manifest <signed-release.json> --index <tempo-update-index.json> --package-dir <package-directory> --artifacts <artifact-inputs.json> --public-keys <reviewed-public-keys.json> --signing-key <private-key.pem> --key-id <reviewed-key-id> --dependency-cache <verified-source-cache> --native-repository <velopack-checkout> --output <new-release-set-directory>
```

Add `--channel prerelease` for the prerelease feed. Supply `--previous-index <previous-index.json>` for follow-on releases, including full-only releases; add `--base-package-dir <base-directory>` when advertising deltas. The tool rejects a reused/lower version or sequence relative to that authenticated index. Choose a sequence greater than every previously published sequence for the channel and retain that publication record outside build-job storage.

Preparation verifies trust/configuration, the embedded repair/provenance/source index, native certification, full-package inventory, reviewed acceptance bindings, deltas and their exact full bases, and all advertised NVIDIA parts. Every public asset must fit GitHub's individual release-asset limit. The original previous manifests and NVIDIA descriptor bytes are preserved so formatting changes cannot create a different public asset. The tool does not rerun Windows native reconstruction; that remains a prerequisite of signed package production and its acceptance evidence.

The result contains:

- `public/`: exact artifacts intended for the versioned release, including matching TEMPO/native/dependency sources, patch, pin, repair, packages and signed release metadata.
- `private/`: certification, provenance, trust snapshot and raw acceptance reports, retained for review.
- `feed/tempo-update-index.json`: the separately controlled index to advertise last.
- `tempo-release-set.json`: the signed inventory of the set, binding its version, commit, channel and sequence.

The output directory must be absent. A failed preparation has no completed release set. Keep the printed catalog SHA-256 separately through the release approval/retention process. Rebuilding a set after upload is not a substitute for recovering the accepted bytes.

## Verify retained material from durable storage

Copy the complete set to approved storage whose retention does not depend on an Actions job or cache expiration. Retain previous release sets, their signed inventories, full update bases, native implementation, sources and matching evidence while they support installed users or recovery. Do not automatically prune them when an active feed drops an older version. Define ownership, access controls, retention/lifecycle policy and a second recovery location before rollout; the command does not infer those properties from a local folder or storage URL.

Recover a separate copy from that storage and verify it using the independently retained digest and public map:

```powershell
python -m scripts.windows_update_release verify --root <recovered-set> --public-keys <independently-trusted-public-keys.json> --catalog-sha256 <approved-catalog-sha256>
```

Verification rejects altered signatures, changed/missing files, links and extra files. It rechecks native/package identities and acceptance bindings. Historical signed metadata remains usable for this archive check after feed expiry. This does not make it eligible to advertise a fresh update. Keep historical public keys available for offline verification without silently changing the installed application's active trust map.

Actions artifacts remain temporary transport/evidence. A verified local copy alone does not establish durable retention; record the successful recovered-copy check together with the approved storage policy.

## Check served assets before and after feed activation

After publication is separately authorized, stage the **exact** `public/` files and root catalog on a public versioned prerelease. Keep `private/` in the approved evidence store. Leave the new feed unadvertised while checking the served bytes:

```powershell
python -m scripts.windows_update_release check-publication --root <recovered-set> --public-keys <independently-trusted-public-keys.json> --catalog-sha256 <approved-catalog-sha256>
```

This is a read-only check. It requires the versioned public prerelease, verifies the target tag's full source commit, and streams every declared public asset and the catalog to compare sizes and SHA-256 values. Source indexes, repair material, previous bases and NVIDIA parts receive the same check. A missing or changed asset fails before any feed readback.

After all applicable acceptance and durable-retention reviews pass and activation is authorized, publish the exact retained index last. For stable delivery, upload the index to the versioned prerelease after its other assets have passed, then promote the approved release to stable/latest as the final eligibility change. Prerelease delivery uses the separately reserved `updates-prerelease` feed location. Preserve the signed channel in both cases.

Repeat the read-only check with `--include-feed`. It checks actual served assets first and the configured client feed URL last. Stable verification also requires the exact target to be stable/latest; prerelease verification requires the matching prerelease flag. Expired release metadata cannot pass publication checks. An index mismatch, stale source tag, unsafe redirect or corrupted asset blocks verification. These commands perform no public writes and never enable updates.
