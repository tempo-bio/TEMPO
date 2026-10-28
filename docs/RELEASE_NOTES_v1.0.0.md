# TEMPO v1.0.0 — unreleased

First Windows x64 release of TEMPO, a desktop application for microscopy acquisition and cell-motion analysis.

- Annotate cells and nuclei, save drafts, and submit images for review.
- Process recordings, inspect contraction traces and measurements, and export results.
- Control camera acquisition, XY-stage collection, and AU26 V2 stimulation.
- Train detection and segmentation models in Colab using reviewed annotations.

## Install

Download **TEMPO_Setup_v1.0.0.exe**. Python and the application libraries are included. Shared annotation and model downloads require access to a Hugging Face workspace. Camera acquisition requires compatible vendor drivers and Micro-Manager adapters.

**The installer is unsigned.** Windows cannot verify TEMPO’s publisher from a digital signature and may display **Unknown publisher** or **Windows protected your PC**. Smart App Control or an organization’s device policy may block installation.

Download only from this project’s release page and compare the installer’s SHA-256 with **SHA256SUMS.txt**. The checksum can detect a changed download. It cannot verify who published the file or whether it is safe. Do not disable Windows security protections to install TEMPO. On a managed computer, ask IT if installation is blocked. [Installation instructions](../README.md#install-on-windows).

## Before collecting data

Validate camera timing, stage calibration, and stimulator output on your hardware before experimental use. Focus assistance is Beta and requires a calibrated profile; none is included with this release.

TEMPO is licensed under AGPL-3.0-only. Dependencies retain their included licenses and notices.
