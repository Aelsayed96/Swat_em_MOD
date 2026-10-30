# -*- coding: utf-8 -*-
"""
ANSYS Maxwell – Winding Export Dialog  (SWAT-EM)
================================================
Uses ansys.aedt.core (pyaedt ≥ 0.8 / ansys-aedt-core ≥ 1.x).

New in this version
───────────────────
• Fixed: correct 3-step assignment:
    1. assign_coil()   – one per coil side (object name or face ID)
    2. assign_winding() – per phase, NO assignment= arg
    3. add_winding_coils() – link coil names into each winding
  The old code passed coil *names* as assignment= on assign_winding which
  triggered internal auto-coil creation on non-existent objects → fail.
• Fixed: winding args are plain floats, not "1A" / "0ohm" strings.
• Fixed: save_project() only called when at least one thing succeeded;
  wrapped in try/except so gRPC errors don't crash the whole apply.
• Stop button – sets _stop flag checked between every step.
• Step delay – configurable ms pause between coil assignments (lets ANSYS
  breathe; reduces gRPC timeout errors on slow machines).
• Resume – remembers last completed step index; "Resume" starts from there.
  Connecting (or plain Apply) always restarts the whole sequence from step
  0 and drops any earlier checkpoint; only the dedicated Resume button
  continues from where a previous run was stopped.
• Per-phase/layer design variables: one AEDT design variable per
  (phase, layer) combination, e.g. Turns_A_L1, is created first and every
  coil terminal that shares that phase/layer has its conductors_number
  bound to that variable (instead of a hardcoded number) — change the
  variable once in AEDT and every coil side sharing it updates together.
• Per-phase winding table – winding_type, is_solid (defaults to
  Stranded — correct for round-wire multi-turn coils, still overridable
  per phase), current, resistance, inductance, voltage, parallel_branches.
• Script preview is live-editable; editing it does NOT push back to table.
  Apply button asks "Script or Table?" so you can act on either.
• Copy-index column added (_C_COPY = 6, _C_TARGET = 7).
• ANSYS copy-index naming helper retained.
"""

import time
import traceback
from collections import defaultdict
from datetime import datetime

from qtpy.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QLineEdit,
    QComboBox, QCheckBox, QPushButton, QTextEdit, QFileDialog, QMessageBox,
    QTableWidget, QTableWidgetItem, QSplitter, QWidget, QFormLayout,
    QHeaderView, QAbstractItemView, QDoubleSpinBox, QSpinBox,
)
from qtpy.QtCore import Qt, QThread, Signal, QObject
from qtpy.QtGui import QFont, QColor, QTextCharFormat, QTextCursor


# ─────────────────────────────────────────────────────────────────────────────
# Colour-coded terminal logger  (thread-safe via Qt signals)
# ─────────────────────────────────────────────────────────────────────────────

class _TerminalLogger(QObject):
    _sig = Signal(str, str)   # (message, level)
    _COLOURS = {
        "INFO":  "#d4d4d4",
        "OK":    "#6dcf81",
        "WARN":  "#e5c07b",
        "ERROR": "#e06c75",
        "HEAD":  "#61afef",
        "SKIP":  "#c678dd",
    }

    def __init__(self, te: QTextEdit):
        super().__init__()
        self._te = te
        self._sig.connect(self._append)

    def _append(self, msg: str, level: str):
        ts  = datetime.now().strftime("%H:%M:%S")
        cur = self._te.textCursor()
        cur.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self._COLOURS.get(level, "#d4d4d4")))
        cur.setCharFormat(fmt)
        cur.insertText(f"[{ts}]  {msg}\n")
        self._te.setTextCursor(cur)
        self._te.ensureCursorVisible()

    def info(self,  m): self._sig.emit(m, "INFO")
    def ok(self,    m): self._sig.emit(m, "OK")
    def warn(self,  m): self._sig.emit(m, "WARN")
    def error(self, m): self._sig.emit(m, "ERROR")
    def head(self,  m): self._sig.emit(m, "HEAD")
    def skip(self,  m): self._sig.emit(m, "SKIP")
    def dump(self, text: str, level="INFO"):
        for line in text.splitlines():
            self._sig.emit(line, level)


# ─────────────────────────────────────────────────────────────────────────────
# Stoppable background worker
# ─────────────────────────────────────────────────────────────────────────────

class _Worker(QThread):
    finished   = Signal(bool, str)   # (success, message)
    checkpoint = Signal(int)         # last completed step index

    def __init__(self, fn, kwargs: dict):
        super().__init__()
        self._fn      = fn
        self._kwargs  = kwargs
        self._stop    = False          # set by caller to request stop

    def request_stop(self):
        self._stop = True

    def run(self):
        try:
            msg = self._fn(worker=self, **self._kwargs)
            self.finished.emit(True, msg or "")
        except _StopRequested as e:
            self.finished.emit(False, f"STOPPED at step {e.step}")
        except Exception:
            self.finished.emit(False, traceback.format_exc())


class _StopRequested(Exception):
    def __init__(self, step): self.step = step


# ─────────────────────────────────────────────────────────────────────────────
# ANSYS copy-index naming
# ─────────────────────────────────────────────────────────────────────────────

def _ansys_copy_name(base: str, copy_index: int, suffix: str = "_Section1") -> str:
    """
    copy 0  →  <base>_0<suffix>           e.g. Layer1_0_Section1
    copy 1  →  <base>_0_1<suffix>         e.g. Layer1_0_1_Section1
    copy N  →  <base>_0_N<suffix>
    """
    middle = "_0" if copy_index == 0 else f"_0_{copy_index}"
    return f"{base}{middle}{suffix}"


_ASSIGN_TOKENS = ("layer", "n", "slot", "copy", "coil", "phase",
                  "direction", "polarity", "name", "ci")


def _copy_token(copy_index: int) -> str:
    """ANSYS copy-index token: 0 -> '_0', N -> '_0_N'."""
    return "_0" if int(copy_index) == 0 else f"_0_{int(copy_index)}"


def _format_assignment(template: str, *, layer, n, slot, copy, phase,
                       direction, polarity, name, coil_start: int = 1):
    """
    Build a name from a template (assignment object, and the coil name).
    The template is the COMPLETE rule - nothing is added automatically.
    Placeholders:
      {layer} label   {n} layer number   {slot}   {phase}   {name}
      {direction} +/-   {polarity} Positive/Negative
      {copy}  ANSYS copy index 0,1,2 ...  (always starts at 0)
      {coil}  coil number inside its layer, starts at `coil_start` (0 or 1)
      {ci}    ANSYS copy token:  copy 0 -> _0 , copy 1 -> _0_1 , copy 2 -> _0_2
    Returns (text, error).  error is "" on success, else a message (text is "").
    """
    c = int(copy)
    tmpl = (template or "").strip()
    if not tmpl:
        return "", "empty template"
    try:
        out = tmpl.format(layer=layer, n=n, slot=slot, copy=c,
                          coil=c + int(coil_start), phase=phase,
                          direction=direction, polarity=polarity,
                          name=name, ci=_copy_token(c))
    except KeyError as exc:
        return "", f"unknown placeholder {{{exc.args[0]}}}"
    except (ValueError, IndexError) as exc:
        return "", f"bad template ({exc})"
    out = out.strip()
    if not out:
        return "", "template produced an empty name"
    return out, ""


def _is_antiperiodic(phases, Q: int, shift: int) -> bool:
    """
    True if shifting the whole winding by `shift` slots reproduces it with
    every coil-side polarity inverted (same phase, same layer).
    phases[ph][layer] = list of signed slot numbers (as SWAT-EM returns them).
    This is the condition for an ODD (anti-periodic) symmetry boundary.
    """
    if Q <= 0 or shift <= 0:
        return False
    for ph_slots in phases:
        for layer_slots in ph_slots:
            have = {int(x) for x in layer_slots}
            moved = set()
            for x in have:
                sgn = 1 if x > 0 else -1
                new_abs = (abs(x) - 1 + shift) % Q + 1
                moved.add(-sgn * new_abs)
            if moved != have:
                return False
    return True


def _sector_slot_count(Q: int, n: int, odd: bool) -> int:
    """Slots inside the modelled sector: Q/n (even/periodic) or Q/(2n) (odd)."""
    return Q // (n * (2 if odd else 1))


# ─────────────────────────────────────────────────────────────────────────────
# pyaedt connect
# ─────────────────────────────────────────────────────────────────────────────

def _is_2d_type(design_type) -> bool:
    return "2D" in str(design_type or "").upper()


def _pyaedt_connect(project_name, design_name, solver_type,
                    new_desktop, non_graphical, log,
                    aedt_version=None, design_types=None):
    """
    Attach to a Maxwell design.  The pyaedt class follows the DESIGN type
    (Maxwell 2D -> Maxwell2d, Maxwell 3D -> Maxwell3d), never the solver combo.
    design_types : {design_name: "Maxwell 2D" | "Maxwell 3D"} read from AEDT
                   (may be empty on the very first connect -> try both).
    The solver type is only passed to pyaedt when a NEW design must be created;
    for an existing design AEDT keeps its own solution type (passing a
    mismatching one is what made 2D designs fail with
    "Specified design is not of type Maxwell 3D").
    """
    try:
        import ansys.aedt.core
    except ImportError:
        raise RuntimeError(
            "pyaedt / ansys-aedt-core is not installed.\n"
            "Run:  pip install pyaedt\n"
            "Then restart SWAT-EM."
        )
    from ansys.aedt.core import Maxwell2d, Maxwell3d
    design_types = design_types or {}
    if aedt_version:
        log.info(f"AEDT version requested: {aedt_version}")
    else:
        log.info("AEDT version: auto (latest installed)")

    known = design_types.get(design_name) if design_name else None
    if known:
        order = [Maxwell2d] if _is_2d_type(known) else [Maxwell3d]
        log.info(f"Design '{design_name}' is a {known} design.")
    else:
        order = [Maxwell3d, Maxwell2d]          # unknown: try both
    creating_new = bool(design_name and design_types and design_name not in design_types)

    log.info(f"{'Launching' if new_desktop else 'Attaching to'} ANSYS Maxwell …")
    last_exc = None
    for cls in order:
        kw = dict(
            project       = project_name,
            design        = design_name,
            new_desktop   = new_desktop,
            non_graphical = non_graphical,
            close_on_exit = False,
        )
        if creating_new:
            kw["solution_type"] = solver_type
        if aedt_version:
            kw["version"] = aedt_version
        try:
            mx = cls(**kw)
            if mx is False or mx is None:
                raise RuntimeError(f"{cls.__name__} returned False")
            log.ok(f"Connected ({cls.__name__}) — AEDT {mx.aedt_version_id}  |  "
                   f"project: {mx.project_name}  |  design: {mx.design_name}")
            return mx
        except Exception as exc:
            last_exc = exc
            if len(order) > 1 and "not of type" in str(exc):
                log.info(f"  {cls.__name__}: design is not of this type — trying the other one …")
                continue
            raise
    raise last_exc if last_exc else RuntimeError("Could not connect to Maxwell")


def _gather_info(mx) -> dict:
    """Read project/design/solver info from a connected Maxwell (call in a worker thread)."""
    info = dict(projects=[], designs=[], project="", design="", solver="", design_solvers={},
                design_types={})
    try: info["project"] = mx.project_name or ""
    except Exception: pass
    try: info["design"] = mx.design_name or ""
    except Exception: pass
    try: info["projects"] = list(mx.odesktop.GetProjectList())
    except Exception: pass
    try: info["designs"] = list(mx.design_list)
    except Exception: pass
    try: info["solver"] = str(mx.solution_type)
    except Exception: pass
    if info["solver"] and info["design"]:
        info["design_solvers"][info["design"]] = info["solver"]
    try:
        info["design_types"][info["design"]] = str(mx.design_type)
    except Exception:
        pass
    # solver type of every design in the active project
    try:
        oproj = mx.odesktop.GetActiveProject()
        for d in info["designs"]:
            try:
                st = oproj.GetDesign(d).GetSolutionType()
                if st:
                    info["design_solvers"][d] = str(st).replace(" ", "")
            except Exception:
                pass
            try:
                dt = oproj.GetDesign(d).GetDesignType()
                if dt:
                    info["design_types"][d] = str(dt)
            except Exception:
                pass
    except Exception:
        pass
    return info


# ─────────────────────────────────────────────────────────────────────────────
# pyaedt apply  (correct 3-step pattern, stoppable, with delay)
# ─────────────────────────────────────────────────────────────────────────────

def _phase_layer_var_name(phase, layer):
    """Design-variable name used for the conductor (turns) count of one
    phase/layer combination, e.g. Turns_A_L1."""
    return f"Turns_{phase}_L{layer}"


def _build_phase_layer_vars(coil_sides):
    """
    Collect one turns value per (phase, layer) combination.

    If coil sides in the same phase/layer disagree on turns (shouldn't
    normally happen in a symmetric winding), the first value encountered
    wins and a note is returned so the caller can warn about it.
    """
    values = {}
    conflicts = []
    for cs in coil_sides:
        key = (cs["phase"], cs["layer"])
        if key not in values:
            values[key] = cs["turns"]
        elif values[key] != cs["turns"] and key not in conflicts:
            conflicts.append(key)
    return values, conflicts


def _pyaedt_apply(maxwell, coil_sides: list, phase_settings: dict,
                  delay_ms: int, resume_from: int, log, worker,
                  closed_coil: bool = False,
                  sequence_cw: bool = False,
                  replace_existing: bool = True) -> str:
    """
    Step-by-step winding assignment.

    Step layout
    ───────────
    Steps 0 … V-1        : create one design variable per (phase, layer),
                            Turns_<phase>_L<layer>, holding the conductor
                            (turns) count for that phase/layer
    Steps V … V+N-1       : assign_coil for each coil side (conductors_number
                            is bound to its phase/layer variable, not a
                            hardcoded number)
    Steps V+N … V+N+P-1   : assign_winding  (one per phase, always Stranded)
    Steps V+N+P … …       : add_winding_coils (one per phase)
    Final step             : save_project

    worker._stop is checked before every step.
    delay_ms is inserted between steps.
    resume_from skips steps < resume_from.
    """

    # ── Closed-coil filtering ────────────────────────────────────────────────
    # In closed-coil mode only layer 1 is used and only the first coil
    # terminal per slot (i.e. one terminal per coil, not two) is needed.
    # Maxwell automatically closes the coil — no return-path terminal required.
    if closed_coil:
        layer1_slots_seen = set()
        filtered = []
        skipped  = []
        for cs in coil_sides:
            if cs["layer"] != 1:
                skipped.append(cs["name"])
                continue
            slot_key = (cs["phase"], cs["slot"])
            if slot_key in layer1_slots_seen:
                skipped.append(cs["name"])
                continue
            layer1_slots_seen.add(slot_key)
            filtered.append(cs)
        if skipped:
            log.warn(
                f"  Closed-coil mode: {len(skipped)} coil side(s) excluded "
                f"(layer 2 or duplicate slot terminal):"
            )
            for nm in skipped:
                log.skip(f"    skipped: {nm}")
        coil_sides = filtered
        log.info(
            f"  Closed-coil mode ON — using {len(coil_sides)} terminal(s) "
            f"(layer 1 only, one per slot)."
        )

    if sequence_cw:
        log.info("  CW direction: inverting all coil polarities.")
        coil_sides = [
            {**cs, "direction": ("-" if cs["direction"] == "+" else "+")}
            for cs in coil_sides
        ]

    applied_coils: dict = {}   # coil_name → phase
    coil_objs:     dict = {}   # coil_name → BoundaryObject
    winding_objs:  dict = {}   # phase     → BoundaryObject
    errors = []

    n_coils = len(coil_sides)

    def _check_stop(step_idx):
        if worker._stop:
            log.warn(f"Stop requested — halting before step {step_idx}.")
            worker.checkpoint.emit(step_idx)
            raise _StopRequested(step_idx)

    def _delay():
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

    # ── Phase 0: per-phase/layer design variables ────────────────────────────
    log.head("── Step 1/4  Creating phase/layer design variables ──")

    var_values, var_conflicts = _build_phase_layer_vars(coil_sides)
    var_keys = list(var_values.keys())          # [(phase, layer), ...]
    if var_conflicts:
        log.warn(
            "  Turns differ between coil sides sharing the same phase/layer "
            "for: " + ", ".join(f"{p}/L{l}" for p, l in var_conflicts) +
            "  — using the first value seen for the shared variable."
        )

    phase_layer_vars: dict = {}   # (phase, layer) → variable name

    for i, key in enumerate(var_keys):
        step = i
        _check_stop(step)

        phase, layer = key
        var_name = _phase_layer_var_name(phase, layer)

        if step < resume_from:
            log.skip(f"  SKIP (resume) step {step}  {var_name}")
            phase_layer_vars[key] = var_name
            _delay()
            continue

        try:
            maxwell[var_name] = int(var_values[key])
            phase_layer_vars[key] = var_name
            log.ok(f"  {var_name} = {var_values[key]}")
        except Exception as exc:
            log.error(f"  FAIL  {var_name}: {exc}")
            errors.append(f"{var_name}: {exc}")

        worker.checkpoint.emit(step)
        _delay()

    # ── Pre-flight: do the assignment objects exist, and are they usable? ───
    is3d = False
    try:
        is3d = not _is_2d_type(getattr(maxwell, "design_type", ""))
    except Exception:
        pass
    obj_names, obj_types = set(), {}
    try:
        obj_names = set(maxwell.modeler.object_names)
        for _nm in obj_names:
            try:
                obj_types[_nm] = str(maxwell.modeler.objects_by_name[_nm].object_type)
            except Exception:
                pass
    except Exception as exc:
        log.warn(f"  Pre-flight: could not read modeler objects ({exc}).")
    missing = {}
    if obj_names:
        import difflib
        for cs in coil_sides:
            t = cs["target"].strip()
            if t and t not in obj_names and t not in missing:
                missing[t] = difflib.get_close_matches(t, sorted(obj_names), n=3, cutoff=0.5)
        log.info(f"  Pre-flight: {len(obj_names)} object(s) in design; "
                 f"{len(missing)} assignment target(s) not found.")
        for t, close in list(missing.items())[:10]:
            log.error(f"    object '{t}' does not exist in the design"
                      + (f"  — similar: {', '.join(close)}" if close else ""))
        if len(missing) > 10:
            log.error(f"    … and {len(missing) - 10} more missing object(s).")
        if missing:
            pre = sorted(n for n in obj_names if n.lower().startswith("layer"))[:12]
            if pre:
                log.info("    objects starting with 'Layer': " + ", ".join(pre))
            log.info("    → fix the 'Assignment object' rule / copy offset to match "
                     "your geometry names.")

    # ── Pre-flight: boundaries that already exist (e.g. from a previous run) ─
    # assign_coil / assign_winding fail with "Failed to create boundary ..."
    # when a boundary of that name is already in the design.
    try:
        want = {cs["name"] for cs in coil_sides}
        want |= {f"Winding_{cs['phase']}" for cs in coil_sides}
        existing = {}
        for b in list(maxwell.boundaries):
            try:
                existing[str(b.name)] = b
            except Exception:
                pass
        clash = sorted(n for n in want if n in existing)
        if clash:
            if replace_existing:
                log.warn(f"  {len(clash)} boundary(ies) with the same name already exist "
                         f"— replacing: {', '.join(clash[:8])}"
                         + (" …" if len(clash) > 8 else ""))
                for n in clash:
                    try:
                        existing[n].delete()
                    except Exception:
                        try:
                            maxwell.oboundary.DeleteBoundaries([n])
                        except Exception as exc:
                            log.error(f"    could not delete '{n}': {exc}")
            else:
                log.warn(f"  {len(clash)} boundary(ies) already exist and will make the "
                         f"assignment fail: {', '.join(clash[:8])}  "
                         f"(tick 'Replace existing boundaries' or rename)")
    except Exception as exc:
        log.warn(f"  Pre-flight: could not read existing boundaries ({exc}).")

    # ── Phase 1: assign_coil ─────────────────────────────────────────────────
    log.head("── Step 2/4  Assigning coil sides ───────────────────")

    coil_step_base = len(var_keys)
    for i, cs in enumerate(coil_sides):
        step = coil_step_base + i
        _check_stop(step)

        if step < resume_from:
            log.skip(f"  SKIP (resume) step {step}  {cs['name']}")
            applied_coils[cs["name"]] = cs["phase"]
            _delay()
            continue

        cs_name  = cs["name"]
        target   = cs["target"].strip()
        positive = cs["direction"] == "+"
        polarity = "Positive" if positive else "Negative"
        var_name = phase_layer_vars.get((cs["phase"], cs["layer"]))

        if not target:
            log.warn(f"  SKIP  {cs_name}: target is empty")
            errors.append(f"{cs_name}: empty target")
            _delay()
            continue

        if target in missing:
            log.error(f"  SKIP  {cs_name}: object '{target}' not found in design")
            errors.append(f"{cs_name}: object '{target}' not found")
            _delay()
            continue

        try:
            # conductors_number is bound to the phase/layer design variable
            # (not a hardcoded integer) so editing that one variable in AEDT
            # changes turns for every coil side sharing it at once.
            obj = maxwell.assign_coil(
                assignment        = [target],
                conductors_number = var_name if var_name else cs["turns"],
                polarity          = polarity,
                name              = cs_name,
            )
            if obj is False:
                raise RuntimeError("assign_coil returned False")
            coil_objs[cs_name] = obj
            applied_coils[cs_name] = cs["phase"]
            log.ok(f"  [{i+1}/{n_coils}]  {cs_name}  →  '{target}'  "
                   f"({'+'if positive else '−'})  turns={var_name or cs['turns']}")
        except Exception as exc:
            hint = ""
            if is3d and obj_types.get(target, "").lower() == "solid":
                hint = (f"  [hint: '{target}' is a SOLID. Maxwell 3D coil terminals need a "
                        f"sheet (cross-section) or a face — assign to a sheet object "
                        f"or a face ID]")
            elif "failed to create boundary" in str(exc).lower():
                hint = ("  [hint: usually the name already exists in this design, or the "
                        "object is not a valid coil face/sheet]")
            log.error(f"  FAIL  {cs_name} → '{target}': {exc}{hint}")
            errors.append(f"{cs_name}: {exc}{hint}")

        worker.checkpoint.emit(step)
        _delay()

    # ── Phase 2: assign_winding (one per phase, no coils yet) ────────────────
    log.head("── Step 3/4  Creating per-phase windings (Stranded) ──")

    # build phase → coil name list  (only successfully assigned coils)
    phases: dict = {}
    for cs in coil_sides:
        if cs["name"] in applied_coils:
            phases.setdefault(cs["phase"], []).append(cs["name"])

    winding_step_base = coil_step_base + n_coils
    for pi, (ph, coil_names) in enumerate(phases.items()):
        step = winding_step_base + pi
        _check_stop(step)

        if step < resume_from:
            log.skip(f"  SKIP (resume) Winding_{ph}")
            winding_objs[ph] = None   # mark as "already done"
            _delay()
            continue

        ps = phase_settings.get(ph, {})
        wtype    = ps.get("winding_type",      "External")
        # Defaults to Stranded: with turns > 1 per coil, "Solid" conductor
        # type is only correct for single-turn bar/busbar windings, not
        # round-wire multi-turn coils like these. Still overridable per
        # phase in the winding table above.
        is_solid = ps.get("is_solid",          False)
        current  = ps.get("current",           1.0)
        resist   = ps.get("resistance",        0.0)
        induct   = ps.get("inductance",        0.0)
        voltage  = ps.get("voltage",           0.0)
        pbranch  = ps.get("parallel_branches", 1)

        try:
            # Correct call: NO assignment= parameter here.
            # Coils are added in step 4 via add_winding_coils().
            w_obj = maxwell.assign_winding(
                winding_type      = wtype,
                is_solid          = is_solid,
                current           = float(current),
                resistance        = float(resist),
                inductance        = float(induct),
                voltage           = float(voltage),
                parallel_branches = int(pbranch),
                name              = f"Winding_{ph}",
            )
            if w_obj is False:
                raise RuntimeError("assign_winding returned False")
            winding_objs[ph] = w_obj
            log.ok(f"  Winding_{ph}  type={wtype}  solid={is_solid}  "
                   f"I={current}A  branches={pbranch}")
        except Exception as exc:
            log.error(f"  FAIL  Winding_{ph}: {exc}")
            errors.append(f"Winding_{ph}: {exc}")

        worker.checkpoint.emit(step)
        _delay()

    # ── Phase 3: add_winding_coils ───────────────────────────────────────────
    log.head("── Step 4/4  Linking coils into windings ────────────")

    link_step_base = winding_step_base + len(phases)
    for pi, (ph, coil_names) in enumerate(phases.items()):
        step = link_step_base + pi
        _check_stop(step)

        if step < resume_from:
            log.skip(f"  SKIP (resume) link Winding_{ph}")
            _delay()
            continue

        if ph not in winding_objs or winding_objs[ph] is None:
            if step >= resume_from:
                log.warn(f"  SKIP  link Winding_{ph}: winding object missing")
            _delay()
            continue

        try:
            maxwell.add_winding_coils(
                assignment = f"Winding_{ph}",
                coils      = coil_names,
            )
            log.ok(f"  Linked {len(coil_names)} coil(s) → Winding_{ph}")
        except Exception as exc:
            log.error(f"  FAIL  link Winding_{ph}: {exc}")
            errors.append(f"link Winding_{ph}: {exc}")

        worker.checkpoint.emit(step)
        _delay()

    # ── Save ─────────────────────────────────────────────────────────────────
    total_steps = link_step_base + len(phases)
    _check_stop(total_steps)

    n_ok = len(coil_objs)
    if n_ok > 0 or winding_objs:
        try:
            maxwell.save_project()
            log.ok("Project saved.")
        except Exception as exc:
            log.warn(f"Save failed (project may still be open in ANSYS): {exc}")
    else:
        log.warn("Nothing was assigned — skipping save.")

    worker.checkpoint.emit(total_steps)

    summary = (f"Created {len(var_keys)} phase/layer variable(s), "
               f"assigned {n_ok}/{n_coils} coil side(s), "
               f"{len(winding_objs)} winding(s).")
    if errors:
        summary += "\nErrors:\n" + "\n".join(errors)
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# pyaedt script generator  (clean Python)
# ─────────────────────────────────────────────────────────────────────────────

def generate_maxwell_script(params: dict) -> str:
    project_name  = params.get("project_name",  "MyProject")
    design_name   = params.get("design_name",   "Maxwell3D_1")
    new_desktop   = params.get("new_desktop",   True)
    non_graphical = params.get("non_graphical", False)
    solver_type   = params.get("solver_type",   "EddyCurrent")
    cls_name      = "Maxwell2d" if _is_2d_type(params.get("design_type")) else "Maxwell3d"
    coil_sides    = params.get("coil_sides",    [])
    phase_settings = params.get("phase_settings", {})
    delay_ms      = params.get("delay_ms",      0)

    closed_coil = params.get("closed_coil", False)
    replace_existing = params.get("replace_existing", True)
    sequence_cw = params.get("sequence_cw", False)
    target_emf  = params.get("target_emf",  "CCW rotation EMF")

    # Apply closed-coil filtering to the script as well
    if closed_coil:
        layer1_slots_seen = set()
        filtered = []
        for cs in coil_sides:
            if cs.get("layer", 1) != 1:
                continue
            slot_key = (cs.get("phase","A"), cs.get("slot", cs.get("copy_index",0)))
            if slot_key in layer1_slots_seen:
                continue
            layer1_slots_seen.add(slot_key)
            filtered.append(cs)
        coil_sides = filtered

    # Apply CW inversion for script
    if sequence_cw:
        coil_sides = [
            {**cs, "direction": ("-" if cs.get("direction","+") == "+" else "+")}
            for cs in coil_sides
        ]
    dir_str = "CW (clockwise)" if sequence_cw else "CCW (counter-clockwise)"

    L = [
        "# ================================================================",
        "#  ANSYS Maxwell – Winding Setup Script",
        "#  Generated by SWAT-EM  " + datetime.now().strftime("%Y-%m-%d %H:%M"),
        f"#  Sequence direction : {dir_str}",
        f"#  Target EMF         : {target_emf}",
        f"#  Coil type          : {'Closed coil' if closed_coil else 'Open coil'}",
        "# ================================================================",
        "import time",
        f"from ansys.aedt.core import {cls_name}",
        "",
        "# ── 1.  Connect ──────────────────────────────────────────────",
        "# (existing design: its own solution type is kept)",
        f"maxwell = {cls_name}(",
        f"    project       = {repr(project_name)},",
        f"    design        = {repr(design_name)},",
        f"    new_desktop   = {new_desktop},",
        f"    non_graphical = {non_graphical},",
        "    close_on_exit = False,",
        ")",
        "",
        f"DELAY_MS = {delay_ms}   # ms pause between steps (0 = no delay)",
        "",
        "# ── 2.  Create per-phase/layer conductor-count variables ──────",
    ]

    var_values, _ = _build_phase_layer_vars(coil_sides)
    for (phase, layer), turns in var_values.items():
        var_name = _phase_layer_var_name(phase, layer)
        L += [f"maxwell[{repr(var_name)}] = {int(turns)}"]

    L += [
        "",
    ]
    if replace_existing:
        _all = [cs.get("name", "CoilSide") for cs in coil_sides]
        _all += sorted({f"Winding_{cs.get('phase', 'A')}" for cs in coil_sides})
        L += [
            "# ── 2b. Remove boundaries of the same name from a previous run ─",
            f"_names = {_all!r}",
            "for _b in list(maxwell.boundaries):",
            "    if _b.name in _names:",
            "        _b.delete()",
            "",
        ]
    L += [
        "# ── 3.  Assign coil sides (turns bound to the phase/layer var) ─",
        "coil_names_by_phase = {}  # phase → [coil_name, …]",
    ]

    phases: dict = {}
    for cs in coil_sides:
        name     = cs.get("name",      "CoilSide")
        target   = cs.get("target",    "").strip()
        phase    = cs.get("phase",     "A")
        layer    = cs.get("layer",     1)
        positive = cs.get("direction", "+") == "+"
        turns    = cs.get("turns",     1)
        copy     = cs.get("copy_index", 0)
        polarity = "Positive" if positive else "Negative"
        var_name = _phase_layer_var_name(phase, layer)

        L += [
            f"# Phase {phase}  |  {'+'if positive else '−'}  |"
            f"  turns={turns}  copy={copy}  target='{target}'",
            "maxwell.assign_coil(",
            f"    assignment        = [{repr(target)}],",
            f"    conductors_number = {repr(var_name)},",
            f"    polarity          = {repr(polarity)},",
            f"    name              = {repr(name)},",
            ")",
            f"coil_names_by_phase.setdefault({repr(phase)}, []).append({repr(name)})",
            f"if DELAY_MS: time.sleep(DELAY_MS / 1000.0)",
        ]
        phases.setdefault(phase, []).append(name)

    L += [
        "",
        "# ── 4.  Create per-phase windings (always Stranded; no",
        "#        assignment= here) ─────────────────────────────────────",
    ]
    for ph, coil_names in phases.items():
        ps = phase_settings.get(ph, {})
        L += [
            f"# Phase {ph}",
            "maxwell.assign_winding(",
            f"    winding_type      = {repr(ps.get('winding_type','External'))},",
            f"    is_solid          = {ps.get('is_solid', False)},  # defaults to Stranded",
            f"    current           = {float(ps.get('current', 1.0))},",
            f"    resistance        = {float(ps.get('resistance', 0.0))},",
            f"    inductance        = {float(ps.get('inductance', 0.0))},",
            f"    voltage           = {float(ps.get('voltage', 0.0))},",
            f"    parallel_branches = {int(ps.get('parallel_branches', 1))},",
            f"    name              = 'Winding_{ph}',",
            ")",
            f"if DELAY_MS: time.sleep(DELAY_MS / 1000.0)",
        ]

    L += [
        "",
        "# ── 5.  Link coils into windings ────────────────────────────",
    ]
    for ph in phases:
        L += [
            f"maxwell.add_winding_coils(",
            f"    assignment = 'Winding_{ph}',",
            f"    coils      = coil_names_by_phase[{repr(ph)}],",
            f")",
            f"if DELAY_MS: time.sleep(DELAY_MS / 1000.0)",
        ]

    L += [
        "",
        "# ── 6.  Save ─────────────────────────────────────────────────",
        "maxwell.save_project()",
        "print('Winding setup complete.')",
        "",
    ]
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# Column indices
# ─────────────────────────────────────────────────────────────────────────────
_C_NAME   = 0
_C_PHASE  = 1
_C_DIR    = 2
_C_TURNS  = 3
_C_LAYER  = 4
_C_SLOT   = 5
_C_COPY   = 6
_C_TARGET = 7

_COL_HEADERS = [
    "Name", "Phase", "Dir (+/−)", "Turns",
    "Layer", "Slot", "Copy Index", "Target Name in ANSYS",
]

# Per-phase winding table columns
_W_PHASE    = 0
_W_TYPE     = 1
_W_SOLID    = 2
_W_CURRENT  = 3
_W_RESIST   = 4
_W_INDUCT   = 5
_W_VOLTAGE  = 6
_W_BRANCHES = 7
_W_HEADERS  = [
    "Phase", "Winding Type", "Solid?",
    "Current (A)", "Resistance (Ω)", "Inductance (H)",
    "Voltage (V)", "Parallel Branches", "Turns / coil side",
]
_W_TURNS = 8


# ─────────────────────────────────────────────────────────────────────────────
# Dialog
# ─────────────────────────────────────────────────────────────────────────────

class AnsysExportDialog(QDialog):
    def __init__(self, data_model, parent=None):
        super().__init__(parent)
        self.data          = data_model
        self._maxwell      = None
        self._worker       = None
        self._resume_from  = 0       # next step to start from
        self._script_dirty = False   # user has manually edited the script

        self.setWindowTitle("ANSYS Maxwell – Winding Export  [pyaedt]")
        self.resize(1200, 800)
        self._build_ui()
        self._populate_from_model()
        self._refresh_script()

    # =========================================================================
    # UI
    # =========================================================================

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        # ── LEFT ─────────────────────────────────────────────────────────────
        left = QWidget()
        lv   = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 3, 0)
        lv.addWidget(self._build_settings_group())
        lv.addWidget(self._build_winding_group())
        lv.addWidget(self._build_table_group(), stretch=1)
        splitter.addWidget(left)

        # ── RIGHT ────────────────────────────────────────────────────────────
        right = QWidget()
        rv    = QVBoxLayout(right)
        rv.setContentsMargins(3, 0, 0, 0)

        grp_script = QGroupBox("pyaedt Script  (editable — changes are preview only until you click Apply)")
        gs = QVBoxLayout(grp_script)
        self.script_edit = QTextEdit()
        self.script_edit.setFont(QFont("Courier New", 9))
        self.script_edit.textChanged.connect(self._on_script_edited)
        gs.addWidget(self.script_edit)
        rv.addWidget(grp_script, stretch=3)

        grp_term = QGroupBox("Terminal / Log")
        gt = QVBoxLayout(grp_term)
        self.terminal = QTextEdit()
        self.terminal.setReadOnly(True)
        self.terminal.setFont(QFont("Courier New", 9))
        self.terminal.setStyleSheet(
            "QTextEdit{background:#1e1e1e;color:#d4d4d4;border:1px solid #444;}"
        )
        gt.addWidget(self.terminal)
        btn_clear = QPushButton("Clear log")
        btn_clear.setFixedWidth(80)
        btn_clear.clicked.connect(self.terminal.clear)
        gt.addWidget(btn_clear, alignment=Qt.AlignRight)
        rv.addWidget(grp_term, stretch=2)

        splitter.addWidget(right)
        splitter.setSizes([520, 680])

        self.log = _TerminalLogger(self.terminal)

        # ── bottom bar ────────────────────────────────────────────────────────
        bar = QHBoxLayout()
        bar.setSpacing(5)

        self.btn_connect = QPushButton("🔌 Connect")
        self.btn_connect.setToolTip("Connect to ANSYS Maxwell via pyaedt")
        self.btn_connect.clicked.connect(self._on_connect)

        self.btn_disconnect = QPushButton("⏏ Disconnect")
        self.btn_disconnect.setToolTip(
            "Release the pyaedt connection without closing ANSYS.\n"
            "ANSYS stays open; you can reconnect later."
        )
        self.btn_disconnect.setEnabled(False)
        self.btn_disconnect.clicked.connect(self._on_disconnect)

        self.btn_apply = QPushButton("▶ Apply")
        self.btn_apply.setToolTip("Apply winding to ANSYS (uses table or script)")
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self._on_apply)

        self.btn_stop = QPushButton("⏹ Stop")
        self.btn_stop.setToolTip("Request stop after current step")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)

        self.btn_resume = QPushButton("↩ Resume")
        self.btn_resume.setToolTip(
            "Continue from the last completed step.\n"
            "Only available after a Stop."
        )
        self.btn_resume.setEnabled(False)
        self.btn_resume.clicked.connect(self._on_resume)

        btn_refresh = QPushButton("🔄 Refresh Script")
        btn_refresh.setToolTip(
            "Regenerate script from table.\n"
            "This will overwrite any manual script edits."
        )
        btn_refresh.clicked.connect(self._force_refresh_script)

        btn_save = QPushButton("💾 Save Script…")
        btn_save.clicked.connect(self._save_script)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.reject)

        # Delay spinner
        bar.addWidget(QLabel("Step delay:"))
        self.spin_delay = QSpinBox()
        self.spin_delay.setRange(0, 10000)
        self.spin_delay.setValue(0)
        self.spin_delay.setSuffix(" ms")
        self.spin_delay.setFixedWidth(90)
        self.spin_delay.setToolTip(
            "Pause (ms) inserted between every coil/winding step.\n"
            "Increase (e.g. 500–2000 ms) if ANSYS gives gRPC timeout errors."
        )

        self.lbl_status = QLabel("● Not connected")
        self.lbl_status.setStyleSheet("color:#888;")

        self.lbl_resume = QLabel("")
        self.lbl_resume.setStyleSheet("color:#c678dd; font-style:italic;")

        bar.addWidget(self.btn_connect)
        bar.addWidget(self.btn_disconnect)
        bar.addWidget(self.btn_apply)
        bar.addWidget(self.btn_stop)
        bar.addWidget(self.btn_resume)
        bar.addSpacing(10)
        bar.addWidget(QLabel("Delay:"))
        bar.addWidget(self.spin_delay)
        bar.addSpacing(10)
        bar.addWidget(self.lbl_status)
        bar.addWidget(self.lbl_resume)
        bar.addStretch()
        bar.addWidget(btn_refresh)
        bar.addWidget(btn_save)
        bar.addWidget(btn_close)
        root.addLayout(bar)

    # ── Settings ──────────────────────────────────────────────────────────────

    def _build_settings_group(self):
        grp  = QGroupBox("Project / Design Settings")
        form = QFormLayout(grp)
        form.setLabelAlignment(Qt.AlignRight)

        # ── AEDT version ──────────────────────────────────────────────────────
        ver_row = QHBoxLayout()
        self.cb_aedt_version = QComboBox()
        self.cb_aedt_version.setMinimumWidth(160)
        self.cb_aedt_version.addItem("Auto (latest installed)", None)
        self.cb_aedt_version.setToolTip("Installed AEDT versions. Click Scan to detect.")
        btn_scan = QPushButton("🔍 Scan")
        btn_scan.setFixedWidth(65)
        btn_scan.setToolTip("Scan for installed AEDT versions on this machine")
        btn_scan.clicked.connect(self._scan_aedt_versions)
        ver_row.addWidget(self.cb_aedt_version, stretch=1)
        ver_row.addWidget(btn_scan)
        form.addRow("AEDT Version:", ver_row)
        self.lbl_aedt_found = QLabel("Click Scan to detect installed versions")
        self.lbl_aedt_found.setStyleSheet("color:#888;font-style:italic;")
        form.addRow("", self.lbl_aedt_found)

        # ── Project (text + live combo + refresh) ─────────────────────────────
        proj_row = QHBoxLayout()
        self.cb_project = QComboBox(); self.cb_project.setEditable(True)
        self.cb_project.setInsertPolicy(QComboBox.NoInsert)
        self.cb_project.setMinimumWidth(220)
        self.le_project = self.cb_project.lineEdit()
        self.le_project.setPlaceholderText("blank → active project  (▼ lists open projects)")
        self.cb_project.setToolTip("Type a name or pick an open project (⟳ to refresh)")
        btn_rp = QPushButton("⟳"); btn_rp.setFixedWidth(26)
        btn_rp.setToolTip("Read projects / designs / solver from the running AEDT")
        btn_rp.clicked.connect(self._refresh_lists)
        proj_row.addWidget(self.cb_project, stretch=1)
        proj_row.addWidget(btn_rp)
        form.addRow("Project Name:", proj_row)

        # ── Design (text + live combo + refresh) ──────────────────────────────
        des_row = QHBoxLayout()
        self.cb_design = QComboBox(); self.cb_design.setEditable(True)
        self.cb_design.setInsertPolicy(QComboBox.NoInsert)
        self.cb_design.setMinimumWidth(220)
        self.le_design = self.cb_design.lineEdit()
        self.le_design.setPlaceholderText("blank → active design  (▼ lists designs)")
        self.cb_design.setToolTip("Type a name or pick a design — solver type follows the design")
        self.cb_design.currentTextChanged.connect(self._on_design_changed)
        btn_rd = QPushButton("⟳"); btn_rd.setFixedWidth(26)
        btn_rd.setToolTip("Read projects / designs / solver from the running AEDT")
        btn_rd.clicked.connect(self._refresh_lists)
        des_row.addWidget(self.cb_design, stretch=1)
        des_row.addWidget(btn_rd)
        form.addRow("Design Name:", des_row)

        # Always attach to the running AEDT, with GUI (options removed from the UI)
        self.chk_new_desktop   = QCheckBox(); self.chk_new_desktop.setChecked(False)
        self.chk_non_graphical = QCheckBox(); self.chk_non_graphical.setChecked(False)

        self.cb_solver = QComboBox()
        self.cb_solver.addItems(["EddyCurrent","Magnetostatic","Transient",
                                 "TransientXY","TransientZ","EddyCurrentXY","EddyCurrentZ",
                                 "MagnetostaticXY","MagnetostaticZ"])
        self.cb_solver.setToolTip("Auto-set from the selected design after Connect / ⟳.\n"
            "Informational for existing designs: AEDT keeps the design's own solver\n"
            "(2D vs 3D follows the design type, not this box).")
        self.lbl_solver_auto = QLabel("")
        self.lbl_solver_auto.setStyleSheet("color:#6dcf81;font-style:italic;")
        srow = QHBoxLayout(); srow.addWidget(self.cb_solver, 1); srow.addWidget(self.lbl_solver_auto)
        form.addRow("Solver Type:", srow)

        # Coil NAME (boundary name in Maxwell).  Does NOT touch the assignment.
        self.le_base   = QLineEdit("{phase}_{layer}_{coil}")
        self.le_base.setToolTip(
            "Name of the coil boundary (assign_coil name=) - phase_layer_coil by default.\n"
            "It never changes the assignment (geometry object).\n"
            "Placeholders: {phase} {layer} {n} {coil} {slot} {copy} {ci}\n"
            "              {direction} {polarity} {name}\n"
            "{coil} = coil number inside its layer (starts at 0 or 1: see the toggle).")
        self.le_base.textChanged.connect(lambda _t: self._apply_naming_rule())
        # Geometry object (assignment): what assign_coil(assignment=) points to
        self.le_obj    = QLineEdit("Layer{layer}{ci}")
        self.le_obj.setToolTip(
            "Rule for the geometry object the coil is assigned to\n"
            "(assign_coil assignment=[...]).  You control the WHOLE name -\n"
            "nothing is appended automatically.\n"
            "Placeholders: {layer} {n} {slot} {copy} {coil} {phase} {direction}\n"
            "              {polarity} {name} {ci}\n"
            "{copy} = ANSYS copy index 0,1,2…   {coil} = 0/1-based number (toggle)\n"
            "{ci} = _0 / _0_1 / _0_2 (ANSYS copy token)\n"
            "Examples:  Layer{layer}{ci} → Layer1_0, Layer1_0_1 …\n"
            "           Coil{layer}_{slot} → Coil1_7     (table cells stay editable)")
        self.le_obj.textChanged.connect(lambda _t: self._apply_naming_rule())
        # Coil numbering start toggle (0 or 1)
        self.btn_coil_start = QPushButton("Coil no. from 1")
        self.btn_coil_start.setCheckable(True)
        self.btn_coil_start.setChecked(True)
        self.btn_coil_start.setMinimumWidth(120)
        self.btn_coil_start.setToolTip(
            "Toggle: {coil} starts at 1 (default) or at 0.\n"
            "{copy} / {ci} always follow the ANSYS copy index (start at 0).")
        self.btn_coil_start.toggled.connect(self._on_coil_start_toggled)
        self.lbl_obj_preview = QLabel("")
        self.lbl_obj_preview.setWordWrap(True)
        self.le_suffix = QLineEdit("")   # legacy holder, unused
        # Layer label popup: what {layer} expands to (instead of 1, 2, 3 …)
        self.cb_layer_lbl = QComboBox(); self.cb_layer_lbl.setEditable(True)
        self.cb_layer_lbl.setInsertPolicy(QComboBox.NoInsert)
        for _t in ["1,2,3", "[blank],RE", "A,B,C", "T,B", "In,Out", "L,R"]:
            self.cb_layer_lbl.addItem(_t)
        self.cb_layer_lbl.setToolTip(
            "Labels used for {layer}, one per layer (comma separated).\n"
            "'[blank]' = empty label.  Example  [blank],RE  →  Layer_0 / LayerRE_0.\n"
            "Layers beyond the list fall back to their number. {n} always gives the number.")
        form.addRow("Coil name:", self.le_base)
        form.addRow("Assignment object:", self.le_obj)
        lbl_row = QHBoxLayout()
        lbl_row.addWidget(QLabel("{layer} →"))
        lbl_row.addWidget(self.cb_layer_lbl, 1)
        lbl_row.addWidget(self.btn_coil_start)
        self.cb_layer_lbl.setCurrentIndex(0)
        form.addRow("", lbl_row)
        form.addRow("Result:", self.lbl_obj_preview)
        self.cb_layer_lbl.currentTextChanged.connect(self._on_layer_labels_changed)

        # Model symmetry: export only the coil sides of the modelled sector
        self.cb_symmetry = QComboBox()
        self.cb_symmetry.setToolTip(
            "Model symmetry - one list for everything:\n"
            "  Full machine       : all slots\n"
            "  1/N model (even)   : periodic boundary, slots 1 … Q/N\n"
            "  1/2N model (odd)   : anti-periodic boundary, slots 1 … Q/(2N)\n"
            "Even options need N to divide the winding periodicity t.\n"
            "Odd options are only offered when the winding really repeats with\n"
            "inverted polarity.")
        self._fill_symmetry_items()
        self.cb_symmetry.currentIndexChanged.connect(lambda _i: self._populate_from_model())
        form.addRow("Model symmetry:", self.cb_symmetry)

        # Closed-coil option
        self.chk_closed_coil = QCheckBox(
            "Closed coil  (single terminal per coil, no return-path object needed)"
        )
        self.chk_closed_coil.setChecked(False)
        self.chk_closed_coil.setToolTip(
            "Closed coil:\n"
            "  Each coil is self-contained — Maxwell closes the circuit\n"
            "  internally. Only ONE terminal object per coil is assigned.\n"
            "  For 2-layer windings only Layer 1 is used; Layer 2 is skipped.\n\n"
            "Open coil (default):\n"
            "  Both the go-terminal and the return-terminal objects are\n"
            "  assigned. Use this for 2-layer windings where you have separate\n"
            "  Layer-1 and Layer-2 objects in your geometry."
        )
        self.chk_closed_coil.toggled.connect(self._on_closed_coil_toggled)
        form.addRow("Coil type:", self.chk_closed_coil)

        self.lbl_closed_coil_info = QLabel("")
        self.lbl_closed_coil_info.setStyleSheet("color:#e5c07b;font-style:italic;")
        self.lbl_closed_coil_info.setWordWrap(True)
        form.addRow("", self.lbl_closed_coil_info)

        self.chk_replace = QCheckBox("Replace existing boundaries with the same name")
        self.chk_replace.setChecked(True)
        self.chk_replace.setToolTip(
            "Re-applying after a previous run: coils/windings with the same names\n"
            "already exist and AEDT refuses to create them again.\n"
            "ON: those boundaries are deleted first, then re-created.")
        self.chk_replace.toggled.connect(lambda _c: self._refresh_script())
        form.addRow("", self.chk_replace)

        # ── Sequence direction (CW / CCW) ──────────────────────────────────
        self.cb_sequence_dir = QComboBox()
        self.cb_sequence_dir.addItems([
            "CCW  (Counter-clockwise — default)",
            "CW   (Clockwise — inverts all polarities)",
        ])
        self.cb_sequence_dir.setToolTip(
            "Winding sequence direction viewed from the driven end.\n\n"
            "CCW (default): Phase A→B→C counter-clockwise.\n"
            "  '+' coil sides = Positive polarity in Maxwell.\n\n"
            "CW: Phase A→B→C clockwise.\n"
            "  All polarities INVERTED: '+' → Negative, '-' → Positive.\n"
            "  Use when your ANSYS geometry uses clockwise slot numbering."
        )
        self.cb_sequence_dir.currentIndexChanged.connect(
            lambda i: self.lbl_seq_info.setVisible(i == 1))
        self.cb_sequence_dir.setVisible(False)   # data holder; the toggle drives it
        self.btn_seq_toggle = QPushButton("⟲  CCW")
        self.btn_seq_toggle.setCheckable(True)
        self.btn_seq_toggle.setMinimumWidth(90)
        self.btn_seq_toggle.setToolTip(
            "Toggle winding sequence direction.\n"
            "⟲ CCW (default)   ⟳ CW (inverts all polarities)\n"
            "(Target EMF has its own toggle.)")
        self.btn_seq_toggle.toggled.connect(self._on_seq_toggled)
        self._seq_row = QHBoxLayout()
        self._seq_row.addWidget(self.btn_seq_toggle)
        self._seq_row.addWidget(self.cb_sequence_dir)

        # Target EMF: CW / CCW toggle (hidden combo keeps the text for log/script)
        self.cb_target_emf = QComboBox()
        self.cb_target_emf.addItems(["CCW rotation EMF", "CW rotation EMF"])
        self.cb_target_emf.setVisible(False)
        self.btn_emf_toggle = QPushButton("⟲  CCW")
        self.btn_emf_toggle.setCheckable(True)
        self.btn_emf_toggle.setMinimumWidth(90)
        self.btn_emf_toggle.setToolTip(
            "Target EMF / rotation direction:  ⟲ CCW (default)  or  ⟳ CW.\n"
            "Recorded in script header and log — does not change the Maxwell solver.")
        self.btn_emf_toggle.toggled.connect(self._on_emf_toggled)
        self._seq_row.addWidget(QLabel("Target EMF:"))
        self._seq_row.addWidget(self.btn_emf_toggle)
        self._seq_row.addWidget(self.cb_target_emf)
        self._seq_row.addStretch(1)
        form.addRow("Sequence direction:", self._seq_row)

        self.lbl_seq_info = QLabel(
            "⚠  CW: all coil polarities will be inverted when applying/generating script."
        )
        self.lbl_seq_info.setStyleSheet("color:#e5c07b;font-style:italic;")
        self.lbl_seq_info.setWordWrap(True)
        self.lbl_seq_info.setVisible(False)
        form.addRow("", self.lbl_seq_info)

        btn_rule = QPushButton("Apply naming to all rows")
        btn_rule.clicked.connect(self._apply_naming_rule)
        form.addRow("", btn_rule)
        return grp

    # ── Per-phase winding table ───────────────────────────────────────────────

    def _build_winding_group(self):
        grp = QGroupBox("Per-Phase Winding Parameters  (editable)")
        lv  = QVBoxLayout(grp)
        self.winding_table = QTableWidget()
        self.winding_table.setColumnCount(len(_W_HEADERS))
        self.winding_table.setHorizontalHeaderLabels(_W_HEADERS)
        self.winding_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.winding_table.setMaximumHeight(130)
        self.winding_table.setEditTriggers(
            QAbstractItemView.DoubleClicked |
            QAbstractItemView.SelectedClicked |
            QAbstractItemView.AnyKeyPressed
        )
        lv.addWidget(self.winding_table)
        return grp

    def _refresh_winding_table(self):
        """Rebuild per-phase winding table from coil table phases."""
        # collect unique phases in order
        phases = []
        seen   = set()
        for row in range(self.table.rowCount()):
            ph = self._cell(row, _C_PHASE) or "A"
            if ph not in seen:
                phases.append(ph)
                seen.add(ph)

        # preserve existing values
        existing = self._collect_phase_settings()

        self.winding_table.setRowCount(len(phases))
        for r, ph in enumerate(phases):
            prev = existing.get(ph, {})

            self.winding_table.setItem(r, _W_PHASE, QTableWidgetItem(ph))
            self.winding_table.item(r, _W_PHASE).setFlags(Qt.ItemIsEnabled)  # phase not editable

            cb_type = QComboBox()
            cb_type.addItems(["External", "Current", "Voltage"])
            cb_type.setCurrentText(prev.get("winding_type", "External"))
            self.winding_table.setCellWidget(r, _W_TYPE, cb_type)

            cb_solid = QComboBox()
            cb_solid.addItems(["Solid", "Stranded"])
            cb_solid.setCurrentIndex(0 if prev.get("is_solid", False) else 1)
            self.winding_table.setCellWidget(r, _W_SOLID, cb_solid)

            for col, key, default in [
                (_W_CURRENT,  "current",           "1.0"),
                (_W_RESIST,   "resistance",         "0.0"),
                (_W_INDUCT,   "inductance",         "0.0"),
                (_W_VOLTAGE,  "voltage",            "0.0"),
                (_W_BRANCHES, "parallel_branches",  "1"),
            ]:
                self.winding_table.setItem(
                    r, col, QTableWidgetItem(str(prev.get(key, default)))
                )

            # turns for every coil side of this phase (all layers)
            t0 = 1
            for row in range(self.table.rowCount()):
                if (self._cell(row, _C_PHASE) or "A") == ph:
                    c = self._cell(row, _C_TURNS)
                    t0 = int(c) if c.isdigit() else 1
                    break
            sp = QSpinBox(); sp.setRange(1, 100000)
            sp.setValue(int(prev.get("turns", t0)) if prev else t0)
            sp.setToolTip("Turns per coil side of this phase — written to every coil side "
                          "and to the Turns_<phase>_L<layer> design variables")
            sp.valueChanged.connect(lambda v, ph=ph: self._on_phase_turns_changed(ph, v))
            self.winding_table.setCellWidget(r, _W_TURNS, sp)

    def _on_phase_turns_changed(self, ph: str, v: int):
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            if (self._cell(row, _C_PHASE) or "A") == ph:
                self._set_cell(row, _C_TURNS, str(v))
        self.table.blockSignals(False)
        self._refresh_script()

    def _collect_phase_settings(self) -> dict:
        settings = {}
        for r in range(self.winding_table.rowCount()):
            ph_item = self.winding_table.item(r, _W_PHASE)
            if not ph_item:
                continue
            ph = ph_item.text().strip()

            cb_type  = self.winding_table.cellWidget(r, _W_TYPE)
            cb_solid = self.winding_table.cellWidget(r, _W_SOLID)

            def _fval(col, default=0.0):
                it = self.winding_table.item(r, col)
                try: return float(it.text()) if it else default
                except ValueError: return default

            def _ival(col, default=1):
                it = self.winding_table.item(r, col)
                try: return int(it.text()) if it else default
                except ValueError: return default

            settings[ph] = {
                "winding_type":      cb_type.currentText()  if cb_type  else "External",
                "is_solid":          cb_solid.currentIndex() == 0 if cb_solid else False,
                "current":           _fval(_W_CURRENT,  1.0),
                "resistance":        _fval(_W_RESIST,   0.0),
                "inductance":        _fval(_W_INDUCT,   0.0),
                "voltage":           _fval(_W_VOLTAGE,  0.0),
                "parallel_branches": _ival(_W_BRANCHES, 1),
                "turns":             (self.winding_table.cellWidget(r, _W_TURNS).value()
                                      if self.winding_table.cellWidget(r, _W_TURNS) else 1),
            }
        return settings

    # ── Coil table ────────────────────────────────────────────────────────────

    def _build_table_group(self):
        grp    = QGroupBox("Coil Sides  (all cells editable)")
        layout = QVBoxLayout(grp)
        tb = QHBoxLayout()
        for label, fn in [("➕ Add", self._add_row),
                           ("🗑 Delete", self._delete_row),
                           ("⟳ Reset", self._populate_from_model)]:
            b = QPushButton(label); b.clicked.connect(fn); tb.addWidget(b)
        tb.addStretch()
        layout.addLayout(tb)

        self.table = QTableWidget()
        self.table.setColumnCount(len(_COL_HEADERS))
        self.table.setHorizontalHeaderLabels(_COL_HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked |
            QAbstractItemView.SelectedClicked |
            QAbstractItemView.EditKeyPressed  |
            QAbstractItemView.AnyKeyPressed
        )
        self.table.itemChanged.connect(self._on_table_changed)
        layout.addWidget(self.table)
        return grp

    # =========================================================================
    # Table helpers
    # =========================================================================

    def _cell(self, row, col) -> str:
        it = self.table.item(row, col)
        return it.text().strip() if it else ""

    def _set_cell(self, row, col, text: str):
        it = self.table.item(row, col)
        if it is None:
            it = QTableWidgetItem()
            self.table.setItem(row, col, it)
        it.setText(text)

    def _direction(self, row) -> str:
        cb = self.table.cellWidget(row, _C_DIR)
        return cb.currentText() if cb else "+"

    def _on_table_changed(self, item):
        """When anything in the coil table changes, refresh script and winding table."""
        if item.column() == _C_PHASE:
            self._refresh_winding_table()
        self._refresh_script()

    # ── Naming ────────────────────────────────────────────────────────────────

    def _sym_data(self):
        """(n, odd) of the selected symmetry entry."""
        d = self.cb_symmetry.currentData() if hasattr(self, "cb_symmetry") else None
        try:
            n, o = str(d).split(":")
            return max(1, int(n)), o == "1"
        except Exception:
            return 1, False

    def _symmetry_odd(self) -> bool:
        return self._sym_data()[1]

    def _symmetry_n(self) -> int:
        return self._sym_data()[0]

    def _fill_symmetry_items(self):
        """One list: Full machine, every valid 1/N (even) and 1/2N (odd) model."""
        try:
            t = int(self.data.get_periodicity_t())
        except Exception:
            t = 1
        t = max(t, 1)
        self._sym_t = t
        try:
            Q = int(self.data.get_num_slots())
            phases = self.data.get_phases()
        except Exception:
            Q, phases = 0, []
        entries = []          # (denominator, odd, n, label)
        for n in range(1, t + 1):
            if t % n:
                continue
            if n > 1:
                entries.append((n, False, n, f"1/{n} model  (even, periodic)"))
            if Q > 0 and Q % (2 * n) == 0 and _is_antiperiodic(phases, Q, Q // (2 * n)):
                entries.append((2 * n, True, n, f"1/{2 * n} model  (odd, anti-periodic)"))
        entries.sort(key=lambda e: (e[0], e[1]))
        old = self.cb_symmetry.currentData() if self.cb_symmetry.count() else None
        self.cb_symmetry.blockSignals(True)
        self.cb_symmetry.clear()
        self.cb_symmetry.addItem("Full machine", "1:0")
        for _den, odd, n, label in entries:
            self.cb_symmetry.addItem(label, f"{n}:{1 if odd else 0}")
        i = self.cb_symmetry.findData(old) if old is not None else -1
        self.cb_symmetry.setCurrentIndex(i if i >= 0 else 0)
        self.cb_symmetry.blockSignals(False)

    def _coil_start(self) -> int:
        return 1 if (not hasattr(self, "btn_coil_start")
                     or self.btn_coil_start.isChecked()) else 0

    def _on_coil_start_toggled(self, on: bool):
        self.btn_coil_start.setText("Coil no. from 1" if on else "Coil no. from 0")
        if hasattr(self, "table") and self.table.rowCount():
            self._apply_naming_rule()

    def _layer_labels(self):
        """Parsed label list; empty list → numbers."""
        txt = self.cb_layer_lbl.currentText().strip()
        if not txt:
            return []
        out = []
        for t in txt.split(","):
            t = t.strip()
            out.append("" if t.lower() in ("[blank]", "blank", "[]") else t)
        return out

    def _layer_label(self, layer) -> str:
        labels = self._layer_labels()
        try:
            i = int(layer) - 1
        except (TypeError, ValueError):
            return str(layer)
        return labels[i] if 0 <= i < len(labels) else str(layer)

    def _on_layer_labels_changed(self, _t=None):
        if hasattr(self, "table") and self.table.rowCount():
            self._apply_naming_rule()

    def _calc_target_ex(self, layer, slot, copy_index, phase, direction, name):
        """Return (assignment object name, error).  Error text is '' if OK."""
        polarity = "Positive" if direction == "+" else "Negative"
        return _format_assignment(
            self.le_obj.text(), layer=self._layer_label(layer), n=layer,
            slot=slot, copy=copy_index, phase=phase, direction=direction,
            polarity=polarity, name=name, coil_start=self._coil_start())

    def _calc_target(self, layer, slot, copy_index, phase, direction, name) -> str:
        txt, err = self._calc_target_ex(layer, slot, copy_index, phase, direction, name)
        if err:
            return ""          # never silently invent a different object
        return txt

    def _update_obj_preview(self):
        """Live preview of the rule for the first copies of each layer."""
        if not hasattr(self, "lbl_obj_preview"):
            return
        try:
            n_layers = int(self.data.get_num_layers())
        except Exception:
            n_layers = 1
        parts, err_msg = [], ""
        for lay in range(1, max(1, min(n_layers, 3)) + 1):
            row = []
            for ci in range(3):
                t, e = self._calc_target_ex(lay, ci + 1, ci, "A", "+", f"Layer{lay}_{ci+1}")
                if e:
                    err_msg = e
                    break
                row.append(t)
            if err_msg:
                break
            parts.append(f"L{lay}:  " + "  ·  ".join(row) + "  …")
        if err_msg:
            self.lbl_obj_preview.setStyleSheet("color:#e06c75;font-weight:bold;")
            self.lbl_obj_preview.setText(
                f"⚠ {err_msg}.  Allowed: " + " ".join("{%s}" % t for t in _ASSIGN_TOKENS))
        else:
            self.lbl_obj_preview.setStyleSheet("color:#6dcf81;font-family:monospace;")
            self.lbl_obj_preview.setText("\n".join(parts))

    def _calc_name(self, layer, slot, copy_index, phase, direction) -> str:
        """Coil boundary name from the 'Coil name' template (never the assignment)."""
        polarity = "Positive" if direction == "+" else "Negative"
        default  = f"{phase}_{self._layer_label(layer)}_{int(copy_index) + self._coil_start()}"
        txt, err = _format_assignment(
            self.le_base.text(), layer=self._layer_label(layer), n=layer,
            slot=slot, copy=copy_index, phase=phase, direction=direction,
            polarity=polarity, name=default, coil_start=self._coil_start())
        return default if err else txt

    def _on_closed_coil_toggled(self, checked: bool):
        """Update the info label and refresh the script when mode changes."""
        if checked:
            n_layers = 1
            try:
                n_layers = self.data.get_num_layers()
            except Exception:
                pass
            if n_layers == 2:
                self.lbl_closed_coil_info.setText(
                    "⚠ 2-layer winding detected.  In closed-coil mode only "
                    "Layer 1 terminals are assigned; Layer 2 rows are "
                    "skipped.  See tooltip for details."
                )
            else:
                self.lbl_closed_coil_info.setText(
                    "Closed coil: only the first terminal per slot is assigned."
                )
        else:
            self.lbl_closed_coil_info.setText("")
        self._refresh_script()

    def _apply_naming_rule(self):
        self.table.blockSignals(True)
        used = set()
        for row in range(self.table.rowCount()):
            ls = self._cell(row, _C_LAYER)
            cs = self._cell(row, _C_COPY)
            ss = self._cell(row, _C_SLOT)
            layer = int(ls) if ls.isdigit() else 1
            slot  = int(ss) if ss.isdigit() else row + 1
            copy  = int(cs) if cs.isdigit() else 0
            phase = self._cell(row, _C_PHASE) or "A"
            direc = self._direction(row)
            name = self._unique_name(
                self._calc_name(layer, slot, copy, phase, direc), used)
            self._set_cell(row, _C_NAME, name)
            target = self._calc_target(layer, slot, copy, phase, direc, name)
            self._set_cell(row, _C_TARGET, target)     # assignment: own template
        self.table.blockSignals(False)
        self._update_obj_preview()
        self._refresh_script()

    @staticmethod
    def _unique_name(name: str, used: set) -> str:
        cand, k = name, 2
        while cand in used:
            cand = f"{name}_{k}"
            k += 1
        used.add(cand)
        return cand

    # ── Insert / populate ─────────────────────────────────────────────────────

    def _insert_row(self, row, name, phase, direction,
                    turns, layer, slot, copy_index, target):
        self.table.blockSignals(True)
        self.table.insertRow(row)
        self.table.setItem(row, _C_NAME,   QTableWidgetItem(name))
        self.table.setItem(row, _C_PHASE,  QTableWidgetItem(phase))
        self.table.setItem(row, _C_TURNS,  QTableWidgetItem(turns))
        self.table.setItem(row, _C_LAYER,  QTableWidgetItem(layer))
        self.table.setItem(row, _C_SLOT,   QTableWidgetItem(slot))
        self.table.setItem(row, _C_COPY,   QTableWidgetItem(copy_index))
        self.table.setItem(row, _C_TARGET, QTableWidgetItem(target))
        cb = QComboBox()
        cb.addItems(["+", "-"])
        cb.setCurrentText(direction)
        self.table.setCellWidget(row, _C_DIR, cb)
        self.table.blockSignals(False)

    def _populate_from_model(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        try:
            phases     = self.data.get_phases()
            phasenames = self.data.get_phasenames()
            turns_raw  = self.data.get_turns()
        except Exception as exc:
            self.log.warn(f"Could not read winding data: {exc}")
            self.table.blockSignals(False)
            return

        # symmetry: keep only slots of the modelled sector
        sym_n = self._symmetry_n() if hasattr(self, "cb_symmetry") else 1
        try:
            Q_all = int(self.data.get_num_slots())
        except Exception:
            Q_all = 0
        sym_odd = self._symmetry_odd() if hasattr(self, "cb_symmetry") else False
        q_keep = (_sector_slot_count(Q_all, sym_n, sym_odd)
                  if ((sym_n > 1 or sym_odd) and Q_all) else None)

        # flatten + compute copy_index per layer
        entries = []
        for ph_idx, ph_slots in enumerate(phases):
            ph_name = phasenames[ph_idx] if ph_idx < len(phasenames) else str(ph_idx+1)
            for layer_idx, layer_slots in enumerate(ph_slots):
                for slot_num in layer_slots:
                    direction = "+" if slot_num > 0 else "-"
                    abs_slot  = abs(slot_num)
                    if q_keep is not None and abs_slot > q_keep:
                        continue
                    try:
                        t = (turns_raw[ph_idx][layer_idx][0]
                             if isinstance(turns_raw, (list, tuple)) else turns_raw)
                    except Exception:
                        t = 1
                    entries.append(dict(layer_idx=layer_idx, abs_slot=abs_slot,
                                        ph_name=ph_name, direction=direction, turns=int(t)))

        layer_slots_seen: dict = defaultdict(list)
        for e in entries:
            s = e["abs_slot"]
            if s not in layer_slots_seen[e["layer_idx"]]:
                layer_slots_seen[e["layer_idx"]].append(s)
        for li in layer_slots_seen:
            layer_slots_seen[li].sort()
        copy_map = {(li, s): ci
                    for li, slots in layer_slots_seen.items()
                    for ci, s in enumerate(slots)}

        used_names = set()
        for ri, e in enumerate(entries):
            li   = e["layer_idx"]
            sl   = e["abs_slot"]
            ci   = copy_map[(li, sl)]
            name = self._unique_name(
                self._calc_name(li+1, sl, ci, e["ph_name"], e["direction"]), used_names)
            tgt  = self._calc_target(li+1, sl, ci, e["ph_name"], e["direction"], name)
            self._insert_row(ri, name, e["ph_name"], e["direction"],
                             str(e["turns"]), str(li+1), str(sl), str(ci), tgt)

        self.table.blockSignals(False)
        self.log.info(f"Loaded {len(entries)} coil side(s) from model"
                      + (f"  (1/{sym_n * (2 if sym_odd else 1)} model, "
                           f"{'odd' if sym_odd else 'even'}: slots 1…{q_keep})"
                           if q_keep else "  (full machine)") + ".")
        self.winding_table.setRowCount(0)   # re-seed per-phase turns from coil table
        self._refresh_winding_table()
        self._update_obj_preview()
        self._refresh_script()

    def _add_row(self):
        r = self.table.rowCount()
        self._insert_row(r, f"CoilSide_{r+1}", "A", "+",
                         "1", "1", str(r+1), "0",
                         "Layer1_0")
        self._refresh_winding_table()

    def _delete_row(self):
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self._refresh_winding_table()

    # =========================================================================
    # Collect params
    # =========================================================================

    def _collect_params(self) -> dict:
        coil_sides = []
        for row in range(self.table.rowCount()):
            t = self._cell(row, _C_TURNS)
            c = self._cell(row, _C_COPY)
            l = self._cell(row, _C_LAYER)
            coil_sides.append({
                "name":       self._cell(row, _C_NAME)  or f"CoilSide_{row+1}",
                "phase":      self._cell(row, _C_PHASE) or "A",
                "direction":  self._direction(row),
                "turns":      int(t) if t.isdigit() else 1,
                "layer":      int(l) if l.isdigit() else 1,
                "slot":       self._cell(row, _C_SLOT) or str(row+1),
                "copy_index": int(c) if c.isdigit() else 0,
                "target":     self._cell(row, _C_TARGET),
            })
        return {
            "project_name":   self.le_project.text().strip() or None,
            "design_name":    self.le_design.text().strip()  or None,
            "new_desktop":    self.chk_new_desktop.isChecked(),
            "non_graphical":  self.chk_non_graphical.isChecked(),
            "solver_type":    self.cb_solver.currentText(),
            "design_type":    (getattr(self, "_design_types", {}) or {}).get(
                                  self.le_design.text().strip()
                                  or getattr(self._maxwell, "design_name", "") or "", ""),
            "coil_sides":     coil_sides,
            "phase_settings": self._collect_phase_settings(),
            "delay_ms":       self.spin_delay.value(),
            "closed_coil":    self.chk_closed_coil.isChecked(),
            "replace_existing": self.chk_replace.isChecked(),
            "sequence_cw":    self.cb_sequence_dir.currentIndex() == 1,
            "target_emf":     self.cb_target_emf.currentText(),
        }

    # =========================================================================
    # Script preview
    # =========================================================================

    def _refresh_script(self):
        """Regenerate script from table — only if user hasn't manually edited it."""
        if self._script_dirty:
            return
        self.script_edit.blockSignals(True)
        self.script_edit.setPlainText(generate_maxwell_script(self._collect_params()))
        self.script_edit.blockSignals(False)

    def _force_refresh_script(self):
        """Force regenerate from table even if user edited the script."""
        self._script_dirty = False
        self.script_edit.blockSignals(True)
        self.script_edit.setPlainText(generate_maxwell_script(self._collect_params()))
        self.script_edit.blockSignals(False)
        self.log.info("Script refreshed from table.")

    def _on_script_edited(self):
        """Mark script as manually edited so auto-refresh won't overwrite it."""
        self._script_dirty = True

    # =========================================================================
    # AEDT version scan + project/design auto-populate
    # =========================================================================

    def _scan_aedt_versions(self):
        """
        Scan installed AEDT versions and populate the version combo.

        Uses ansys.aedt.core.internal.aedt_versions.aedt_versions.installed_versions
        which reads ANSYSEM_ROOTxxx / AWP_ROOTxxx environment variables — no
        Desktop instance required.
        """
        self.log.info("Scanning for installed AEDT versions …")
        try:
            from ansys.aedt.core.internal.aedt_versions import aedt_versions as _av
            # installed_versions is a property on the singleton instance
            versions = _av.installed_versions          # dict: {ver_id: path}
            self.cb_aedt_version.blockSignals(True)
            self.cb_aedt_version.clear()
            self.cb_aedt_version.addItem("Auto (latest installed)", None)
            if versions:
                for ver_id, path in sorted(versions.items(), reverse=True):
                    self.cb_aedt_version.addItem(f"{ver_id}  —  {path}", ver_id)
                latest = next(iter(sorted(versions.keys(), reverse=True)))
                self.lbl_aedt_found.setText(
                    f"Found {len(versions)} version(s).  Latest: {latest}")
                self.lbl_aedt_found.setStyleSheet("color:#6dcf81;")
                self.log.ok(f"Installed AEDT: {sorted(versions.keys(), reverse=True)}")
            else:
                # No env vars set — AEDT may still be reachable via gRPC/COM
                self.lbl_aedt_found.setText(
                    "No AEDT env variables found (ANSYSEM_ROOTxxx / AWP_ROOTxxx).  "
                    "Leave version as Auto — pyaedt will detect the running session."
                )
                self.lbl_aedt_found.setStyleSheet("color:#e5c07b;")
                self.log.warn(
                    "No ANSYSEM_ROOTxxx / AWP_ROOTxxx environment variables found.  "
                    "pyaedt will attach to the running ANSYS session automatically."
                )
            self.cb_aedt_version.blockSignals(False)
        except Exception as exc:
            self.log.error(f"Version scan failed: {exc}")
            self.lbl_aedt_found.setText(f"Scan error: {exc}")
            self.lbl_aedt_found.setStyleSheet("color:#e06c75;")

    def _on_seq_toggled(self, cw: bool):
        self.btn_seq_toggle.setText("⟳  CW" if cw else "⟲  CCW")
        self.cb_sequence_dir.setCurrentIndex(1 if cw else 0)   # also shows warning
        if hasattr(self, "script_edit") or hasattr(self, "te_script"):
            self._refresh_script()

    def _on_emf_toggled(self, cw: bool):
        self.btn_emf_toggle.setText("⟳  CW" if cw else "⟲  CCW")
        self.cb_target_emf.setCurrentIndex(1 if cw else 0)
        if hasattr(self, "script_edit") or hasattr(self, "te_script"):
            self._refresh_script()

    def _on_design_changed(self, name: str):
        """Selecting a design auto-picks its solver type."""
        st = getattr(self, "_design_solvers", {}).get(name)
        if st:
            self._set_solver(st)

    def _set_solver(self, st: str):
        idx = self.cb_solver.findText(st)
        if idx < 0:
            self.cb_solver.addItem(st); idx = self.cb_solver.findText(st)
        self.cb_solver.setCurrentIndex(idx)
        self.lbl_solver_auto.setText("auto-detected")

    def _fill_lists(self, info: dict):
        """Apply info gathered by _gather_info (GUI thread)."""
        self._design_solvers = info.get("design_solvers", {})
        self._design_types = info.get("design_types", {}) or getattr(self, "_design_types", {})
        for cb, items, cur in ((self.cb_project, info.get("projects", []), info.get("project", "")),
                               (self.cb_design,  info.get("designs", []),  info.get("design", ""))):
            cb.blockSignals(True)
            cb.clear()
            cb.addItems([str(i) for i in items])
            cb.setEditText(cur or "")
            cb.blockSignals(False)
        if info.get("solver"):
            self._set_solver(info["solver"])
        self.log.ok(f"Projects: {info.get('projects')}  | Designs: {info.get('designs')}"
                    f"  | active: {info.get('project')} / {info.get('design')}"
                    f"  | solver: {info.get('solver')}")

    def _refresh_lists(self):
        if self._maxwell is None:
            self.log.warn("Not connected — press Connect first."); return
        if self._worker and self._worker.isRunning():
            self.log.warn("A task is running — please wait."); return
        mx = self._maxwell
        def _task(worker, **_):
            self._conn_info = _gather_info(mx)
            return "lists"
        self._worker = _Worker(_task, {})
        self._worker.finished.connect(self._on_lists_done)
        self._worker.start()

    def _on_lists_done(self, ok, msg):
        if ok:
            self._fill_lists(self._conn_info)
        else:
            self.log.error(f"Cannot read project/design lists: {msg}")

    def _refresh_project_list(self):
        """Populate project combo from connected ANSYS session."""
        if self._maxwell is None:
            self.log.warn("Not connected — cannot refresh project list."); return
        try:
            projects = list(self._maxwell.odesktop.GetProjectList())
            self.cb_project.blockSignals(True)
            self.cb_project.clear()
            for p in projects:
                self.cb_project.addItem(p)
            cur = self._maxwell.project_name or ""
            idx = self.cb_project.findText(cur)
            if idx >= 0: self.cb_project.setCurrentIndex(idx)
            self.cb_project.blockSignals(False)
            self.log.ok(f"Projects: {projects}  (active: {cur})")
        except Exception as exc:
            self.log.error(f"Cannot read project list: {exc}")

    def _refresh_design_list(self):
        """Populate design combo with designs from active project."""
        if self._maxwell is None:
            self.log.warn("Not connected — cannot refresh design list."); return
        try:
            all_designs = self._maxwell.design_list
            self.cb_design.blockSignals(True)
            self.cb_design.clear()
            for d in all_designs:
                self.cb_design.addItem(d)
            cur = self._maxwell.design_name or ""
            idx = self.cb_design.findText(cur)
            if idx >= 0: self.cb_design.setCurrentIndex(idx)
            self.cb_design.blockSignals(False)
            self.log.ok(f"Designs: {all_designs}  (active: {cur})")
        except Exception as exc:
            self.log.error(f"Cannot read design list: {exc}")

    # =========================================================================
    # Connect
    # =========================================================================

    def _on_connect(self):
        if self._worker and self._worker.isRunning():
            self.log.warn("A task is running — please wait.")
            return
        params = self._collect_params()
        self.log.head("═══ Connecting to ANSYS Maxwell ════════════════════")
        self._set_busy(True)

        ver_idx  = self.cb_aedt_version.currentIndex()
        sel_ver  = self.cb_aedt_version.itemData(ver_idx) if ver_idx > 0 else None

        def _task(worker, project_name, design_name, solver_type,
                  new_desktop, non_graphical, **_):
            mx = _pyaedt_connect(project_name, design_name, solver_type,
                                 new_desktop, non_graphical, self.log,
                                 aedt_version=sel_ver,
                                 design_types=dict(getattr(self, "_design_types", {}) or {}))
            self._maxwell = mx
            self._conn_info = _gather_info(mx)
            return f"project={mx.project_name}  design={mx.design_name}"

        self._worker = _Worker(_task, params)
        self._worker.finished.connect(self._on_connect_done)
        self._worker.start()

    def _on_connect_done(self, success, message):
        self._set_busy(False)
        if success:
            self.log.ok(f"Connected: {message}")
            self.lbl_status.setText("● Connected")
            self.lbl_status.setStyleSheet("color:#6dcf81;font-weight:bold;")
            self.btn_apply.setEnabled(True)
            self.btn_disconnect.setEnabled(True)
            self._resume_from = 0
            self.btn_resume.setEnabled(False)
            self.lbl_resume.setText("")
            if self._maxwell:
                self._fill_lists(getattr(self, "_conn_info", {}) or {})
                ver = getattr(self._maxwell, "aedt_version_id", None)
                if ver:
                    self.lbl_aedt_found.setText(f"Connected — AEDT {ver}")
                    self.lbl_aedt_found.setStyleSheet("color:#6dcf81;font-weight:bold;")
            # Show closed-coil info if already checked
            if self.chk_closed_coil.isChecked():
                self._on_closed_coil_toggled(True)
        else:
            self.log.error("Connection failed:")
            self.log.dump(message, "ERROR")
            self.lbl_status.setText("● Not connected")
            self.lbl_status.setStyleSheet("color:#e06c75;")
            self._maxwell = None
            self.btn_apply.setEnabled(False)
            self.btn_disconnect.setEnabled(False)

    def _on_disconnect(self):
        """Release pyaedt without closing ANSYS."""
        if self._worker and self._worker.isRunning():
            self.log.warn("A task is running — stop it before disconnecting.")
            return
        if self._maxwell is None:
            return
        try:
            self._maxwell.release_desktop(
                close_projects = False,
                close_desktop  = False,
            )
            self.log.ok("Disconnected from ANSYS (ANSYS remains open).")
        except Exception as exc:
            self.log.warn(f"release_desktop raised: {exc}  (treating as disconnected)")
        finally:
            self._maxwell = None
            self.lbl_status.setText("● Not connected")
            self.lbl_status.setStyleSheet("color:#888;")
            self.btn_apply.setEnabled(False)
            self.btn_disconnect.setEnabled(False)
            self._resume_from = 0
            self.btn_resume.setEnabled(False)
            self.lbl_resume.setText("")

    # =========================================================================
    # Apply
    # =========================================================================

    def _on_apply(self):
        if self._maxwell is None:
            self.log.warn("Not connected. Click Connect first.")
            return
        if self._worker and self._worker.isRunning():
            self.log.warn("A task is running — please wait.")
            return

        # If user edited the script, ask which source to use
        if self._script_dirty:
            ans = QMessageBox.question(
                self, "Apply source",
                "You have manually edited the script.\n\n"
                "▶ YES   — execute the script as-is (via exec)\n"
                "▶ NO    — use the table data (ignore script edits)",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if ans == QMessageBox.Cancel:
                return
            if ans == QMessageBox.Yes:
                self._apply_from_script()
                return
            # else fall through to table-based apply

        # Apply always runs the full sequence from step 0, regardless of
        # any earlier Stop checkpoint. Use the Resume button to pick up
        # from where a previous run was stopped.
        self._apply_from_table(resume_from=0)

    def _apply_from_table(self, resume_from=0):
        params     = self._collect_params()
        coil_sides = params["coil_sides"]
        ph_settings = params["phase_settings"]
        delay_ms   = params["delay_ms"]

        empty = [cs["name"] for cs in coil_sides if not cs["target"]]
        if empty:
            self.log.warn(f"Empty targets (will skip): {empty}")

        if resume_from > 0:
            self.log.head(f"═══ Resuming from step {resume_from} ═══════════════════")
        else:
            self.log.head("═══ Applying to ANSYS Maxwell ══════════════════════")

        self._set_busy(True, stop=True)
        maxwell = self._maxwell

        closed_coil = self.chk_closed_coil.isChecked()
        if closed_coil:
            n_layers = self.data.get_num_layers()
            if n_layers == 2:
                self.log.warn(
                    "Closed-coil + 2-layer winding: only Layer 1 terminals "
                    "will be assigned. Layer 2 is skipped because in a closed "
                    "coil the return path is inside the coil itself — Maxwell "
                    "does not need a second terminal. If your geometry has "
                    "separate Layer-2 objects you can uncheck Closed Coil and "
                    "assign both layers as open coils instead."
                )

        sequence_cw = self.cb_sequence_dir.currentIndex() == 1
        replace_existing = self.chk_replace.isChecked()
        target_emf  = self.cb_target_emf.currentText()
        if sequence_cw:
            self.log.warn(f"CW sequence: polarities inverted (target EMF: {target_emf})")

        def _task(worker, **_):
            return _pyaedt_apply(maxwell, coil_sides, ph_settings,
                                 delay_ms, resume_from, self.log, worker,
                                 closed_coil=closed_coil,
                                 sequence_cw=sequence_cw,
                                 replace_existing=replace_existing)

        self._worker = _Worker(_task, {})
        self._worker.finished.connect(self._on_apply_done)
        self._worker.checkpoint.connect(self._on_checkpoint)
        self._worker.start()

    def _apply_from_script(self):
        """exec() the current script text in a namespace that includes maxwell."""
        self.log.head("═══ Executing script text ══════════════════════════")
        script_text = self.script_edit.toPlainText()
        maxwell     = self._maxwell
        self._set_busy(True, stop=False)

        def _task(worker, **_):
            ns = {"maxwell": maxwell}
            exec(compile(script_text, "<script_preview>", "exec"), ns)
            return "Script executed successfully."

        self._worker = _Worker(_task, {})
        self._worker.finished.connect(self._on_apply_done)
        self._worker.start()

    def _on_checkpoint(self, step: int):
        self._resume_from = step + 1

    def _on_apply_done(self, success, message):
        self._set_busy(False, stop=False, keep_apply=True)
        if success:
            self.log.ok(f"Apply finished: {message}")
            self._resume_from = 0
            self.btn_resume.setEnabled(False)
            self.lbl_resume.setText("")
        else:
            if message.startswith("STOPPED"):
                self.log.warn(message)
                self.btn_resume.setEnabled(True)
                self.lbl_resume.setText(f"Stopped at step {self._resume_from}  (click Resume)")
            else:
                self.log.error("Apply failed:")
                self.log.dump(message, "ERROR")
                # offer resume even on error so user can retry from checkpoint
                if self._resume_from > 0:
                    self.btn_resume.setEnabled(True)
                    self.lbl_resume.setText(f"Last good step: {self._resume_from-1}  (click Resume)")

    # =========================================================================
    # Stop / Resume
    # =========================================================================

    def _on_stop(self):
        if self._worker and self._worker.isRunning():
            self._worker.request_stop()
            self.log.warn("Stop requested — will halt after current step …")
            self.btn_stop.setEnabled(False)

    def _on_resume(self):
        if self._maxwell is None:
            self.log.warn("Not connected — cannot resume.")
            return
        self.btn_resume.setEnabled(False)
        self.lbl_resume.setText("")
        self._apply_from_table(resume_from=self._resume_from)

    # =========================================================================
    # Save script
    # =========================================================================

    def _save_script(self):
        fn, _ = QFileDialog.getSaveFileName(
            self, "Save pyaedt Script",
            f"{self.le_project.text().strip() or 'winding'}_winding.py",
            "Python Script (*.py);;All Files (*)",
        )
        if not fn:
            return
        if not fn.endswith(".py"):
            fn += ".py"
        try:
            with open(fn, "w", encoding="utf-8") as fh:
                fh.write(self.script_edit.toPlainText())
            self.log.ok(f"Script saved → {fn}")
        except Exception as exc:
            self.log.error(f"Save failed: {exc}")

    # =========================================================================
    # Helpers
    # =========================================================================

    def _set_busy(self, busy: bool, stop: bool = False, keep_apply: bool = False):
        self.btn_connect.setEnabled(not busy)
        self.btn_disconnect.setEnabled(not busy and self._maxwell is not None)
        self.btn_stop.setEnabled(busy and stop)
        if not keep_apply:
            self.btn_apply.setEnabled(not busy and self._maxwell is not None)
