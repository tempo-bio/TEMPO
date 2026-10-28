# Stimulation and capture

TEMPO supports the AU26 V2 stimulator: an Uno R4 Minima with the OPA340 buffer,
CD74HC4053 output switch, and optional CD74HC4051 eight-channel multiplexer.

## Install or update the Arduino

1. Connect the Uno R4 Minima by USB. Finish any active capture or stimulation.
2. In Acquisition, choose **Firmware…** under **Stimulator Connection**.
3. Select its port and choose **Install / Update Firmware**. This replaces the
   Arduino's current sketch. Keep the board connected through verification.
4. Close setup and choose **Connect**. Connecting leaves the output
   in resting discharge; starting stimulation is a separate action.

No Arduino IDE or terminal is required for guided setup. Initial setup needs
internet access. TEMPO downloads checksum-pinned Arduino CLI 1.5.1 and installs
Arduino's `renesas_uno` core 1.6.0 in TEMPO's application-support directory,
separately from the user's Arduino IDE installation. Later uploads reuse them.
Guided setup verifies the selected board is an Uno R4 Minima before uploading,
then checks TEMPO's firmware protocol and confirmed resting discharge.

If setup fails, retain the error details, close other serial applications, check
the data-capable USB cable, and retry. Bootloader recovery may require the board's
reset button. OS device permissions or drivers may require system setup.

**Manual backup:** open `firmware/tempo_pacer/tempo_pacer.ino` in Arduino IDE,
install **Arduino UNO R4 Boards**, select **Arduino Uno R4 Minima** and its port,
and upload. Alternatively, Arduino CLI uses the board identifier
`arduino:renesas_uno:minima`. Close the serial monitor before connecting TEMPO.

The firmware's `DAC_REFERENCE_V` is inherited as 4.77 V from AU26. It must match
the measured reference of the actual board for useful amplitude calibration.
The displayed voltage is the requested DAC voltage, not measured electrode
voltage. A0 holds the amplitude, D2 selects stimulation/discharge, D3 isolates
the output, D4 disables the optional multiplexer, and D5–D7 select its channel.

## Capture a well

Camera and capture tools are in the left panel, the live image is in the centre,
and stimulation is in the right panel. Drag the dividers to allocate space.
Recording actions stay at the bottom of the Camera panel; Stop Stimulation
stays at the bottom of the Stimulation panel. Camera Settings opens
camera configuration, hardware setup, USB connection, and advanced properties.

Choose a well and set voltage, frequency, pulse width, and pre-pacing before
switching **Stimulation** on. New profiles default to 0.6 V, 1 Hz and 2 ms;
existing saved preferences are retained. **Setup → Output assignments…** declares
whether the multiplexer is fitted and assigns each connected well to an output.
The interface labels outputs 1–8; existing device indices and wiring assignments
are preserved. These assignments do not specify microscope coordinates.

Voltage, frequency, and pulse width are editable in their displayed boxes,
using the keyboard or up/down arrows. The switch sits between Stimulation off
and Stimulation on. Click Manual or Timed in the pre-pacing slider, drag its
transparent selection overlay, or use the Left/Right keys when it has focus.
Timed mode exposes the duration field. Settings remain locked during pacing.

**Manual** pre-pacing waits until **Begin Imaging** in Camera, which starts the
first recording. **Timed** requires a positive duration. During timed pre-pacing,
**Record After Pre-pacing** queues the first recording; cancelling that pending
recording leaves pacing running. Starting, Stopping, and Output unconfirmed are
not acknowledgments that the requested output state has been reached.

Pacing stays unchanged between fields. **Stop Recording** finishes one field;
switching stimulation off between fields stops output and completes that well.
Switching off during recording interrupts the field. **Stop Stimulation**
interrupts capture and prioritizes resting discharge. Leave stimulation off for
unstimulated capture.

Nano Scout estimates the current preview field in the background. **Count Field**
requests a fresh estimate. Healthy individual cells contribute to the target;
dead cells and groups are shown separately. A field contributes only after a
successful recording. Repeated identifiers and calibrated overlaps do not add
credit. Without stage coordinates, assign a new FOV identifier only after moving
to a new field. **History…** opens the full recording history.

**Setup → Stimulation guide…** provides workflow, counting, output-state, and
metadata guidance. **Adjust View → Real Scope Setup…** explains physical scope
settings, scale examples, trigger choices, and troubleshooting.

## Automatic field collection

Choose **Automatic Collection…** to set the collection area and cell target.
This action is available when TEMPO detects an XY stage.

1. Enter measured pixel size and camera-to-stage rotation/axis reversals in
   **Field Calibration…** within Automatic Collection, with the intended camera, objective and binning.
2. For each well, move the stage to two opposite corners of a rectangle entirely
   inside the usable well area and mark them in **Automatic Collection…**.
3. Enter the healthy-cell target, maximum number of fields to inspect, minimum
   healthy cells per recorded field, and recording duration (initially 15 s).
4. Choose the current well, all eligible wells, or a row-range preset, then start collection. Only calibrated, assigned wells qualify for stimulated collection.

TEMPO searches a serpentine grid with nonoverlapping camera footprints fully
inside the marked rectangle. Camera orientation is included in footprint size.
It waits after stage motion, clears stale frames, counts, and records qualifying
fields. A group alone does not qualify a field. It stops at the cell target or
reports a shortfall when the search area or field limit is exhausted.
**Collection Results…** retains the outcome for each well after the run.

A currently paced well is adopted without restarting its pre-pacing. Each well normally pre-paces once and stays paced through its recordings. A focus-review delay beyond 60 seconds stops pacing and requires repeated pre-pacing on resume; the affected field and later fields in the well remain flagged for possible viability and measurement-validity effects. See [focus review and recording quality](CAPTURE_QUALITY.md#focus-review-during-automatic-collection). Manual
pre-pacing requires **Begin Imaging** at each well. Output is stopped before
moving to the next well. Multiple stimulated wells require the multiplexer and
an explicit channel assignment for every well. A single output collects one
well at a time. Optical configuration changes require recalibration and new
area marks where their saved configuration no longer matches.

## Expected oscilloscope signal

**Expected Signal** is a simulation of the commanded output. **Adjust View…**
offers recommended pulse-detail and pulse-train views, editable time/div and
volts/div, and a button to restore recommended scales. Constant output and the
10 Hz diagnostic square wave use their own waveform. Cycling shows one
channel's effective frequency: the selected train frequency divided by eight.
At rest, the trace previews the pacing settings for the next session.

The preview fixes DC coupling, baseline reference, and appropriate triggering.
**Real Scope Setup…** explains how to reproduce the view on a physical scope,
including matching the actual probe attenuation. TEMPO does not configure or
read a scope. The simulator cannot predict loaded electrode voltage, transients,
noise, or measured pulse timing.

## Confirmation, stopping and data

V2 requests explicit confirmation above 0.8 V, 3 Hz, 10 ms, or 15% duty cycle.
These are inherited device confirmation thresholds, not validated biological
safety limits. Width must always be shorter than the period, and requested
voltage remains limited to 1 V. Constant output and scope tests are available
under **Diagnostics…**, outside active well sessions.

TEMPO sends heartbeats every 250 ms. The firmware returns to resting discharge
after two seconds without valid communication. Pulse edges and the watchdog are
serviced by an independent 20 µs timer. Reconnecting does not restart output.
An acknowledged resting state is distinguished from unconfirmed output.
Physical waveform, discharge, isolation, and connection-loss behavior still
require verification with the actual hardware and oscilloscope.

Recordings have sibling `.capture.json` documents. Well-session records retain
settings, timing, field estimates, positions and completion/interruption state.
Calibrated fields also produce the existing `.spatial.json` registration format.
Completed automatic recordings are queued after the well's final metadata is
saved. Processing retains capture provenance and adds stimulation columns to
cell results. Older recordings remain readable with unavailable capture data.
Host timestamps do not establish measured pulse-to-frame synchronization.
