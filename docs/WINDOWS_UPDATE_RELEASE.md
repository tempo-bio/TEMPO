# Release Windows updates

Use this procedure to turn a reviewed Windows build into an updater-enabled Velopack release. Start with the completed build run, access to the release signing environment, and access to the team's release evidence store. Use the project's release virtual environment for the command-line steps. [Build a Windows candidate](PACKAGING_AND_DISTRIBUTION.md)

## Assemble and smoke-test the stable-channel installer

1. Run **Build & Package Windows Release** on the intended release branch with `release_tag` set to the application version, including its leading `v`. Record the successful build run ID and full source commit.
2. Dispatch the same workflow on that branch with the same `release_tag`, the completed run ID in `assemble_build_run`, and its full SHA in `assemble_source_commit`. Leave `smoke_source_run` empty for a new assembly. The workflow uses the earlier run's build and native evidence, writes the reviewed pin and certification report, and smoke-tests the exact installer on a clean Windows runner.
3. Retrieve `TEMPO_<tag>_stable_channel` and `TEMPO_<tag>_release_smoke`. Review the smoke results, including the startup test with files left by an old uninstall. For v1.0.6, also review `TEMPO_v1.0.6_delta_upgrade`, which tests replacement from the published v1.0.4 base. Keep the exact installer, full package, repair companion, matching public keys, reviewed pin, and certification report for signing and retention.
4. Sign metadata for those exact packages. After approval, stage the binaries on the versioned prerelease and verify the served assets before advertising the update. [Sign the update metadata](#sign-the-update-metadata)

## Sign the update metadata

Use the reviewed public-key file from assembly. The map contains key IDs and base64-encoded raw Ed25519 public keys. The companion and application configuration must use the same file bytes recorded in the native verification report. Keep private keys in the signing environment, outside the repository and build artifacts. [Rotate an installed signing key](#rotate-an-installed-signing-key)

In the signing environment, run `python -m scripts.package_windows_updates --help` from the matching source checkout. Supply the retained full package, its exact version and commit, the reviewed native pin and certification, and the intended channel. Use the signed previous index for a follow-on release. Choose a sequence above the last published sequence for the channel and an expiry that covers the trial and publication window. Use the release's storage schema and minimum updater version.

To advertise a delta, supply the patch specification, the exact base package, and the Windows delta verification report. For NVIDIA support, pass the descriptor from the same build as the runtime parts. Keep the resulting signed release record, index, package, and any deltas together for release-set preparation.

For the v1.0.6 upgrade trial, start from the published v1.0.4 Velopack installation. Exercise the signed update path with the exact candidate packages and verify that the desktop starts and still sees the saved workspace, drafts, recordings, and analysis outputs.

Use the signed index from the versioned v1.0.5 trial as the previous index for v1.0.6 stable-channel signing. It includes both v1.0.4 and v1.0.5, so the installed v1.0.5 trial can use its own recovery base when downloading the v1.0.6 full package.

## Review release acceptance

Windows executable signing and Ed25519 metadata signing are separate decisions. Retain the Windows signature verification report for the exact installer, updater and repair hashes, including the intended publisher and certificate/timestamp results. Resolve any unsigned-distribution decision explicitly before publication. The release-set verifier checks signed records, artifact hashes and evidence bindings; Windows Authenticode validation and actual application/hardware acceptance require their own reviewed evidence.

Use the candidate packaging contract in `installer/velopack/README.md` from the matching source checkout for native and package evidence. A final acceptance record must describe the actual version/source and exact `installer`, `repair`, `updater` and `full_package` SHA-256 identities. Its `schema_version` is 1, and `release_certified` can be true only after review of the applicable gates.

The `checks` map requires each of these names to be true, with retained raw evidence:

- `fresh_install`, `two_successive_releases`, `skipped_release`, `delta_full_fallback`.
- `interrupted_replacement`, `offline_recovery`, `state_preservation`, `uninstall_repair`, `concurrent_process_refusal`.
- `hardware_shutdown`, `optional_package_transition`, `code_signing_review`, `trust_bootstrap_and_rotation`.
- `source_regressions`, `repository_privacy`, `dependency_sources`, `frozen_runtime`, `interface_review`.
- For a release advertising NVIDIA support, include `matching_worker_inference`.

Each name has an `evidence` entry containing the raw report's `sha256`, and a corresponding `report_<name>` artifact input. The record also includes `version`, `source_commit`, `artifact_sha256` with the four identities above, and `code_signing` with a reviewed `mode` (`authenticode` or `unsigned-approved`) and `decision_reference`. Writing assertions does not establish that the tests occurred. The candidate contract's `release_certified: false`, same-source replacements, simulated damage and mocked GPU checks cannot substitute for these gates.

For retained releases through v1.0.4, verification still checks the original `legacy_cpu_migration` evidence and, for releases with NVIDIA support, `legacy_nvidia_migration`. For the historical stable-channel v1.0.2 build at `4dc336fc8b655ed347a6a1e37a0ae944bf3502e3`, the tool also expects the recorded deferral `"deferred_checks": {"hardware_shutdown": "v1.0.3"}` and `software_shutdown` evidence in place of `hardware_shutdown`. The software check covers restart during processing and with unsaved work, refusal while TEMPO processes remain active, and completion after ordinary shutdown.

## Isolated signed update tests

**Test signed Windows updates and repair on isolated builds** accepts the historical `v1.0.2` trial label. It builds three CPU applications from the selected source, with a different pinned version of tqdm in the first build, and signs an isolated test feed. The trials cover delta failure, recovery-base download, successive and skipped replacements, offline repair after simulated damage, and bundled diagnostics after restart.

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
python -m scripts.windows_update_release prepare --manifest <signed-release.json> --index <tempo-update-index.json> --package-dir <package-directory> --artifacts <artifact-inputs.json> --public-keys <reviewed-public-keys.json> --signing-key <private-key.pem> --key-id <reviewed-key-id> --dependency-cache <verified-source-cache> --native-repository <velopack-checkout> --native-pin <reviewed-pin.json> --output <new-release-set-directory>
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

Copy the complete set to the team's approved release evidence store. A new maintainer should ask the release owner for access and confirm they can retrieve a previous release set and its independently retained catalog digest. Keep earlier sets and full update bases while installed users need them for updates or recovery, including after a version leaves the active feed.

Recover a separate copy from that storage and verify it using the independently retained digest and public map:

```powershell
python -m scripts.windows_update_release verify --root <recovered-set> --public-keys <independently-trusted-public-keys.json> --catalog-sha256 <approved-catalog-sha256>
```

Verification rejects altered signatures, changed/missing files, links and extra files. It rechecks native/package identities and acceptance bindings. Historical signed metadata remains usable for this archive check after feed expiry. This does not make it eligible to advertise a fresh update. Keep historical public keys available for offline verification without silently changing the installed application's active trust map.

Save the result of the recovered-copy check with the release approval record. Actions artifacts expire after seven days.

## Check served assets before and after feed activation

After publication is separately authorized, stage the **exact** `public/` files and root catalog on a public versioned prerelease. Keep `private/` in the approved evidence store. Leave the new feed unadvertised while checking the served bytes:

```powershell
python -m scripts.windows_update_release check-publication --root <recovered-set> --public-keys <independently-trusted-public-keys.json> --catalog-sha256 <approved-catalog-sha256>
```

This is a read-only check. It requires the versioned public prerelease, verifies the target tag's full source commit, and streams every declared public asset and the catalog to compare sizes and SHA-256 values. Source indexes, repair material, previous bases and NVIDIA parts receive the same check. A missing or changed asset fails before any feed readback.

After all applicable acceptance and durable-retention reviews pass and activation is authorized, publish the exact retained index last. For stable delivery, upload the index to the versioned prerelease after its other assets have passed, then promote the approved release to stable/latest as the final eligibility change. Prerelease delivery uses the separately reserved `updates-prerelease` feed location. Preserve the signed channel in both cases.

For v1.0.6, after uploading the staged index and before promoting the release, run **Build & Package Windows Release** with `release_tag=v1.0.6`, the certified build commit in `assemble_source_commit`, and the exact index SHA-256 in `staged_index_sha256`. Both Windows jobs must succeed. They use the published v1.0.4 installer and update service with the versioned index, exercise the delta and full-download fallback, and check that the updated desktop confirms the signed installation.

Repeat the read-only check with `--include-feed`. It checks actual served assets first and the configured client feed URL last. Stable verification also requires the exact target to be stable/latest; prerelease verification requires the matching prerelease flag. Expired release metadata cannot pass publication checks. An index mismatch, stale source tag, unsafe redirect or corrupted asset blocks verification. These commands perform no public writes and never enable updates.

## Rotate an installed signing key

The client accepts up to sixteen signatures over the same canonical release payload. `scripts.package_windows_updates` supports a primary signer and explicit additional signers:

```powershell
python -m scripts.package_windows_updates <existing release arguments> --trusted-keys <reviewed-public-keys.json> --additional-signing-key next-key=<next-private-key.pem>
```

Every co-signer, including the primary signer, must match that explicit public map. Additional signatures do not change the signed payload, asset identities or sequence. Private keys are read only for signing and are not copied into output.

For rotation, first certify an application and companion that contain the reviewed overlap of old/new public keys. Co-sign metadata during the transition so clients with either trust map can authenticate it. The current client validates every record in an index: keep the keys needed by older records in the overlap map, or remove unsupported records from the active index while retaining their offline release sets. Test the complete old-client-to-bridge-to-new-client path before cutting over. Do not retire a signer while clients that depend on it must still update through the shared feed. A compromised key or unsupported old client may require a separately verified manual installer; metadata expiration or changing a GitHub flag does not replace installed trust.
