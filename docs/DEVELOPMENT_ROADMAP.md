# TEMPO-C v1.1 roadmap

These are proposed changes. For the contracts and checks that apply to the current application, see [Development and verification](DEVELOPMENT_AND_RELEASE_GUIDE.md).

## Validation and architecture

Validate focus on held-out fields from the intended microscope. Include different cell concentrations and fields whose cell edges look sharp while intracellular detail is blurred. Exercise manual focusing, automatic collection, recording quality decisions, and saved analysis and export. Review the desktop with independent collaborators. Test camera, stage, and stimulation behavior on the target hardware.

Continue moving acquisition and analysis responsibilities out of `tempo_gui.py` into dedicated controllers.

## Proposed features

### Configurable outlier-beat inclusion

Give users an analysis option to include or exclude beats identified as outliers, with configurable definitions of an outlier. Apply the rules to detected beat measurements rather than treating the peak-detection threshold as an outlier policy.

- Let users choose the measured properties and criteria, such as amplitude, beat interval or contraction/relaxation duration, with explicit units, thresholds, rule combinations and reference population. Support absolute bounds and configurable deviation rules where the data supports them. Distinguish missing measurements or insufficient reference data from confirmed outliers.
- Preserve every detected beat's identity, original measurements and trace, the criteria that flagged it, and the chosen inclusion policy. Including flagged beats keeps their outlier status visible; excluding them changes the applicable summaries without deleting source data or inventing new beat intervals.
- Recalculate affected analysis summaries and show included, excluded and total beat counts. If no eligible measurements remain, report that state explicitly. Save the rules and policy with the analysis and carry flags, inclusion decisions and counts through reopening, reanalysis and export so results can be reproduced.

### Expanded theme colors and customizable fonts

Expand beyond Dark, Light and Oatmeal with a substantially larger selection of color themes. Let users choose the interface font family and size, preview their choices and restore defaults. Save appearance choices as personal preferences and apply them consistently across controls, dialogs, plots and help views, with a fallback when a chosen font is unavailable. Preserve readable contrast, meaningful status/selection colors and scientific color scales. Review rendered theme/font combinations at ordinary, wide and small-window sizes, including larger fonts, to keep text, controls and plot labels readable and reachable.

### Experiment workspace and editable plate layout

Organize recordings, analysis revisions and notes within an experiment. Assign treatments, concentrations and units, controls, preparation identities and replicate groups to wells. Build on existing recording/well/field identities and preserve the experimental design with results and exports rather than relying on filenames or a separate spreadsheet.

### Comparisons across conditions and experiments

Select control wells, compare treatment groups and concentration responses, and compare baseline with post-treatment recordings. Show individual-cell distributions alongside well and biological-replicate summaries, with grouping, pairing, normalization and inclusion decisions explicit. Preserve the comparison settings and source analysis revisions so comparisons can be reopened and reproduced.

Include linked population selection: plot any two available measurements, select cells by numerical ranges or drawn regions, and highlight the same cells in the spatial map and results table. Save named populations, selection rules, source revisions and membership with comparisons and exports. Preserve experimental treatment/replicate identities separately from these measurement-based selections.

### Reusable acquisition protocols

Save named acquisition sequences with well assignments, camera/recording settings, pre-pacing, pacing changes, settling intervals and operator checkpoints. Let users inspect the sequence before starting and repeat it across experiments. Record requested steps, acknowledged hardware settings, actual execution and interruptions with the acquired data; retain reachable stopping actions throughout execution.

### Repeated measurements at saved fields

Save named stage positions with reference images and optical calibration, revisit them after treatment or at scheduled intervals, and assist with image registration. Support paired field comparisons while recording elapsed time, alignment evidence and failed relocations. Use the same-cell tracking workflow below for individual-cell pairing; returning to a stage position alone does not establish cell identity.

### Same-cell tracking across recordings

Maintain persistent cell identities across baseline, treatment and recovery recordings. Show proposed matches over aligned reference images, provide matching confidence and manual correction, and explicitly identify unmatched or ambiguous cells. Preserve source recording/field/cell identities and matching revisions with paired comparisons and exports; missing or uncertain matches must not silently become confirmed pairs.

### Assisted focus recovery at revisited fields

With compatible motorized Z hardware, search a configured, bounded range around a saved position and assess intracellular detail using a validated focus profile. Present the proposed focus position for operator acceptance or apply it within an explicitly configured acquisition protocol. Preserve manual focusing, reachable stopping actions, requested and acknowledged stage positions, calibration/profile identity, and focus failures or overrides. Keep focus assessment distinct from authorization to move the stage and avoid certifying focus from cell-edge sharpness alone.

### Unified experiment quality review

Bring focus evidence, timing gaps, saturation, pacing interruptions, segmentation concerns and analysis exclusions into one experiment-level review. Link flagged results to the relevant image, trace or acquisition event, and distinguish unavailable evidence from a passing check. Reuse existing quality records and decision history, preserve reasons for inclusion/exclusion, and carry the reviewed state into comparisons and exports.

### Frame-linked video, trace and optical-flow review

When original or intermediate cell footage is retained, provide integrated playback and scrubbing linked to the measured trace. Selecting a trace time seeks to its corresponding video frame; playback moves the trace cursor. Show the selected cell's reference outline, available stimulation events and frame-associated optical-flow vectors. Support original-field and retained-cell views where available, and preserve frame/time correspondence across cropped footage, timing gaps and differing frame rates.

Compute or load optical-flow vectors from verified retained footage in background workers and retain their method, settings, frame associations and coordinate/scale information. Present these as an additional motion visualization alongside the existing MUSCLEMOTION signal. Use calibrated units only where supported; missing footage, timing or flow evidence must remain explicit. The existing recorded-signal view remains usable without footage, and optical-flow visualization does not replace fixed-reference intensity measurements.

### Pacing-response verification

Compare recorded stimulation events with observed optical contractions. Report pulses followed by a response, missed responses and extra contractions with supporting traces, assessment coverage and uncertainty. Carry the evidence into experiment quality review and allow operator checkpoints when response criteria are not met. Automatic stimulation adjustment is a separate extension; an optical response assessment does not directly measure electrical activation.

### Synchronized recording and pre-trigger buffering

Build on the recorded stimulation and video timing metadata to retain a configurable interval of acquired frames before a recording trigger. Preserve device events, frame identities, exposure and timestamp semantics, clock relationships, and measured timing uncertainty. Support hardware synchronization where available and assess software synchronization against the actual camera and stimulator. Keep buffering and event collection in background acquisition workers.

### Stimulus-to-contraction latency

Measure the delay from a recorded stimulus to observed contraction onset and compare distributions across cells, treatments and pacing frequencies. Preserve the onset method, event association, timing evidence and uncertainty with each measurement. Report unavailable or ambiguous associations explicitly, and distinguish stimulus-to-motion latency from electrical-activation-to-contraction delay. Sub-frame interpolation must not be presented as independently validated submillisecond or microsecond accuracy.

### Mechanical activation and synchrony maps

Map contraction onset across cells or image regions, visualize the spread of mechanical activity and summarize response synchrony. Link spatial results to the underlying traces, calibrated coordinates, event associations and timing uncertainty. Report unresolved delays and missing responses explicitly. Electrical conduction interpretations require separate validation; mechanical activation maps alone do not establish electrical conduction velocity or conduction block.

### Sarcomere length and shortening analysis

Where striations are sufficiently resolved, measure resting sarcomere length, shortening fraction and shortening/relengthening rates alongside existing motion measurements. Show the measured region, sarcomere-length trace and signal-quality evidence. Preserve spatial calibration, orientation/region selection, analysis method/settings and timing provenance with results and exports. Report unresolved striations or unreliable measurements explicitly, and validate against reference measurements before interpreting the new metrics scientifically.

Estimate the spatial spacing of repeated sarcomeric bands in each assessed frame and convert pixel spacing using a measured calibration matched to the optical configuration. This provides image-plane sarcomere spacing rather than a conversion of MUSCLEMOTION intensity amplitude into length. Account for orientation and unresolved/out-of-plane structure; do not infer whole-cell length or force from band spacing alone.
