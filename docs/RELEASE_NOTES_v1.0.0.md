# TEMPO v1.0.0 — unreleased

TEMPO's first planned Windows x64 installer provides shared image annotation, Colab training integration, microscopy acquisition, video processing, and analysis.

- Connect your own Hugging Face account through browser sign-in or a personal token, with skippable first-run guidance.
- Reserve shared images, edit cell outlines and classes, save drafts, and submit complete images for review.
- Show collaborator profile/lab names while preserving account-based ownership of reservations and submissions.
- Adopt the last synced draft after a seven-day reservation expires.
- Review and approve annotations before they become training input; maintainers mark inseparable clumps as groups.
- Open the configured Colab trainer, follow reported progress, request cancellation, and install verified model updates.
- Process queued recordings with cooperative cancellation, saved outcomes, and exact output associations.
- Explore field and full-well measurements, separate cell/group traces and averages, and registered spatial data, with explanations of measured units.
- Save personal preferences separately from administrator-managed shared settings.
- Control camera capture, calibrated XY field collection, AU26 V2 stimulation, and pre-pacing, with recorded session metadata.
- Choose a data folder and optional original/intermediate video retention; native lossless recordings preserve acquired pixel depth and timing evidence.
- Review recording-quality flags and inclusion decisions, export analysis bundles, correct saved outlines when source footage is available, and reanalyze retained measurement traces.
- Run versioned model-evaluation protocols and explicitly publish verified proposed models with their regular-training settings.

## Installation

The installer is `TEMPO_Setup_v1.0.0.exe`. Keep it with the SHA-256 checksum and source provenance from its approved build. It includes Python and the runtime libraries; project weights are obtained through the configured private model repository unless explicitly bundled.

Camera acquisition requires compatible vendor drivers and Micro-Manager adapters. Existing-image annotation requires no connected camera. Training runs in Colab and has separate runtime and account requirements.

## Verification and limits

The manual Windows workflow checks the source and packaged runtimes, interface fixtures, scientific/regression tests, and installation, offline verification, and uninstall on a separate runner. These checks do not establish compatibility with every physical camera, real collaborator account, or Windows installation.

The following acceptance work is deferred to v1.1 and remains unverified:

- Focus calibration and validation against operator-reviewed fields. Focus assistance is Beta and ships without a calibrated profile; missing compatible profiles produce Unavailable.
- Physical camera throughput, stage calibration, acquisition recovery and shutdown, and stimulator waveform/discharge/communication-loss behavior.
- Native-interface acceptance and hands-on Windows/collaboration checks using independent collaborator credentials.
- Architectural modularization of `tempo_gui.py` into dedicated viewport, acquisition, and analysis controller delegates (retained as a unified shell for v1.0.0 stability).

Updated Colab GPU execution and scientific model-quality evaluation remain separate deferred checks. Existing synthetic workflow and automated inference checks do not establish scientific accuracy. Code signing is tabled; the planned installer is unsigned.

See [Windows packaging](PACKAGING_AND_DISTRIBUTION.md) and [collaboration](COLLABORATION_WORKFLOW.md) for setup and usage.
