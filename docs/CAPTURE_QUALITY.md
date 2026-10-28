# Recording storage, focus and quality review

## Choose where new data is saved

Open **Settings → Optical Acquisition → Data Folder**, choose a folder, then
**Save Personal Settings**. An empty field uses **Documents/TEMPO** in your user
account. New recordings use `Raw/`; new analyses use `Processed/`. A running
analysis keeps the destination it started with. Changing this setting does not
move existing recordings, results, or queued files.

The queue and processing history live in the personal configuration directory,
under `processing/`. On first use, TEMPO copies a legacy application-local queue
and history if their replacements do not exist. Original metadata and recording
paths remain intact. Keep the selected data folder backed up independently of
application updates. Imported files remain in their existing locations.

## Decide which videos to keep

Capture has two independent options, both initially unchecked:

- **Save Original Recordings** keeps the full field recording.
- **Save Intermediate Cell Videos** keeps cropped, masked cell videos.

**Native lossless** retains acquired unsigned 8-bit or 16-bit pixels in BigTIFF.
**8-bit compatibility** uses a fixed full-scale intensity conversion and records
that conversion in metadata. It can discard intensity precision. Processing of a
new capture uses its native temporary footage regardless of the retained format.
Only exported footage is converted.

Compatibility video formats require even frame dimensions. TEMPO pads odd-sized
images, records the padding and removes it on readback; it does not crop source
edge pixels. Before native capture, TEMPO checks disk writing and available
space. A short disk check cannot establish the sustained rate of your camera,
driver and storage; check for dropped frames in actual recordings.

With both options unchecked, a recording still flows through processing.
Successful results keep source measurement traces, computed measurements,
confirmed masks, a native reference image, and provenance/quality metadata.
TEMPO deletes its owned temporary footage only after successful processing,
durable history/queue updates, and result checksums are verified. Imported files
are never deleted. Failed or interrupted captures remain available for recovery.

Keeping traces permits later scalar-kinetics analysis. Changing outlines,
recomputing pixel measurements, or recomputing optical flow requires original
footage; a single reference image cannot substitute for a video.

## Live focus assistance

Capture displays **Focus** alongside the healthy/dead cell estimate. Checks run
on already acquired preview frames without taking over camera acquisition.
Focus is assessed independently of whether Nano Scout can count cells. The
display identifies this feature as **Beta**.

Possible results are **Sharp**, **Out of focus**, **Mixed focus**, and
**Unavailable**. A missing or incompatible reviewed profile produces Unavailable;
TEMPO does not certify an image from edge sharpness alone. The integration ships
without a calibrated focus profile. Its image features combine intracellular
texture, periodic detail, edge response and exposure evidence. The current
feature method must be evaluated against expert labels before using its focus
classifications for scientific decisions.

Profiles are specific to optical configuration, exposure, gain and sensor depth.
Changing these inputs invalidates matching guidance. A preview assessment
describes its sampled frame, not an entire recording. Recordings preserve their
sampled assessments, frame identities, times, method/profile identifiers and
protocol flags. Saturation is evaluated against the active sensor bit depth
when it is available.

The existing **Z-index** remains operator-entered metadata. It is not a
calibrated physical depth. Focus assistance does not move Z, recommend a
direction, or establish a best depth.

## Focus review during automatic collection

A qualifying field whose focus is not Sharp pauses collection for inspection.
The live camera preview and manual focusing remain available. **Resume
Collection** records an override for that field only; later fields are checked
again. The field is counted again after review and is skipped if its healthy
individual-cell estimate no longer meets the collection threshold.

For a stimulated well, pacing continues during the first 60 seconds. Only an
explicit resume after inspection/refocusing counts as a response. If the
response window expires, TEMPO requests a stop, distinguishes acknowledged
resting output from an unconfirmed stop, and flashes a warning. Resume requires
acknowledged pacing and another configured timed or manual pre-pacing interval.

The paused field and subsequent fields in that well retain warnings about
**possible cell-viability effects** and **possible measurement-validity effects**.
Earlier fields are not retroactively flagged. The elapsed time establishes a
protocol deviation; TEMPO does not measure a biological change. Session and field
records distinguish pacing periods, initial pre-pacing and repeated pre-pacing.
Unstimulated collection requires focus review without a pacing timeout.

## Review quality and include or exclude data

In **Physiological Analysis**, load the results CSV and choose **Recording
Quality…**. Each recording has its own **Include** checkbox and **Reason** field.
Flagged recordings are included by default. Uncheck Include to exclude that
recording from summaries and plots, optionally enter a reason in the same row,
and choose **Apply**. Recheck it to include it again.

Decisions are stored beside the CSV, bound to its checksum. The imported CSV is
not rewritten. Assessment flags remain attached even when a reviewer includes
the data. Decision history preserves the local account identity, time, reason
and each change. A changed CSV requires a fresh review; unsuccessful saves keep
the unsaved choices visible. Legacy data without assessments remain unknown.

**Export Analysis…** produces a ZIP containing the results with current inclusion
decisions, saved traces, masks, reference image, provenance and available quality,
capture and spatial metadata. The package has file checksums and preserves
decision history. It does not include source or intermediate videos. Back those
up separately when they are retained.

## Reanalysis and recovery

Select a result in **Kinematics Extraction** and choose **Correct Outlines…**.
TEMPO verifies its original or retained source video, then opens the saved
outlines in the review window. Select a table row, or Command-click (Mac) /
Ctrl-click (Windows/Linux) an outline. **Add outline** draws a new object;
**Replace selected outline** redraws the selected boundary while retaining its
identity. Click corners and right-click to finish; Escape cancels an unfinished
outline. Choose the healthy/dead cell or group class and **Label Selected** to
change that label. **Delete Selected** removes the current outline. Standard
Undo reverses an edit. Include checkboxes determine which outlines to process.

**Create New Analysis** recomputes image measurements using the selected
outlines and current settings in a new directory. The previous analysis remains
intact. The new revision records the source analysis, source checksums, corrected
outlines, reviewer and time, and carries forward quality history/inclusion.
Re-running model detection replaces unsaved corrections only after confirmation.
Geometry reanalysis is unavailable without verified source footage.

Select a completed result in **Kinematics Extraction** and choose **Reanalyze
Traces…** to apply current kinetics settings in a new analysis directory. This
requires source measurement columns in the saved traces. Older processed-only
traces require original footage. Existing analyses remain intact. Image
extraction and optical flow are not repeated; unavailable flow results are
marked rather than copied from a different analysis.

For a timing-related failure with an available source file, **Set Frame Rate…**
records an explicitly declared rate and adds the recording back to the queue.
That assumption and its history remain in provenance. Camera timestamps are
used when complete; host receipt times are not exposure timestamps. Gaps split
transient analysis so a measured beat cannot span a known acquisition gap.

To recover an interrupted capture, choose **Add Recordings** and select its
temporary `.tif` file. Manual captures are under `Raw/.capture-cache/`; automatic
captures are in the experiment's Raw folder. A system file picker may require
showing hidden files to reach `.capture-cache`. TEMPO offers **Recover Readable
Frames** for an interrupted owned capture. Recovery makes a new copy, retains
the original unchanged, adds a partial-recording warning and queues the copy.
Then choose **Process Queue**. A capture still owned by an active writer cannot
be recovered or processed. Preserve the data folder if recovery fails.

## Developer interface and validation limits

`tempo_recording` owns native I/O, source timing declarations, recovery and
post-success retention. `tempo_quality` owns versioned image evidence and
append-only inclusion decisions. `tempo_quality_ui` provides recording-level
review; `tempo_analysis_io` creates portable bundles and trace-only revisions.
Keep acquisition I/O in workers and keep assessment work bounded independently
of camera acquisition. Do not silently substitute display-normalized frames for
scientific pixel measurements.

The optional profile is `focus_profile.json` in the personal configuration
directory. `fit_profile` requires explicitly reviewed train/test fields with both
focus labels in each partition; fields and identical source pixels cannot cross
partitions. Its optical signature must match the capture quality signature.
Keep the expert reference set and profile validation together. A fitted profile
is not evidence of adequate accuracy by itself: review false-Sharp cases,
concentration differences, weak sarcomeric texture, sharp edges with defocused
interiors, saturation and live changes in focus. Calibrate and evaluate on
separate fields before claiming reliable guidance.

Offscreen tests and simulated stimulation acknowledgements establish software
behavior only. Camera throughput/duration and recovery need the target camera;
output, discharge and communication-loss behavior need the physical stimulator
and oscilloscope before hardware release acceptance.
