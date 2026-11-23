# Recording storage, focus and quality review

## Choose where new data is saved

Open **Settings → Optical Acquisition → Data Folder**, choose a folder, then
**Save Personal Settings**. An empty field uses **Documents/TEMPO** in your user
account. New recordings use `Raw/`; new analyses use `Processed/`. A running
analysis keeps the destination it started with. Changing this setting does not
move existing recordings, results, or queued files.

The queue and processing history live in `processing/` under the personal
configuration directory. If an older installation has an application-local
queue or history, TEMPO copies it when there is no personal copy yet. Keep the
original recordings at their existing paths so the queue can still find them.
Back up the data folder alongside other research files.

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
**Unavailable**. Use a reviewed profile that matches the current camera and
microscope settings. Calibrate when TEMPO has no matching profile or the optical
setup has changed. The classifier uses intracellular texture, periodic detail,
edges, and exposure. Check its results against expert labels before relying on
them for scientific decisions.

Profiles are specific to optical configuration, exposure, gain, and sensor bit
depth. TEMPO uses the settings the camera reports to select a profile. A preview
assessment describes the sampled frame. Recordings preserve their
sampled assessments, frame identities, times, method/profile identifiers and
protocol flags. Saturation is evaluated against the active sensor bit depth
when it is available.

The existing **Z-index** remains operator-entered metadata. It is not a
calibrated physical depth. Focus assistance does not move Z, recommend a
direction, or establish a best depth.

To create a profile for your microscope, follow [Calibrate Focus Beta](#calibrate-focus-beta).

## Calibrate Focus Beta

These steps require a build with **Focus Calibration…** in Acquisition. The
published v1.0.4 and v1.0.5 installers predate this window. In a build with
the window, choose **Acquisition → Focus Calibration…**. Calibrate for the camera and
microscope settings you intend to use live. Keep the objective, illumination,
exposure, gain, binning, and image size or ROI fixed while collecting planes.

### Capture and label

1. If the camera is connected and configured as it was for the images, choose
   **Use Camera Settings**. For photos from an earlier session, you can also
   choose **Use Recording Metadata…** and open a completed **Native lossless**
   TEMPO recording or its `.tif.recording.json` sidecar from the same setup.
2. Choose **New Field** at the first XY position. With the camera feed running,
   adjust focus manually and select **Capture Plane** at each depth. Enter the
   **Z-Index** in Acquisition if you want to record it. **Show Live Preview**
   returns the viewer from a saved plane to the current camera image. You can
   also choose **Import TIFFs…** to add original snapshots or a multipage
   Z-stack from that field. Confirm the stack was captured at that position with
   the same settings.
3. Select a plane, inspect the intracellular detail, and set **Your label**.
   Use **Sharp** when sarcomeres are clear, **Out of focus** when that detail
   is visibly blurred, and **Skip** for ambiguous planes. **Actual Pixels**
   helps inspect fine detail. Crisp cell edges can still surround blurred
   intracellular detail.
4. Move to a different XY position before choosing **New Field** again.
   Use **Use to fit** for fields used to calibrate and **Hold back for checking**
   for independent fields. TEMPO initially assigns the first field to fitting
   and the second to checking. Include sharp and blurred planes in both sets
   and sample representative cell densities. All planes in a field stay together.

TEMPO saves the draft, labels, and native plane pixels under **Focus
Calibration** in your selected data folder. To resume, reopen the calibration
window and choose the same camera settings or reference metadata. Earlier
TEMPO snapshots may have different **FOV ID** numbers for planes taken at
the same XY position. Group those files using your acquisition notes.

### Review and apply

1. Choose **Fit and Check**. TEMPO fits on the assigned fields and opens **Review
   Results** with predictions on the held-out fields.
2. Select a result to inspect the original plane, your label, and TEMPO's
   classification. Investigate disagreements, especially blurred planes marked
   **Sharp**. Check any saturation or inadequate-signal flags. If you change the
   fit after seeing the results, hold back fresh fields for the next check.
3. Choose **Apply Profile** after reviewing the results. TEMPO uses the profile
   immediately when the reported camera settings match. Check live preview on
   a fresh field while moving through focus before using the profile during
   automatic collection. If a previous profile exists for these settings, use
   **Restore Previous Profile…** in **Review Results** to switch back.

If **Focus** reads **Unavailable**, hover over the label for the reason. For a
missing profile, calibrate or use a setup that already has one. Match the
objective and any other settings you change by hand. For insufficient image
detail, check that cells are visible in the live image.

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
Quality…**. The review table organizes recordings by **Well** and **Field**,
identifies specific flagged cells alongside field-level quality flags, and
provides an **Include** checkbox and **Reason** field for each recording.
Double-click any row or click its **Inspect** button to immediately close the dialog
and center the Physiological Analysis map and statistics on that specific field.
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

The desktop workflow and the offline fitter use `tempo_quality.fit_profile`
with the recorded settings signature and active sensor bit depth. Keep fields
and identical source pixels in one partition. Acquisition can read existing
`focus_profile.json` files. Profiles applied in the window are stored separately
by settings signature.

Maintainers can fit on another computer from the TEMPO source checkout. In the
project Python environment, use a CSV with columns `image,field,partition,label`.
List native single-plane TIFFs relative to the CSV, use `train` or `test` for each
field, and label each example `sharp` or `out_of_focus`. Run this command from the
checkout with the recording or sidecar from the matching camera setup.

```sh
python -m scripts.calibrate_focus --manifest focus-labels.csv --reference reference.tif.recording.json --output focus_profile.json
```

Review the held-out results before handing off the candidate. An existing
single-profile installation reads `focus_profile.json` from the personal
configuration directory at startup.

Offscreen tests and simulated stimulation acknowledgements establish software
behavior only. Camera throughput/duration and recovery need the target camera;
output, discharge and communication-loss behavior need the physical stimulator
and oscilloscope before hardware release acceptance.
