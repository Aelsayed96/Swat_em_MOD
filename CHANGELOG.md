# Changelog

## v0.11.0 — 2026-10-30
### Changed (simplified naming / options)
- Removed the "Auto-append ANSYS copy index" checkbox: **both rules are now used exactly as written**, nothing is appended. Use `{ci}` for the ANSYS copy token (`_0`, `_0_1`, `_0_2`).
- Removed the Coil-name preset list and the Assignment-object preset list and the "Copy offset" box.
- Default **Coil name = `{phase}_{layer}_{coil}`** (e.g. `A_1_1`). Default Assignment object = `Layer{layer}{ci}`.
- New toggle button **Coil no. from 1 / from 0**: sets where `{coil}` starts. `{copy}` / `{ci}` always follow the ANSYS copy index (start at 0).
- **Model symmetry is one list**: Full machine, 1/N (even), 1/2N (odd) — only the options valid for the winding are shown. The separate Even/Odd box is gone.
- **Target EMF is a CW / CCW toggle button** (no more drop-down, independent of the sequence-direction toggle).
### Fixed
- Re-applying failed with "Failed to create boundary Coil …" because the coils/windings from the previous run already existed. New option **Replace existing boundaries with the same name** (default ON): same-named boundaries are deleted first, then re-created (also in the generated script). With it OFF the log lists the clashing names.
- The failure hint now says: name already exists, or the object is not a valid coil face/sheet.
### Tests
- Updated `tests/test_ansys_assignment_rule.py`.

## v0.10.1 — 2026-10-30
### Fixed
- **"Specified design is not of type Maxwell 3D"** when a 2D design was selected: the exporter always created a `Maxwell3d` object. It now follows the *design type* read from AEDT (Maxwell 2D → `Maxwell2d`, Maxwell 3D → `Maxwell3d`); if the type is not known yet it tries 3D, then 2D.
- The Solver Type box no longer forces a solution type on an existing design (that caused the Transient / TransientXY mismatch). It is only used when a new design has to be created. Added `TransientZ`, `EddyCurrentXY/Z`, `MagnetostaticXY/Z` entries.
- Generated script uses `Maxwell2d` / `Maxwell3d` to match the design and no longer sets `solution_type` for an existing design.
### Added
- Pre-flight check before assigning coils: lists assignment objects that do not exist in the design (with close-name suggestions and the objects starting with `Layer`) and skips those rows instead of failing with "Failed to create boundary CoilTerminal".
- Error hints: Maxwell 3D target is a *solid* (needs a sheet/face), or the boundary name already exists.

## v0.10.0 — 2026-10-30
### Added
- **Full control of the assignment rule** (`assign_coil(assignment=[...])`). "Assignment object" is now the *complete* rule, not just a base name. New placeholders: `{ci}` (ANSYS copy token `_0` / `_0_1` / `_0_2`), `{copy}`, `{coil}` (= copy+1), `{slot}`, `{phase}`, `{layer}`, `{n}`, `{direction}`, `{polarity}`, `{name}`.
- Rule preset drop-down (`Layer{layer}{ci}`, `Layer{layer}_0_{copy}`, `Layer{layer}_{slot}` …).
- **Copy offset** spin box (shifts `{copy}`, `{coil}`, `{ci}`).
- **Live "Result" preview** of the first 3 copies per layer (e.g. `L1: Layer1_0 · Layer1_0_1 · Layer1_0_2`), updated while typing; shows a red error for unknown placeholders.
### Changed
- The naming checkbox is now "Auto-append ANSYS copy index". ON (default): `Layer{layer}` → `Layer1_0`, `Layer1_0_1` … (same as before). OFF: the rule is used exactly as written. If the rule already contains `{ci}`, `{copy}` or `{coil}`, nothing is appended twice.
- Bad templates no longer silently fall back to `Layer<n>`; the target is left empty (row is skipped with a warning at Apply) and the preview shows the error.
- Table "Assignment" cells remain individually editable and are not overwritten except by "Apply naming" / rule edits.
### Tests
- `tests/test_ansys_assignment_rule.py`.

## v0.9.0 — 2026-10-30
### Changed
- **Base name no longer affects the assignment.** "Base name" is now "Coil name" and only sets the coil boundary name (`assign_coil(name=...)`). Default `Layer{layer}_{coil}`; presets in the drop-down. `{coil}` = coil number in its layer (copy index + 1). Names are made unique automatically.
- The geometry object the coil is assigned to (`assign_coil(assignment=...)`) has its own field, "Assignment object" (default `Layer{layer}`, ANSYS copy-index naming: `_0`, `_0_1`, ...). "Apply naming to all rows" sets both columns.
### Added
- Symmetry type **Even (periodic) / Odd (anti-periodic)** next to Model symmetry. Even: 1/N model (slots 1..Q/N). Odd: 1/(2N) model (slots 1..Q/(2N)), e.g. N=2 + Odd = quarter model. Odd is only offered for N where the winding repeats with inverted polarity (checked from the winding data).

## v0.8.0 — 2026-10-30
### Changed
- Removed "Launch new session" and "Non-graphical" options (always attaches to running AEDT with GUI).
- Removed the Suffix field (names are now <base>_0, <base>_0_1 …, no _Section1).
- Layer labels are a user input, default 1,2,3 (e.g. [blank],RE).
### Added
- Model symmetry selector (Full / 1/2 / 1/4 … from winding periodicity t): only coil sides of the modelled sector are exported, copy indices renumbered.
- "Turns / coil side" column in Per-Phase Winding: sets turns for all coil sides of that phase and the Turns_<phase>_L<layer> design variables.

## v0.7.2 — 2026-10-30
### Added
- Ansys exporter: Project / Design are now editable drop-down lists filled from the running AEDT (read in a worker thread, so they no longer stay blank); ⟳ refreshes.
- Solver Type is auto-detected from the connected design and follows the selected design.
- Layer-label popup next to Base name: `{layer}` can expand to custom labels (e.g. [blank],RE / A,B,C / T,B) instead of 1,2,3; `{n}` keeps the number. Naming re-applies instantly.
- Sequence direction is now a CCW/CW toggle button; Target EMF sits in the same row and follows the toggle.

## v0.7.1 — 2026-10-30
### Fixed
- Startup crash `KeyError: 'tab_flux_barrier'`: unknown/stale tab names in the saved view config (written by the earlier combined build) are now ignored in `plot_tabs_add` and `plot_tabs_update_menu`.
- Verified headless with a config containing stale tab names.

## v0.7.0 — standalone SWAT-EM
- Split into three apps; SWAT-EM keeps Ansys winding exporter (F6) and winding comparison (F7). Flux-barrier and Maxwell optimization removed (now separate apps).
