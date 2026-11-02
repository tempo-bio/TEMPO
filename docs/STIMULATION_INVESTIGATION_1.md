# Stimulation Investigation 1

Stimulation Investigation 1 collects experimental conditions, manually entered
oscilloscope readings, and raw Arduino readings. It is an Acquisition mode in
TEMPO's v1.0.1 interface. The Stimulation panel remains available on the right;
the investigation replaces the Camera panel and image preview.

The package performs no voltage conversion, channel subtraction, electric-field
calculation, calibration fitting, or automatic stimulation adjustment. Expected
Signal remains a simulation of the commanded output.

## Install the optional download

Install the stable TEMPO v1.0.1 Windows application first. Download the separate
Stimulation Investigation 1 ZIP from GitHub Packages and extract the complete
archive. Close TEMPO, then double-click **Install.cmd**. The installer verifies
the existing application, backs up the affected files, and reuses its installed
runtime libraries. Reopen TEMPO to access the investigation mode.

Research data, settings, credentials, and models are retained. To restore the
original application files, close TEMPO and run **Remove.cmd** from the extracted
package. Remove the package before upgrading or uninstalling the base application.
For a custom application folder, see README.txt in the download.

## Start an investigation

1. Finish imaging and stop the camera feed and stimulation. Close any active
   stimulation diagnostic before changing modes.
2. In Acquisition, select **Stimulation Investigation 1**.
3. Choose **Add condition**. Enter a name, well, medium, and fill volume in µL.
4. Describe the stimulation electrodes and sensing wires. Material and
   dimensions are free response. Describe positions relative to the well edge
   and, for the sensing wires, the stimulation electrodes. If the two members
   differ, identify each in the same description, for example `Left: …; Right: …`.
5. Record the stimulation settings used for that condition. **Copy settings**
   copies the connected Arduino's acknowledged settings into this
   condition; it does not apply settings to the stimulator.
6. Choose **Save investigation** and a JSON filename in your research-data folder.

Use **Duplicate** to start a condition with the same setup descriptions and new
measurement records. Duplication does not copy existing instrument records.
Immersion depth and temperature are not collected.

The Stimulation switch, voltage, frequency, pulse width, connection, Expected
Signal, and Stop Stimulation stay in the right panel. Investigation mode does
not need a camera or an imaging well session. For a fitted multiplexer, choose
the output in this panel. Single-output configurations use output 1. Pre-pacing
is an imaging workflow and is hidden in investigation mode.

Switching back to Imaging retains the investigation draft and restores the
camera layout. Stop stimulation and finish or cancel raw capture first.

## Oscilloscope records

In **Oscilloscope**, choose **Add record**. Enter the instrument/probe, measurement
window, readings, scope settings, and notes that you actually observed. All
measurement fields are optional and preserve the entered text. **Scope difference**
is a value read from the oscilloscope; TEMPO does not subtract the channels.
**Attach file…** copies a chosen trace, image, or other file beside the investigation.
Activate an attachment with Enter or a double click to open it with the system's
associated application.

Oscilloscope records do not require Arduino records. You can enter readings
without connecting an Arduino or exporting traces from the oscilloscope.

Choose **Save investigation** to save manual edits. There is no separate record
commit: the investigation saves conditions and instrument records together.

## Live Arduino records

### Prepare the development firmware and inputs

The normal v1.0.1 stimulation firmware does not provide analog acquisition.
In the **Arduino** tab, choose **Install development firmware…**. Select the Uno R4
Minima port and explicitly start installation. This replaces its sketch. Finish
stimulation, imaging, and raw capture before uploading, and keep USB connected
until verification finishes. Reconnect using the existing Stimulation connection.

The development variant is built from the maintained pacing sketch with
`TEMPO_INVESTIGATION_1` enabled. The ordinary firmware installer still installs
the ordinary sketch. The development variant retains the v1.0.1 stimulation
protocol and adds bounded A1/A2 recording through the same serial connection.
No second serial program should open that port.

The two recording inputs are **A1** and **A2**. A0 remains the stimulation DAC.
Before attaching sensing wires, verify each wire's voltage relative to Arduino
ground through resting, pulsing, and switching states against the board's ADC
input limits. A small voltage difference between wires does not establish that
either input is within range. Confirm the actual wiring and reference with the
scope before direct connection. The package does not establish input protection,
source-impedance compatibility, or the accuracy of direct sensing in the medium.

### Record

1. Select the condition and the **Arduino** tab.
2. Choose 1–64 raw readings, a requested interval of 100–1,000,000 µs, and a trigger.
   The inputs alternate A1, A2, A1, A2. The interval is between successive readings,
   not between simultaneous pairs. **Immediate** also works with stimulation off.
   **Next pulse** waits for a new pulse in an already running pulse train.
3. If a pulse train is required, start it with the existing Stimulation switch.
4. Choose **Record Arduino**. Save the investigation first if prompted.
5. The same button becomes **Cancel capture** while acquisition is pending.

Starting or cancelling raw recording does not start or stop stimulation.
**Stop Stimulation** remains reachable and also cancels device acquisition.
Complete and cancelled captures retain their available readings. USB errors
retain already received pages and mark the capture interrupted. Capture has a
bounded host timeout; missing replies are not interpreted as successful output
or successful acquisition.

Arduino captures save raw 14-bit ADC counts, conversion start/end timestamps,
input identity, stimulation mode/phase/output around conversion, the requested
capture configuration, firmware, port, and stimulation settings at the start and
end. Timestamps are the device's unsigned 32-bit microsecond values and can wrap.
The inputs are sequential. Actual timing is retained; requested sampling
intervals and capture of short pulses are not guaranteed. No voltage calibration
or timing analysis is performed. The table shows selected columns; the saved
JSON retains all fields.

Arduino records do not require oscilloscope records. Add capture notes separately.
Raw capture saves its initial record and every received page, along with any
current investigation edits. A failed save remains visible as unsaved work.

## Preserve and move data

Each instrument record retains the condition as it was when the record was
created. Later condition edits do not rewrite that snapshot. **Recorded setup…**
shows the original context. A newly added scope record uses the current condition;
for historical measurements, enter the appropriate condition before adding it.

The saved JSON and its neighboring `<filename>.files` directory belong together.
**Export…** creates a ZIP containing `investigation.json` and copied attachments.
It includes current in-memory edits and can preserve work after a save conflict.
Extract the complete archive and open its JSON to continue elsewhere. Export
does not replace an existing archive. The application also refuses to silently
overwrite an investigation changed by another process.

When opening a file left during raw acquisition, TEMPO marks that unfinished
record interrupted and retains its received samples. Unsaved manual changes
require Save, Discard, or Cancel when closing TEMPO.

## Development validation

Run collection and protocol tests with the project's Python environment:

```sh
python -m unittest tests.test_stimulation_investigation tests.test_stimulation_investigation_ui tests.test_stimulation tests.test_pacer_firmware tests.test_firmware_setup
python -m scripts.render_stimulation_investigation_screens
```

The renderer uses isolated offscreen fixtures and simulated data. It does not
open native TEMPO or connect hardware. Host C++ tests exercise the maintained
sketch and the opt-in variant against a simulated ADC/timer/serial fixture.
Physical ADC loading, waveform timing, input compatibility, and measurement
accuracy require the actual Arduino, sensing fixture, and oscilloscope.
