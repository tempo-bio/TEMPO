# TEMPO Agent Guidelines & Project Rules

This document defines operational rules, architectural guidelines, and lifecycle protocols for all AI agents working on the **TEMPO** codebase.

---

## 1. Scratch Files & Artifact Lifecycle Protocol

To prevent IDE static analysis noise, false-positive import errors (e.g., `Cannot find module cv2`, `vk_math_engine`, `tempo_statistics`), and phantom errors in the IDE Problems pane:

### A. When to Delete Scratch Documents (Default Action)
* **Ephemeral Validation**: Scratch scripts (`<conversation>/scratch/*.py`, temporary shells, or one-off verification snippets) are strictly temporary tools used to execute an immediate test, verify a bug fix, or inspect data.
* **Immediate Cleanup**: Once the test has run and the results/findings have been confirmed, the agent **MUST delete the temporary scratch file before concluding the task**.
* **External Import Hazards**: Any scratch script located outside the workspace root (e.g., in `~/.gemini/.../scratch/`) will fail IDE static import resolution. Agents must never leave scripts in scratch directories that import workspace modules or environment-specific libraries (`cv2`, `torch`, `PyQt6`, `vk_math_engine`, `tempo_statistics`).

### B. When to Keep or Promote Scratch Documents
* **Regression & Unit Tests**: If a test script has ongoing value (e.g., verifying dual-amplitude normalization math, Savitzky-Golay filtering, or filename regex parsing), **do NOT leave it in a scratch directory**. 
  * Formally promote it into the project repository under a dedicated folder (e.g., `tests/test_kinematics_and_stats.py`).
  * Ensure import paths resolve cleanly relative to the workspace root without requiring hacky `sys.path` workarounds that confuse static linters.
* **Documentation of Findings**: Report task findings and validation results in chat. Keep optional local evidence in ignored `.agent-artifacts/`; update maintained documentation only when it provides reusable instructions or reference material. Do not create tracked walkthroughs, repair reports, audit journals, or per-task verification notes unless the user explicitly requests them.

### C. Git Hygiene Before Completing Work
* **Generated Artifacts**: Screenshots, geometry dumps, logs, benchmark outputs, execution records, temporary notebooks, local queues, annotation backups, and personal preferences must remain outside Git tracking. Use `.agent-artifacts/` for new validation outputs and confirm ignore rules before generating them. Keep explicitly maintained deliverables, such as `notebooks/TEMPO_Colab_Trainer.ipynb`, tracked and free of saved outputs or private values.
* **Existing Tracked Clutter**: Ignore rules do not remove files already tracked. Remove generated artifacts from the Git index while retaining local files; preserve research data, backups, preferences, and useful implementation changes. Check maintained documentation for references to reports removed from tracking.
* **Final Check**: Inspect staged changes, unstaged changes, and untracked files before finishing. Remove temporary validation scripts, keep meaningful regression tests and reusable fixtures, and verify that generated outputs are ignored. Do not append task history or test-run summaries to maintained documentation.
* **No Hidden Changes**: Never use blanket resets, destructive cleanup, `assume-unchanged`, or `skip-worktree` to make Git appear clean. Do not commit without explicit authorization. Genuine implementation changes remain visible until the user commits or discards them.

---

## 2. Environment & Python Interpreter Hygiene

* **Dual Environment Awareness**:
  * **`.venv_x86` (Rosetta 2 x86_64)**: Required for hardware acquisition via Micro-Manager drivers (`pymmcore`, `pymmcore-plus`). The GUI (`tempo_gui.py`) automatically relaunches under Rosetta on Apple Silicon.
  * **`.venv` (Native ARM64)**: Used for Apple Silicon native operations and PyTorch MPS acceleration (`train_yolo.py`).
* **Package Management**: Never run global `pip install`. All dependencies must reside within the respective virtual environment.
* **IDE Configuration**: Ensure `.vscode/settings.json` points to the primary project virtual environment (`${workspaceFolder}/.venv_x86/bin/python` or `${workspaceFolder}/.venv/bin/python`) with `${workspaceFolder}` included in `python.analysis.extraPaths`.

---

## 3. Core Architectural Principles

* **Kinematics Math Engine (`vk_math_engine.py`)**:
  * Maintain mathematical rigor. Differential signal extraction (`cv2.absdiff`) must use static baseline reference frames, not dynamic min-max rescaling.
  * Area-normalized amplitude calculations must account for the actual segmentation mask area rather than the bounding box.
* **Memory & Performance**:
  * Avoid full-frame bitmap allocation inside tight frame loops (e.g., in `masking_engine.py`). Crop bounding boxes before applying polygon masks.
* Never acquire high-speed camera frames directly on the GUI timer thread; use dedicated background worker threads.

---

## 4. Presentation Decisions and Visual Review

* Add UI text only when it answers a question the user needs answered at that point, helps them choose a value, or explains a necessary action. If a label or button already communicates it, omit the extra sentence.
* Do not narrate internal wiring, implementation work, permission checks, or routine synchronization in helper text. Avoid messages such as “Administrator verified” or “Open Notebook uses the shared value.” Show a permission restriction when it explains an unavailable action; show sync status when it affects whether work was saved or needs attention.
* Prefer deleting redundant copy to polishing or relocating it. Do not turn an unnecessary paragraph into a tooltip or information icon. Keep useful scientific definitions, units, recommendations, and instructions where the user needs them.
* Review added or changed copy in the rendered interface before finishing. Remove duplicate explanations and routine success messages. Preserve actionable errors, missing-data states, unsaved changes, partial saves, pending writes, and consequences that affect other collaborators.
* Reusable technical caveats belong in the shared `InformationButton` beside their feature, available on hover and keyboard activation. Do not repeat them as permanent paragraphs or automatic reminders. Keep instructions, recommendations, units, current errors, missing-data states, pending writes, and action consequences visible where needed.
* Align related forms across sections. Size controls from their text and font; give extra space to content rather than stretching buttons or metric rows. Prefer one stateful control for mutually exclusive actions, retaining actual completion/cancellation state.
* Evaluate each added label or control by what the user needs to do or understand at that point. Remove duplicate information and controls without a working purpose. Routine alignment and spacing decisions should not require user intervention.
* Inspect actual populated, empty, unavailable, and active-state renders at ordinary, wide, small-window, and larger-font sizes. Geometry tests must check visible text bounds, alignment, control visibility, and scrolling; passing tests alone do not establish visual acceptance.
* Reproduce the reported data schema, saved metric choice, selection state, and content volume. Modern synthetic columns cannot substitute for a historical CSV, an empty review cannot establish editing usability, and a two-entry queue cannot establish growth before scrolling. Inspect plot tick labels at the smallest rendered panel widths.
* Verify hover information by moving the pointer onto and away from the icon without clicking. A directly injected tooltip event does not establish hover behavior. Check icon bounds through their enclosing layouts; keep information icons out of clipped tab corners. File-picker fields and buttons share a height and baseline.
* Use bounded, isolated offscreen fixtures for automated GUI review. Do not automatically launch native desktop TEMPO instances. Native review uses one user-opened instance with the agreed exit check; retain shutdown protections and keep simulated evidence distinct from hardware verification.

### Control Semantics, Information Hierarchy, and Review

* Before choosing or materially changing a layout or visual design, present a small set of actual rendered alternatives and let the user select a direction. Show comparable screenshots with the same representative content, window size, and operating state; explain the practical tradeoffs briefly. This applies to styling, hierarchy, density, and control presentation as well as panel arrangement. Keep alternatives in isolated fixtures until the user chooses; do not silently adopt your preferred design.
* Choose controls by behavior: slider switches request immediate binary on/off changes; checkboxes select independent options or values committed with a form; action buttons initiate commands. Use one stateful action for mutually exclusive start/stop commands. Do not replace every checkbox with a switch. See [Microsoft switches](https://learn.microsoft.com/en-us/windows/apps/develop/ui/controls/toggles), [checkboxes](https://learn.microsoft.com/en-us/windows/apps/develop/ui/controls/checkbox), and [buttons](https://learn.microsoft.com/en-us/windows/apps/develop/ui/controls/buttons).
* Separate a hardware request from its acknowledged state. Show Starting, Stopping, rejected requests, and unconfirmed output explicitly; a switch animation does not establish that hardware changed state. Keep stopping reachable while a request is pending. Switch labels remain stable, with state explained separately. See [GNOME switch states](https://developer.gnome.org/hig/patterns/controls/switches.html) and [W3C switch semantics](https://www.w3.org/WAI/ARIA/apg/patterns/switch/).
* Preserve the user's chosen panel ownership across sizes and operating states. Acquisition has exactly two resizable side panels: Camera/capture on the left, Stimulation on the right, and imaging in the center. Camera settings and recording controls do not migrate beneath the image; stimulation connection and settings do not migrate into Camera. When replacing an interface, account for every existing capability and saved setting. Reduce clutter through clear secondary views, not deletion. See [Apple layout guidance](https://developer.apple.com/design/human-interface-guidelines/layout) and [NN/g complex-application design](https://www.nngroup.com/articles/complex-application-design/).
* Use three levels of guidance: essential instructions, units, errors, and action consequences remain visible; focused scientific definitions and caveats use the shared InformationButton; additional-information windows provide substantial procedures, examples, interpretation, and troubleshooting. Concise tooltips do not require terse helper windows. Preserve useful material removed from crowded information boxes in the relevant guide, while deleting genuinely redundant copy. Do not hide information most users need to complete the current action. See [Apple contextual help](https://developer.apple.com/design/human-interface-guidelines/offering-help) and [GOV.UK disclosure guidance](https://design-system.service.gov.uk/components/details/).
* Information controls must work on click, real pointer hover, and keyboard activation, retaining keyboard focus and following the shared focus-neutral appearance. Hover content remains readable when the pointer moves into it and can be dismissed with Escape. Do not add disconnected letters, redundant help icons, or scroll-dependent instructions. See [W3C hover and focus guidance](https://www.w3.org/WAI/WCAG22/Understanding/content-on-hover-or-focus.html).
* All controls, including custom-painted switches, sliders, and information buttons, must follow `FocusNeutralStyle`: do not add a focus outline, glow, or persistent click highlight. Preserve keyboard navigation and activation without painting an extra focus state. Keep actual selection, on/off state, and the pre-pacing selection overlay visible; these communicate values rather than focus. Verify appearance after a real click and pointer departure, then after keyboard focus changes.
* Allocate space by task priority: an expected-signal plot must remain readable before expanding history. Empty lists use compact contextual states rather than large blank frames, and recent history has bounded height with access to the complete record. Check empty and populated geometry separately. See [Carbon empty states](https://www.carbondesignsystem.com/building-blocks/core/patterns/empty-states).
* Informational and live-adjustment windows use native dismissal and standard keyboard shortcuts, without redundant Close buttons. Keep Save/Apply/Cancel when they describe actual commit or cancellation behavior; retain firmware-upload close guards during flashing.
* Visual acceptance must include meaningful populated camera properties, controls in their correct panels, readable waveform divisions and scale labels, empty and long histories, real help interaction, and reachable recording/stopping actions. Check ordinary, wide, small-window, and larger-font layouts. Passing geometry assertions is necessary evidence, not visual approval.
