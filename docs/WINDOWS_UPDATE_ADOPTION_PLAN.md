# Windows update adoption plan

## Objective and release scope

Deliver in-app updates for packaged Windows TEMPO in v1.0.2. Prefer incremental downloads when they reduce transfer cost, retain a verified full-package fallback, and install only after the user accepts the restart and TEMPO finishes its work. Existing installations require one manual upgrade to obtain the updater.

The experimental stimulation package remains optional. Its experimental results and incorporation into the standard application are separate from updater adoption; v1.0.3 is a possible integration release, conditional on those results. An application update must not silently remove an installed experimental package or treat its modified executable as an unmodified base.

This document is an implementation and acceptance plan, not a statement that the updater is implemented or release-ready. The starting candidate is Velopack 1.2.161. Adoption depends on resolving its verification, download-selection, and Windows recovery behavior. A later upstream version or a narrowly maintained source patch may replace that candidate after the same acceptance checks.

The initial scope is per-user Windows x64 installations. Keep source-checkout updating and the macOS acquisition/development environments separate. Do not install Python, pip dependencies, or a .NET runtime on users' computers as part of application updating; build tools belong in the isolated release environment.

## Required outcomes

1. Users can check, download, defer, install, and restart from TEMPO without locating an installer themselves.
2. Invalid downloads, insufficient space, cancelled operations, and failed installation leave an identifiable recovery path and preserve research work.
3. Preferences, credentials, onboarding progress, models, collaboration images and drafts, queued work, recordings, analysis outputs, and experimental records survive migration and subsequent updates.
4. Acquisition, stimulation, processing, assistance, firmware flashing, and pending writes retain their existing shutdown protections.
5. Application code, the optional NVIDIA worker, and any installed add-on retain explicitly supported version relationships.
6. Builds remain tied to exact source revisions, dependency locks, verification contracts, notices, and manually approved publication.

## Adoption decisions

| Area | Proposed decision |
| --- | --- |
| Application packaging | Keep PyInstaller folder output; place Velopack around the verified payload. Replace Inno as owner of new installations only after migration and recovery gates pass. |
| Installation identity | Use a stable, distinct package ID such as `TEMPO.Bio.Desktop`; reserve `%LOCALAPPDATA%\TEMPO.Bio.Desktop` for the application. Confirm the final ID before the first public updater build. Never use the existing `%LOCALAPPDATA%\TEMPO` user-data directory as a replaceable application root. |
| User storage | Keep existing preferences, credentials, downloaded models, and recordings in their current external stores. Move remaining writable collaboration state to a stable user-data location. |
| Update policy | Check without blocking startup; download only on request in v1.0.2; install only after explicit acceptance and safe shutdown. No unattended installation during an experiment. |
| Release channels | Stable by default; prerelease only by explicit selection. Channel eligibility, package identity, architecture, version, and source revision must agree. |
| Patch selection | TEMPO explicitly evaluates base identity, chain continuity, transfer size, disk space, and supported file sizes. Do not rely on the candidate Python client's planner settings. |
| NVIDIA support | Preserve exact application/worker compatibility. For v1.0.2, prepare the matching worker through the existing verified multipart route before switching an opted-in installation. NVIDIA incremental transfer is a separate optimization described below. |
| Experimental add-on | Detect existing receipts before migration or replacement. Require a compatible replacement package or an explicit choice to remove/defer; preserve experiment data and package recovery material. |
| Recovery | One component owns the application-directory transaction. Require verified restoration behavior before adopting that component. A wrapper alone does not repair destructive cleanup inside the updater. |
| Publication | Build certification and release publication remain separate. Retain previous full update packages outside expiring Actions artifacts. |

Velopack's [Windows layout](https://docs.velopack.io/packaging/installer) replaces the `current` directory during updates. Its [delta format](https://docs.velopack.io/packaging/deltas) depends on retained full packages. Those constraints determine storage migration and release retention.

## Work sequence and gates

| Phase | Main deliverable | Dependency | Exit gate |
| --- | --- | --- | --- |
| 0 | Adoptable updater and recovery mechanism | None | Native Windows replacement failures preserve a recoverable application; candidate defects have explicit fixes or controlled workarounds. |
| 1 | Release identity, signed metadata, and verified package contract | Phase 0 candidate selected | Reused caches and reconstructed content are independently verifiable; invalid or incompatible releases cannot be applied. |
| 2 | Stable user storage and restartable migration | Inventory can begin immediately | All supported legacy stores survive repeated and interrupted migration without overwriting differing work. |
| 3 | Update service and safe application handoff | Phases 0–2 | Download, cancel, defer, install, restart, and recovery states behave correctly around active work and multiple instances. |
| 4 | NVIDIA and optional-package coordination | Identity contract; migration inventory | Every supported installation can update without silently losing its selected capabilities or research data. |
| 5 | Windows packaging and release automation | Contracts from phases 0–4 | Exact installer and update packages pass installation, upgrade, recovery, and uninstall checks on a separate runner. |
| 6 | Reviewed update interface and operator instructions | Service states defined | User selects a rendered interface direction; representative layouts and real interactions pass review. |
| 7 | Prerelease adoption and stable release | All preceding gates | Legacy migration and two successive updater-managed upgrades pass; approved artifacts are published without rebuilding. |

Storage inventory, package-contract design, and CI preparation can proceed independently. Do not complete a broad installer replacement before phase 0 establishes a safe installation owner. Visual alternatives can be prepared once the state model is stable; do not silently choose a new layout.

## Phase 0: establish a safe updater foundation

### Candidate selection

Pin matching Python, CLI, and native updater versions and retain their hashes and matching source. Review the implementation used by Python, rather than assuming the C# implementation or current documentation describes the same behavior.

The candidate's [Python/Rust manager](https://github.com/velopack/velopack/blob/1.2.161/src/lib-rust/src/manager.rs) needs explicit coverage for cached-target verification, patch-count limits, and total patch size. Its [compression code](https://github.com/velopack/velopack/blob/1.2.161/src/vpk/Velopack.Packaging/Zstd.cs) also requires a tested large-file boundary; treat a changed file at or above 1 GiB as unsupported for this candidate until a replacement is validated.

First test installation, update, and uninstallation with a minimal Windows fixture that includes the real Velopack startup integration. Inject failures before staging, after extraction, after the old directory moves, during replacement, and during restart. Include locked files, denied writes, updater termination, and simulated interruption of the transaction. Never use a real research installation for these checks.

### Recovery requirement

The current [Windows replacement implementation](https://github.com/velopack/velopack/blob/1.2.161/src/bins/src/commands/apply_windows_impl.rs) requires review of process termination and backup cleanup on error. Choose one of these paths before proceeding:

1. Prefer an upstream version whose restoration and backup-retention behavior passes the failure matrix.
2. If necessary, maintain a narrow, reviewed patch to the native updater, with reproducible builds, source attribution, and regression tests. Keep its identity distinct from the unmodified upstream artifact.
3. If neither is acceptable, stop the Velopack installation migration. Replan in-app updates around the existing installer, preserving the chosen v1.0.2 updater scope; do not quietly ship the known recovery gap.

The selected transaction owner must stage the new application, retain the old application and matching component selection, journal its transitions outside replaceable directories, and restore the old application if activation fails. A repair entry point must operate when the main Python application cannot import or start. Do not create competing directory-swapping logic in the GUI and updater.

Before activation, retain a verified known-good full installer or package with the required repair executable locally, for example under `%LOCALAPPDATA%\TEMPO\updates\recovery`. This store must be outside the application root, Velopack-managed caches/temporary backups, and old-installer deletion scope. Its offline procedure must work even when the ordinary updater or transaction journal is damaged. Include this retained copy in space estimates and remove it only under an explicit retention policy after a replacement recovery copy is verified.

Distinguish an installation failure from a new version that starts but later proves faulty. Any launch of an older binary, including manual reinstall or repair, must be compatible with newer shared preferences, migration receipts, queues, and research writes. Gate automatic restoration on that compatibility. If an older binary cannot safely read the current stores, preserve newer writes and provide a supported forward repair or read-only recovery path; retaining an old executable alone does not establish usable recovery. Never reverse newer research writes as a side effect of application rollback.

**Phase 0 acceptance:** a failed replacement does not delete the sole usable previous application; an interrupted operation can be resumed or repaired without the main GUI; offline repair works with a broken updater/journal and compatible preserved data; restart failure retains the previous payload and transaction evidence. The real Windows executable must acknowledge updater lifecycle arguments before normal initialization.

## Phase 1: define identity, verification, and package selection

### Release identity and trust

Make `tempo_release.VERSION` the application-version source and remove conflicting filename/version literals from packaging and installer-verification code. A release identity includes application version, full source revision, package ID, architecture, channel, dependency-lock identity, updater version, and metadata schema version.

Publish signed TEMPO update metadata with a versioned schema. Embed its trust anchor in the application and bootstrap/repair component. Keep private signing material outside the repository. Define supported key rotation and expiration behavior before the first public feed. A downloaded checksum from the same untrusted metadata is insufficient to establish publisher identity.

The release record must identify:

- Full package and installer: immutable asset names, exact byte counts, and SHA-256 hashes.
- Each patch: exact base and target identities, ordered chain links, byte count, hash, and required updater version.
- The final application file inventory: canonical relative paths, sizes, and SHA-256 hashes, including executable helpers and shipped source/notices.
- Optional NVIDIA target identity, transfer descriptor, and component inventory; optional add-on compatibility declarations.
- Expanded size, staging requirements, metadata validity, and a monotonically ordered publication record for stale-feed handling.

Reject unsafe Windows paths, duplicate or case-colliding entries, traversal, links/reparse-point escapes, oversized metadata, unreasonable expanded sizes, unexpected executables, and mismatched identities. Reject automatic downgrades. An explicit repair to a previously trusted version is a separate operation with data-compatibility checks.

Code signing and update-metadata signing have separate jobs. Add a Windows code-signing configuration to the release process and verify the identity of updater/installer executables. If publisher code signing is unavailable, resolve and document that release decision before publication; trusted metadata verification remains required. Do not promise that signing eliminates all Windows reputation warnings.

### Verify downloads, caches, and reconstructed content

Check package size and SHA-256 before accepting a downloaded full package or patch. Recheck reused cached packages; a matching filename or successful prior receipt is not proof that bytes remain intact. Preserve a usable verified base until its replacement is committed.

After patch reconstruction, validate every target file against the signed inventory and reject missing or unexpected files. Do not assume the reconstructed ZIP has the same hash as the originally published full ZIP: recompression can change archive bytes while preserving member contents. Store a verified content identity for reconstructed bases and validate that identity before subsequent reuse.

Unverified archive content must never replace or execute the updater itself. If the selected library extracts helpers during its download operation, establish verification before that side effect or change that path. Serialize validation and handoff, recheck the exact transaction inputs immediately before replacement, and prevent another TEMPO instance from rewriting the staged package during activation.

### Select the transfer explicitly

Use working policy defaults of no more than four patches and a combined patch download below 75% of the full package. These are configurable implementation defaults to benchmark, not claimed optimal thresholds.

Choose a full download when the base cannot be verified, a chain link is missing, an installed overlay changes the base, the chain exceeds policy, a changed file exceeds the candidate's tested limit, or the full download is cheaper. Preflight free space on every destination volume using actual expanded and retained sizes, not a universal fixed-space estimate.

If a patch fails verification or reconstruction, keep the current application and offer or perform the already-authorized full fallback. When fallback materially increases the displayed download size, let the user accept that transfer or defer. A failed patch must not retry indefinitely. Retry downloads with bounded timeouts; only advertise resume when byte-range handling is implemented and tested against the published assets.

**Phase 1 acceptance:** corrupt cached targets and bases are rejected; broken or uneconomic chains choose full packages; signed target inventories validate reconstructed content; modified helpers cannot run; stale, wrong-channel, wrong-architecture, or mismatched releases cannot install.

## Phase 2: move writable state and migrate legacy installations

### Storage inventory

| State | Current contract | Adoption action |
| --- | --- | --- |
| Preferences and onboarding | `%LOCALAPPDATA%\TEMPO\config` | Preserve paths, appearance, settings, load errors, and completed/incomplete setup state. |
| Credentials | Windows Credential Manager | Preserve credential targets and account selection; do not export secrets into migration files. |
| Downloaded models | User storage selected by `tempo_model_catalog` | Preserve repository-specific directories and installed identities. Verify legacy bundled/local model fallbacks before moving an installation. |
| Collaboration images and sessions | `get_collaboration_dir()` currently derives from the application base | Migrate to a stable root such as `%LOCALAPPDATA%\TEMPO\collaboration`, retaining workspace isolation, drafts, reservations, pending submissions, and recovery backups. |
| Queues, evaluation and training work | A mix of explicit roots and relative paths | Enumerate actual writers and saved paths. Preserve local pending jobs, referenced artifacts, source snapshots, and compiled data; do not infer that all queues are cloud-only. |
| NVIDIA runtime | `%LOCALAPPDATA%\TEMPO\nvidia-runtime` | Preserve the old worker and selection until the matching new worker is ready. |
| Recordings, analysis and experiment records | User-selected data directories | Retain locations and metadata; do not relocate large research stores during an application update. |
| Add-on receipts and backups | May reside below `.tempo-packages` in the old installation | Detect and preserve before old-installer cleanup; use their compatibility and removal contracts. |

Audit writable uses of the bundle root, executable directory, current directory, and `get_base_dir()` throughout the application. Include annotation/training exports that currently write below `datasets`. Keep immutable assets/source reads in the bundle. Route generated caches and mutable state to their appropriate user stores. Respect custom paths, historical formats, workspace identities, and non-default account selections. Remap saved absolute paths only when they refer to a verified migrated store; do not rewrite user-selected external research paths.

### Migration transaction

1. Discover the old installation from its registered uninstall identity and verified executable/provenance, including custom locations and supported prereleases. A directory named TEMPO is not sufficient evidence. Check registered installations in the supported registry views; handle multiple candidates explicitly.
2. Establish application/worker locks and require other running installations to finish or close. Never force-close them to perform migration. Check add-on receipts before modifying the old installation.
3. Inventory all mutable stores and copy them to staged destinations. Retain original files; do not move-and-delete the only copy. Check available space, readable files, and Windows path/reparse-point safety first.
4. Verify sizes, hashes, and referenced-file consistency. Publish migrated stores and a versioned migration receipt only after verification. Existing identical destinations may be reused; differing files become a recoverable conflict, never a last-writer-wins overwrite.
5. Install the new application separately and validate its storage selection, account availability, setup progress, and offline reads before retiring old shortcuts or registrations. Do not mark a completed user as a new installation merely because the executable moved.
6. Keep migration restartable after interruption. Once published, use one authoritative data root; prevent legacy and new processes from independently editing separate copies. Preserve original migration snapshots without presenting them as active work.
7. Remove the old application through its known uninstaller only after user data and package recovery material are outside its deletion scope. Repair shortcuts and Windows application registration without deleting unrelated folders. A cleanup failure must not invalidate successful migration.

The supported transition is one active installation per Windows user, with the old payload retained for recovery rather than a second independently writable desktop installation. Assign new shortcuts and Add/Remove Programs entries to the correct installation owner. Test that either uninstaller cannot remove the other's application files or shared user data. Preserve source-checkout behavior and recognize legacy Inno installations separately from Velopack installations; a frozen app without `unins000.exe` must not be described as a source checkout.

Retain backups until migration and a subsequent normal restart are confirmed. Define a bounded, user-visible cleanup policy for older recovery copies; do not silently delete unsynced drafts or the only copy of an experiment artifact.

**Phase 2 acceptance:** migrations from v1.0.0, v1.0.1, supported previews, custom paths, and interrupted attempts are idempotent; differing destination work is preserved; fresh and established users retain the correct onboarding behavior; no mutable store depends on the replaced `current` directory.

## Phase 3: integrate the update service and safe shutdown

### Responsibilities

Keep network, verification, transfer selection, transaction persistence, and recovery logic outside Qt views. The view receives structured progress and actionable errors. Use background workers for network requests, hashing, extraction, and disk checks.

| Existing integration point | Planned responsibility |
| --- | --- |
| `update_wizard.py` | Present release details and progress; dispatch packaged updates to the new service; preserve the source-checkout path. |
| `tempo_gui.py` startup | Handle updater lifecycle arguments before native-library initialization. Preserve headless assistance, probe, verification, diagnostics, and Rosetta entrypoints. |
| `TempoDashboard.request_close_action()` and `closeEvent()` | Own acceptance of application shutdown; report when work prevents handoff. |
| `tempo_tasks.py` and `tempo_process.py` | Expose active work and ensure background/helper processes finish releasing resources. |
| `tempo_config.py`, `tempo_hub.py`, `tempo_first_run.py` | Stable storage and migration before services capture their paths; preserve onboarding state. |
| `tempo_uninstall.py` | Detect the installation technology and invoke its supported removal path with the existing shutdown protections. |
| New update service modules | Typed release/transaction records, verification, transfer policy, component preparation, and lifecycle orchestration. Split responsibilities when implemented; avoid another large GUI controller. |

Suggested implementation boundaries are `tempo_update_manifest.py` for release/inventory validation and transfer planning, `tempo_update_migration.py` for stable paths and migration receipts, and `tempo_update_service.py` for the update state machine and handoff. These are proposed new modules, not existing APIs. Keep the native replacement/recovery implementation selected in phase 0 outside those GUI-process responsibilities.

Call the selected Velopack startup integration before normal application startup, with automatic startup application disabled. Establish argument routing that acknowledges lifecycle hooks without constructing Qt, touching hardware, migrating data, or starting downloads. Model-worker subprocesses must not apply pending updates or open additional windows.

### State model

Use explicit states: `idle`, `checking`, `available`, `downloading`, `verifying`, `ready`, `waiting_for_shutdown`, `installing`, `confirming`, `complete`, `cancelled`, `failed`, and `recovery_required`. Persist only the states needed to resume or recover. Include transaction and exact source/target identities; do not infer completion from a progress bar reaching 100%.

Cancellation during checking or downloading leaves the current application running. Deferring a ready update keeps verified staging under a bounded cache policy. Cancelling while waiting for shutdown clears the close action and resumes normal work. Once replacement starts, expose its actual transaction/recovery state rather than promising instant cancellation.

Accepting Install and Restart reserves the transaction and prevents new work from starting. Downloads and hashing should be scheduled or throttled so they do not interfere with recording; begin with no automatic background download. Recheck space and all required component identities before requesting close.

Wait for processing, model assistance and descendants, annotation synchronization, maintainer writes, and capture/stimulation cleanup. Preserve existing unsaved/unconfirmed-write choices. Firmware flashing must finish before handoff. Keep stopping and cancelling reachable while waiting. If cleanup fails or the user cancels close, keep the current application and its update available.

Use an OS-level lock shared by all TEMPO instances, the installer, and recovery helper. Do not overwrite an existing pending close action. An updater timeout must abort handoff rather than authorize termination of another user's active acquisition. Start the native handoff only after the normal close guards have accepted and the bounded process-exit deadline can be met. Before invoking native apply, the handoff component must observe actual exit of every relevant application/helper process, including other instances: acceptance of a Qt close event is insufficient. Account for older installations that do not yet participate in the new lock protocol.

After replacement, perform a bounded, non-destructive offline health check and confirm normal desktop readiness. Keep that acknowledgment separate from any network login/settings refresh. Preserve repair access if imports fail or readiness never arrives. Never label the update complete merely because the helper launched.

**Phase 3 acceptance:** every active-work state can delay or cancel installation; successful handoff leaves no orphan worker; cancelling returns the app to a usable state; source checkouts and headless commands do not enter the packaged update path; multiple instances cannot race the transaction.

## Phase 4: coordinate NVIDIA support and experimental add-ons

### NVIDIA behavior required for v1.0.2

The optional NVIDIA runtime is a complete headless TEMPO worker pinned to the application version and source revision. Preserve that contract in `tempo_nvidia_runtime`, `tempo_nvidia_setup`, and `tempo_nvidia_manifest`. Reusing large libraries does not authorize using an old worker with a new application.

For an installation without NVIDIA support, update only the application. For an opted-in installation, disclose the combined application/worker transfer size and prepare the exact target worker through the existing verified multipart downloader before application activation. Split runtime preparation from activation: the current installer selects the worker immediately after probing, so it cannot be called unchanged for pre-update staging. Download, extract, verify, and probe the staged worker without writing the live `active.json` marker. Use target-aware validation rather than the running application's old version constants.

Retain the old worker and active marker for rollback. Journal application and component selection together; a crash between application activation and marker update must be reconciled before model assistance is enabled. Do not describe separate filesystem operations as globally atomic. If preparation or the GPU probe fails, leave the old app/worker pair usable and allow retry or deferral. A user may explicitly continue with CPU support, but that consequence must be visible and recorded.

### NVIDIA incremental optimization

After the required full-runtime coordination works, assess incremental NVIDIA delivery as a separately gated extension. Benchmark the actual full compressed worker, its largest individual files, reconstruction space/time, and dependency-changing releases. Use a distinct component identity and signed descriptor; avoid mixing ambiguous application and component packages in a generic GitHub release lookup.

Candidate delivery is a verified component full package plus a base-specific delta, using Velopack's patch engine only where its format and size limits fit. If the full compressed component exceeds the host's asset limit, define and verify multipart transport for that component rather than assuming a native single-asset source will work. [GitHub limits each release asset to under 2 GiB](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas).

Preserve the existing multipart full-worker fallback. Do not attempt to patch arbitrary sequential ZIP parts independently: changed offsets can defeat reuse and complicate validation. A future reusable dependency-component design needs explicit worker/runtime compatibility contracts and its own tests; it is not a prerequisite for core application increments in v1.0.2.

### Optional experimental package

Inventory installed add-on receipts, expected application hashes, backup hashes, supported base revisions, and partial-removal states. The currently published experimental overlay requires its supported v1.0.1 base and removal before upgrade. Do not copy that executable over v1.0.2 or reconstruct an application delta against its modified files.

Provide a compatible, separately installed add-on build if it is to remain available on v1.0.2. Treat that as compatibility work, not scientific validation or integration into the standard release. Preserve the existing experiment installation for users who need to finish collecting data until a compatible package is available.

If an installed package is incompatible, let the user defer the application upgrade or follow the package's verified removal procedure. Snapshot its receipt/recovery material first. Removal restores only the original verified application files; it must not delete experimental records, attachments, configuration, or independently installed firmware. Never flash firmware as a side effect of software updating.

**Phase 4 acceptance:** CPU-only and NVIDIA-enabled installations reach valid target configurations; staging leaves the current NVIDIA selection intact; failed component preparation leaves the old configuration usable; installed, absent, incompatible, modified, and partially removed add-ons have explicit outcomes; experiment data and firmware remain intact. Verify real NVIDIA inference, target-worker probing, preparation failure, explicit CPU continuation, deferral, and recovery on supported GPU hardware; a headless CI smoke check alone does not establish GPU acceptance.

## Phase 5: extend packaging and certification

1. Add the chosen updater dependencies and matching build tools to isolated, hash-pinned Windows environments. Update license/source inventories and provenance. Keep CPU/NVIDIA identities aligned and record any native updater patch source.
2. Preserve existing scientific, privacy, dependency, notebook, interface, source-runtime, and packaged-runtime gates. Verify the real frozen entrypoint with lifecycle checking enabled; the evaluation-only compatibility bypass must not reach release builds.
3. Generate and sign the target inventory, full package, installer, and supported deltas from the exact certified payload. Retain the exact previous public full package as input; do not rebuild an older version and call it the same base.
4. Enforce file-size limits, package/expanded-size limits, patch-chain continuity, and publication asset naming. A supported full-only release can bridge a dependency change; clients must later fall back correctly across that missing delta.
5. Reconstruct every published delta and compare its complete file inventory with the independently built full target. Verify the packaged notices, cloud source bundle, diagnostics, and worker entrypoints as well as the main executable.
6. Extend the separate Windows installation runner for fresh install, legacy migration, two consecutive updates, skipped versions, component coordination, failure recovery, repair, and uninstall. Use isolated accounts/stores and paths with spaces, non-ASCII characters, and custom locations. Avoid tests dependent on a developer checkout or preinstalled Python.
7. Record per-scenario transfer bytes, expanded bytes, peak additional disk use, verification/reconstruction durations, and preserved-state checks. Compare application-only and dependency-changing releases; do not extrapolate one timing or download ratio to every release.
8. Retain full packages, signed inventories, descriptors, matching source and provenance in durable release storage. Expiring Actions artifacts remain temporary transport/evidence. Keep supported intermediate patches available; a missing base or patch must degrade to a full update.

Add reusable tooling such as `scripts/package_windows_updates.py` and `scripts/verify_windows_updates.ps1`, with their exact interfaces defined alongside the release contract. Add focused regression modules for manifest/selection rules, storage migration, handoff/cancellation, component coordination, and recovery. Extend existing software-update, NVIDIA, uninstall, and release-verification tests where the behavior already belongs there; do not duplicate the same guarantee in several fixture frameworks.

For release-feed publication, upload all required assets before advertising their release record. Keep installers and packages immutable after publication; publish a new identity for a rebuild. Verify that the public release tag resolves to the certified full source commit. Check the served installer, update packages, signed inventory, NVIDIA descriptor and every runtime part against the exact signed names, URLs, sizes, and hashes, extending the existing published-runtime verification where appropriate. Publish the signed feed record or pointer last. CI certification does not publish a release or authorize switching users to it.

**Phase 5 acceptance:** the exact downloadable payload passes the full Windows matrix; certification includes update transactions and preservation of user state; the feed cannot advertise a partially uploaded release; previous source and update bases remain available.

## Phase 6: review the interface and documentation

Prepare a small set of actual rendered update-dialog alternatives with identical representative data and window states. Let the user select the direction before adopting changes to hierarchy, density, controls, or styling. Reuse the existing Settings entry point unless an approved alternative requires otherwise.

Show the installed/available version, relevant release changes, transfer size, required restart, and any NVIDIA or add-on consequence. Distinguish download readiness from installation completion. Errors must explain retry, defer, or repair. Keep routine verification details in expandable error details or focused information controls rather than permanent paragraphs.

Use standard native dismissal for informational states and retain Cancel when it cancels an actual download or pending handoff. Follow `FocusNeutralStyle` and the shared InformationButton interaction. Verify real pointer hover/departure, click, Escape, and keyboard activation. Keep active-state meaning visible without extra focus decoration.

Review ordinary, wide, small-window, and larger-font renders with real-length release notes, slow and failed downloads, low disk space, pending shutdown, incompatible add-ons, and recovery states. Check text bounds, button visibility, scrolling, and duplicate copy. Use isolated offscreen fixtures; native review uses one user-opened instance with an agreed exit check.

Update the README and getting-started guide with distinct fresh-install, one-time migration, later in-app update, offline/manual repair, and uninstall procedures. Explain the optional GPU download and add-on consequences where users make that choice. Update packaging/developer references and read back the saved text. Write v1.0.2 release notes only after reviewing the complete release diff, without publishing private troubleshooting history or local evaluation reports.

**Phase 6 acceptance:** the selected interface works in all required states and sizes; errors and consequences are actionable; published installation/update directions describe the implemented behavior.

## Phase 7: prerelease and rollout

Use explicitly versioned prerelease builds with a separate channel. Preserve default stable eligibility and ensure changing a GitHub prerelease flag alone cannot contradict the signed channel record. Confirm version ordering for prerelease-to-stable and same-source rebuilds.

First certify fresh installation and migration from supported legacy installers. Then exercise at least two sequential updater-managed revisions and a skipped-version path using the complete application. Include an opted-in NVIDIA installation and an optional-package participant. A self-update must preserve the updater's own ability to obtain the next release.

Run an operator acceptance session on the intended Windows hardware: no restart during recording/stimulation or firmware flashing, reachable stopping, release of hardware before handoff, restored camera/driver paths, preserved account/settings, and usable model assistance afterward. Automated simulated evidence remains separate from physical hardware acceptance.

Expand the prerelease audience only after migration and recovery gates pass. Promote the exact approved artifacts; if a version change requires new package metadata or binaries, create and certify that distinct identity rather than relabeling an existing package. Publish no release until authorized.

Retain a downloadable full installer and repair directions. For a withdrawn release, stop offering it in signed metadata; do not automatically downgrade users or rerun failed updates indefinitely. Preserve a path to a corrected release and make users who need action identifiable through local status and instructions, without collecting research data or credentials.

## Acceptance matrix

| Scenario | Required result |
| --- | --- |
| Fresh per-user install | Runs without external Python/.NET; correct first-run setup and uninstall registration. |
| v1.0.0/v1.0.1/preview/custom-path migration | Verified user-state preservation; old registration/shortcuts retired only after success. |
| Existing destination with different drafts | Both versions of the work retained; migration pauses for a resolvable conflict. |
| Add-on present, incompatible, or partly removed | No silent overwrite/removal; receipt and experiment data retained; clear defer/removal/compatible-package route. |
| CPU-only or opted-in NVIDIA installation | Correct target pair; failed GPU preparation leaves the old pair usable. |
| Current, older, prerelease, or wrong-architecture feed | Only explicitly eligible compatible targets are offered; no automatic downgrade. |
| Missing/corrupt base or cached target | Reverification rejects corruption; safe full fallback or actionable error. |
| Missing/oversized/uneconomic patch chain | Explicit policy chooses a full package without disabling future updates. |
| Large dependency file at supported boundary | Packaging capability checked; unsupported delta omitted cleanly; full update remains usable. |
| Dropped connection, cancellation, or retry | Current application unaffected; bounded partial-file cleanup; retry completes from a known state. |
| Low space or write/access failure | Installation does not begin, or transaction restores a usable application; recovery material retained. |
| Recording, stimulation, processing, assistance, pending save, flashing | Existing guards delay/prevent handoff; stop/cancel remains reachable; no forced termination. |
| Multiple instances or concurrent installer | One transaction owner; others cannot mutate its staging or restart the old application into replacement. |
| Failure at each directory/marker transition | Journal identifies state; old app/component pair recoverable without the main GUI; a broken journal/updater still permits verified offline repair. |
| New version cannot import/start or health check times out | Update not marked complete; bounded restoration or repair access remains. |
| Older binary after newer state/schema writes | Incompatible downgrade blocked; newer data retained; supported forward repair or read-only recovery available. |
| Two successive updates and a skipped version | Updater, source/notices, state, models, and next-update ability all retained. |
| Uninstall and repair | Correct installation owner; research stores retained; explicit preference/token removal choices still work. |

## Implementation boundaries and completion criteria

Maintain new regression tests under `tests/` and reusable packaging/verification tools under `scripts/`. Keep logs, captures, installers, queues, and migration fixtures in ignored artifact directories. Do not commit or publish as part of implementing this plan without authorization.

Implementation is ready for release review when every applicable gate has recorded evidence, the chosen updater version and recovery owner are pinned, legacy data migration is verified, all installation/component combinations have defined outcomes, and the actual Windows release payload passes the acceptance matrix. GPU incremental delivery may follow later; complete coordination of a user's selected GPU runtime may not.

Until those conditions hold, describe the updater as in development. Document any explicitly deferred optimization without presenting an unverified recovery or preservation requirement as complete.
