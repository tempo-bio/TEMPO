Stimulation Investigation 1 — optional package for TEMPO v1.0.1 (Windows x64)

1. Close TEMPO and extract the complete ZIP into a folder.
2. Double-click Install.cmd. If prompted, enter your TEMPO installation folder.
3. Reopen TEMPO and select Stimulation Investigation 1 in Acquisition.

The package reuses your installed v1.0.1 libraries. It backs up the application
files it replaces. Research data, preferences, credentials, and models are kept.
To restore the original application files, close TEMPO and run Remove.cmd.
Remove this package before uninstalling or upgrading the base TEMPO application.

For a custom installation folder, use Command Prompt:
Install.cmd -TempoDirectory "D:\Applications\TEMPO"
Remove.cmd -TempoDirectory "D:\Applications\TEMPO"

GUIDE.md explains data collection. Guide… in TEMPO opens the same instructions.
Scope records and Arduino records are independent. Live Arduino recording needs
the development firmware, installed separately through the Arduino tab.
This is an experimental data-collection package; no calibration or automatic
output adjustment is included. Physical measurement accuracy is not established.
