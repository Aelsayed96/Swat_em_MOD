# -*- coding: utf-8 -*-
"""
Compare two winding configurations side by side.

Both sides are selected from the windings already loaded in the current
SWAT-EM project (the same list shown in the main-window sidebar).
No generate-from-parameters panel; no file-load button.

A summary table lists key scalar characteristics of both windings with
differing values highlighted, and tabs show:
  • Winding-factor overlay (both on one axis)
  • Winding factor side by side
  • MMF side by side
  • Star of slots side by side
"""

import numpy as np
import pyqtgraph as pg

from qtpy.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QComboBox, QPushButton, QMessageBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QWidget, QHeaderView, QSplitter,
)
from qtpy.QtCore import Qt
from qtpy.QtGui import QFont, QColor
from qtpy import QtCore

from swat_em import plots


_COLOR_A        = "#2f7ed8"          # blue
_COLOR_B        = "#d8542f"          # orange
_HIGHLIGHT_DIFF = QColor("#fff2b2")  # amber


# ─────────────────────────────────────────────────────────────────────────────
# Overlay winding-factor chart
# ─────────────────────────────────────────────────────────────────────────────

class _OverlayWindingFactor:
    """Side-by-side bar chart of kw spectra for both configs (phase 1)."""

    def __init__(self, layout):
        self.fig = pg.PlotWidget()
        layout.addWidget(self.fig)
        self.fig.setLabel("bottom", "<div>Harmonic order ν</div>")
        self.fig.setLabel("left",   "<div>Winding factor kw  (phase 1)</div>")
        self.fig.showGrid(x=True, y=True)
        self.fig.setLimits(xMin=0, yMin=0)
        self.leg = self.fig.addLegend(offset=(-10, 10))

    def plot(self, data_a, data_b, name_a="A", name_b="B"):
        self.fig.clear()
        try:
            self.leg.clear()
        except Exception:
            pass

        nu_a = np.array(data_a.results["nu_el"])
        kw_a = np.abs(np.array(data_a.results["kw_el"])[:, 0])
        nu_b = np.array(data_b.results["nu_el"])
        kw_b = np.abs(np.array(data_b.results["kw_el"])[:, 0])

        if len(nu_a) == 0 or len(nu_b) == 0:
            return

        dx    = min(nu_a[1] - nu_a[0] if len(nu_a) > 1 else 1,
                    nu_b[1] - nu_b[0] if len(nu_b) > 1 else 1)
        width = dx / 3.0

        self.fig.addItem(pg.BarGraphItem(
            x=nu_a - width / 2, height=kw_a, width=width,
            brush=_COLOR_A, pen=pg.mkPen(width=0),
        ))
        self.fig.addItem(pg.BarGraphItem(
            x=nu_b + width / 2, height=kw_b, width=width,
            brush=_COLOR_B, pen=pg.mkPen(width=0),
        ))
        # invisible curves for legend entries
        self.fig.addItem(pg.PlotCurveItem(nu_a, kw_a, name=name_a,
                                          pen=pg.mkPen(_COLOR_A)))
        self.fig.addItem(pg.PlotCurveItem(nu_b, kw_b, name=name_b,
                                          pen=pg.mkPen(_COLOR_B)))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(v):
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


# ─────────────────────────────────────────────────────────────────────────────
# Dialog
# ─────────────────────────────────────────────────────────────────────────────

class CompareWindingsDialog(QDialog):
    """
    Parameters
    ----------
    project  : swat_em.project  – the live project object (gives us all models)
    data_a   : datamodel        – pre-select for slot A  (optional)
    data_b   : datamodel        – pre-select for slot B  (optional)
    """

    def __init__(self, parent=None, project=None, data_a=None, data_b=None):
        super().__init__(parent)
        self.setWindowTitle("Compare Winding Configurations")
        self.resize(1300, 820)

        self._project = project   # may be None (handled gracefully)
        self._models  = []        # list of datamodel instances
        self._titles  = []        # matching display names

        self._load_models()
        self._build_ui()

        # Pre-select A / B if supplied
        self._preselect(self.cb_a, data_a)
        self._preselect(self.cb_b, data_b)

        # Auto-compare if both are pre-selected
        if data_a is not None and data_b is not None:
            self._on_compare()

    # ── Load available models ─────────────────────────────────────────────────

    def _load_models(self):
        """Pull all models from the project into self._models / self._titles."""
        self._models = []
        self._titles = []
        if self._project is None:
            return
        try:
            n = len(self._project.get_titles())
            for i in range(n):
                m = self._project.get_model_by_index(i)
                t = self._project.get_titles()[i] or f"Winding {i+1}"
                self._models.append(m)
                self._titles.append(t)
        except Exception:
            pass

    def _preselect(self, combo: QComboBox, data):
        """Select the combo entry whose model is `data` (by identity)."""
        if data is None:
            return
        for i, m in enumerate(self._models):
            if m is data:
                combo.setCurrentIndex(i)
                return

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # ── selector row ─────────────────────────────────────────────────────
        sel_grp = QGroupBox("Select windings to compare")
        sel_h   = QHBoxLayout(sel_grp)

        # A
        grp_a = QGroupBox("Winding A")
        grp_a.setStyleSheet(f"QGroupBox{{color:{_COLOR_A};font-weight:bold;}}")
        v_a = QVBoxLayout(grp_a)
        self.cb_a = QComboBox()
        self.cb_a.addItems(self._titles)
        self.lbl_a = QLabel("—")
        self.lbl_a.setStyleSheet("color:#888; font-style:italic;")
        v_a.addWidget(self.cb_a)
        v_a.addWidget(self.lbl_a)
        self.cb_a.currentIndexChanged.connect(lambda i: self._update_label(self.lbl_a, i))
        sel_h.addWidget(grp_a)

        # B
        grp_b = QGroupBox("Winding B")
        grp_b.setStyleSheet(f"QGroupBox{{color:{_COLOR_B};font-weight:bold;}}")
        v_b = QVBoxLayout(grp_b)
        self.cb_b = QComboBox()
        self.cb_b.addItems(self._titles)
        # default: pick second winding for B if available
        if len(self._titles) > 1:
            self.cb_b.setCurrentIndex(1)
        self.lbl_b = QLabel("—")
        self.lbl_b.setStyleSheet("color:#888; font-style:italic;")
        v_b.addWidget(self.cb_b)
        v_b.addWidget(self.lbl_b)
        self.cb_b.currentIndexChanged.connect(lambda i: self._update_label(self.lbl_b, i))
        sel_h.addWidget(grp_b)

        root.addWidget(sel_grp)

        # Initialise labels
        self._update_label(self.lbl_a, self.cb_a.currentIndex())
        self._update_label(self.lbl_b, self.cb_b.currentIndex())

        # ── Compare / Close buttons ───────────────────────────────────────────
        btn_row = QHBoxLayout()
        self.btn_compare = QPushButton("⇄  Compare")
        self.btn_compare.setDefault(True)
        self.btn_compare.clicked.connect(self._on_compare)
        btn_row.addWidget(self.btn_compare)
        btn_row.addStretch()
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.reject)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

        # ── Splitter: summary table  |  plot tabs ────────────────────────────
        splitter = QSplitter(QtCore.Qt.Vertical)
        root.addWidget(splitter, stretch=1)

        # Summary table
        self.summary_table = QTableWidget()
        self.summary_table.setColumnCount(3)
        self.summary_table.setHorizontalHeaderLabels(["Metric", "A", "B"])
        self.summary_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.summary_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.summary_table.setAlternatingRowColors(True)
        splitter.addWidget(self.summary_table)

        # Plot tabs
        self.tabs = QTabWidget()
        splitter.addWidget(self.tabs)
        splitter.setSizes([200, 580])

        # Tab 1 – overlay winding factor
        tab_ov = QWidget()
        self.overlay_wf = _OverlayWindingFactor(QVBoxLayout(tab_ov))
        self.tabs.addTab(tab_ov, "Winding factor  (overlay)")

        # Tab 2 – side-by-side winding factor
        tab_wf = QWidget()
        hl_wf  = QHBoxLayout(tab_wf)
        col_a_wf = QVBoxLayout(); col_b_wf = QVBoxLayout()
        self._wf_a = plots._windingfactor(col_a_wf, None, None, None)
        self._wf_b = plots._windingfactor(col_b_wf, None, None, None)
        hl_wf.addLayout(col_a_wf); hl_wf.addLayout(col_b_wf)
        self.tabs.addTab(tab_wf, "Winding factor  (side by side)")

        # Tab 3 – side-by-side MMF
        tab_mmk = QWidget()
        hl_mmk  = QHBoxLayout(tab_mmk)
        col_a_mmk = QVBoxLayout(); col_b_mmk = QVBoxLayout()
        self._mmk_a = plots._mmk(col_a_mmk, None, None, None)
        self._mmk_b = plots._mmk(col_b_mmk, None, None, None)
        hl_mmk.addLayout(col_a_mmk); hl_mmk.addLayout(col_b_mmk)
        self.tabs.addTab(tab_mmk, "MMF  (side by side)")

        # Tab 4 – side-by-side star of slots
        tab_star = QWidget()
        hl_star  = QHBoxLayout(tab_star)
        col_a_star = QVBoxLayout(); col_b_star = QVBoxLayout()
        self._star_a = plots._slot_star(col_a_star, None, None, None)
        self._star_b = plots._slot_star(col_b_star, None, None, None)
        hl_star.addLayout(col_a_star); hl_star.addLayout(col_b_star)
        self.tabs.addTab(tab_star, "Star of slots  (side by side)")

        # Tab 5 – Force & structural comparison
        tab_force = QWidget()
        self._build_force_tab(tab_force)
        self.tabs.addTab(tab_force, "Force & Structural")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _update_label(self, lbl: QLabel, idx: int):
        """Show a one-line summary under the combo for the chosen model."""
        if idx < 0 or idx >= len(self._models):
            lbl.setText("—")
            return
        m = self._models[idx]
        try:
            lbl.setText(
                f"Q={m.get_num_slots()}  "
                f"P={2*m.get_num_polepairs()}  "
                f"m={m.get_num_phases()}  "
                f"layers={m.get_num_layers()}"
            )
        except Exception:
            lbl.setText("(no data)")

    def _get_selected(self):
        """Return (data_a, name_a, data_b, name_b) from the combos."""
        ia = self.cb_a.currentIndex()
        ib = self.cb_b.currentIndex()
        if ia < 0 or ia >= len(self._models):
            return None, "", None, ""
        if ib < 0 or ib >= len(self._models):
            return None, "", None, ""
        return (self._models[ia], self._titles[ia],
                self._models[ib], self._titles[ib])

    # ── Compare ───────────────────────────────────────────────────────────────

    def _build_force_tab(self, parent):
        """
        Force & structural analysis comparison tab.

        Shows:
        ① Radial force mode spectrum overlay (bar chart) — mode order vs amplitude proxy
        ② Key structural metrics table (cogging order, UMP, slot harmonics)
        ③ Guidance notes based on mode 0 presence and UMP risk
        """
        import math
        lv = QVBoxLayout(parent)

        info = QLabel(
            "Radial force mode orders for both windings.  "
            "Mode 0 = spatially uniform force → net radial pull on stator (dangerous).  "
            "Even modes with low order excite stator ovalization.  "
            "Click ⇄ Compare to populate."
        )
        info.setWordWrap(True)
        lv.addWidget(info)

        splitter = QSplitter(Qt.Vertical)
        lv.addWidget(splitter, stretch=1)

        # Mode bar chart
        grp_chart = QGroupBox("Radial force mode orders  (kw² proxy amplitude)")
        gc = QVBoxLayout(grp_chart)
        self._force_plot = pg.PlotWidget()
        self._force_plot.setLabel("bottom", "Mode order r")
        self._force_plot.setLabel("left",   "|kw_r|²  (relative amplitude proxy)")
        self._force_plot.showGrid(x=True, y=True)
        self._force_plot.setLimits(xMin=-1, yMin=0)
        leg = self._force_plot.addLegend(offset=(-10,10))
        self._force_leg = leg
        gc.addWidget(self._force_plot)
        splitter.addWidget(grp_chart)

        # Structural metrics table
        grp_tbl = QGroupBox("Structural metrics")
        gt = QVBoxLayout(grp_tbl)
        self._struct_table = QTableWidget()
        self._struct_table.setColumnCount(3)
        self._struct_table.setHorizontalHeaderLabels(["Metric","A","B"])
        self._struct_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._struct_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._struct_table.setAlternatingRowColors(True)
        gt.addWidget(self._struct_table)
        splitter.addWidget(grp_tbl)

        # Guidance
        self._struct_notes = QLabel("")
        self._struct_notes.setWordWrap(True)
        self._struct_notes.setStyleSheet(
            "QLabel{background:#2b2b2b;color:#e5c07b;padding:6px;border-radius:4px;}")
        lv.addWidget(self._struct_notes)

    def _update_force_tab(self, data_a, data_b, name_a, name_b):
        """
        Populate the Force & Structural tab from two solved datamodels.

        The kw spectrum gives us harmonic orders.  We use |kw_ν|² as a proxy
        for the amplitude of the ν-th radial force density harmonic
        (proportional to B² ∝ kw²).  Force mode order r = ν_a - ν_b for
        all pairs of harmonics from winding A and B (here both are the same
        winding evaluated at different harmonics, so we plot kw_ν² vs ν).
        """
        import math

        self._force_plot.clear()
        try:
            self._force_leg.clear()
        except Exception:
            pass

        def _kw_spectrum(data):
            nu = np.array(data.results.get("nu_el", []))
            kw = np.abs(np.array(data.results.get("kw_el", [[]])))
            if kw.ndim == 2 and kw.shape[1] > 0:
                kw = kw[:, 0]
            else:
                kw = kw.flatten()
            return nu, kw

        nu_a, kw_a = _kw_spectrum(data_a)
        nu_b, kw_b = _kw_spectrum(data_b)

        if len(nu_a) and len(kw_a):
            amp_a = kw_a ** 2
            dx = (nu_a[1] - nu_a[0]) / 3.0 if len(nu_a) > 1 else 0.3
            self._force_plot.addItem(pg.BarGraphItem(
                x=nu_a - dx/2, height=amp_a, width=dx,
                brush=_COLOR_A, pen=pg.mkPen(width=0)))
            self._force_plot.addItem(pg.PlotCurveItem(
                nu_a, amp_a, name=name_a, pen=pg.mkPen(_COLOR_A)))

        if len(nu_b) and len(kw_b):
            amp_b = kw_b ** 2
            dx = (nu_b[1] - nu_b[0]) / 3.0 if len(nu_b) > 1 else 0.3
            self._force_plot.addItem(pg.BarGraphItem(
                x=nu_b + dx/2, height=amp_b, width=dx,
                brush=_COLOR_B, pen=pg.mkPen(width=0)))
            self._force_plot.addItem(pg.PlotCurveItem(
                nu_b, amp_b, name=name_b, pen=pg.mkPen(_COLOR_B)))

        # Structural metrics table
        def _lcm(a, b):
            return abs(a*b)//math.gcd(a,b) if a and b else "n/a"
        def _gcd(a, b):
            return math.gcd(a, b) if a and b else "n/a"
        def _ump(q, p):
            if not q or not p: return "n/a"
            pp = p//2
            if math.gcd(q,pp)==1 and pp%2==1:
                return "⚠ Risk"
            return "Low"
        def _mode0(r):
            if not r: return "n/a"
            try:
                rl = list(r) if hasattr(r,'__iter__') else [r]
                return "⚠ Present" if 0 in rl else "Absent"
            except: return "n/a"

        Qa = data_a.get_num_slots(); Pa = 2*data_a.get_num_polepairs()
        Qb = data_b.get_num_slots(); Pb = 2*data_b.get_num_polepairs()
        bc_a, _ = data_a.get_basic_characteristics()
        bc_b, _ = data_b.get_basic_characteristics()
        ra = bc_a.get("r"); rb = bc_b.get("r")

        pp_a = Pa//2; pp_b = Pb//2

        struct_rows = [
            ("Cogging order  LCM(Q,P)", _lcm(Qa,Pa), _lcm(Qb,Pb)),
            ("Symmetry sections  GCD(Q,P)", _gcd(Qa,Pa), _gcd(Qb,Pb)),
            ("Slot harmonics  ν_slot",
             f"{Qa//pp_a-1} / {Qa//pp_a+1}" if pp_a else "n/a",
             f"{Qb//pp_b-1} / {Qb//pp_b+1}" if pp_b else "n/a"),
            ("Radial force modes  r", _fmt(ra), _fmt(rb)),
            ("Mode-0 force", _mode0(ra), _mode0(rb)),
            ("UMP risk", _ump(Qa,Pa), _ump(Qb,Pb)),
        ]

        bold = QFont(); bold.setBold(True)
        _HIGHLIGHT_WARN = QColor("#5c3a00")

        self._struct_table.setRowCount(len(struct_rows))
        for i,(p,va,vb) in enumerate(struct_rows):
            pi = QTableWidgetItem(p); pi.setFont(bold)
            self._struct_table.setItem(i,0,pi)
            ia = QTableWidgetItem(str(va)); ib = QTableWidgetItem(str(vb))
            if str(va) != str(vb):
                ia.setBackground(_HIGHLIGHT_DIFF); ib.setBackground(_HIGHLIGHT_DIFF)
            if "⚠" in str(va): ia.setBackground(_HIGHLIGHT_WARN)
            if "⚠" in str(vb): ib.setBackground(_HIGHLIGHT_WARN)
            self._struct_table.setItem(i,1,ia)
            self._struct_table.setItem(i,2,ib)
        self._struct_table.resizeRowsToContents()

        # Guidance notes
        notes = []
        for label, val, name in [("A",ra,name_a),("B",rb,name_b)]:
            try:
                rl = list(val) if hasattr(val,'__iter__') else [val]
                if 0 in rl:
                    notes.append(
                        f"⚠  {name} (Config {label}): Mode 0 present — "
                        "spatially uniform radial force → potential net pull "
                        "on stator bore. Consider choosing Q/P with GCD > 1."
                    )
                low_modes = [r for r in rl if 0 < abs(r) <= 4]
                if low_modes:
                    notes.append(
                        f"ℹ  {name}: Low-order modes {low_modes} → "
                        "stator ovalization risk. Check stator ring stiffness."
                    )
            except: pass
        for d, Q, P, n in [("A",Qa,Pa,name_a),("B",Qb,Pb,name_b)]:
            if _ump(Q,P) != "Low":
                notes.append(
                    f"⚠  {n} (Config {d}): UMP risk — GCD(Q,P/2)=1 with odd P/2. "
                    "Unbalanced magnetic pull may cause bearing wear."
                )
            cog = _lcm(Q,P)
            if cog and isinstance(cog,int) and cog < 24:
                notes.append(
                    f"ℹ  {n}: Low cogging order {cog} → visible cogging torque ripple. "
                    "Consider skewing or fractional-slot winding."
                )
        self._struct_notes.setText(
            "\n".join(notes) if notes else "✔  No critical structural issues detected."
        )

    def _on_compare(self):
        if not self._models:
            QMessageBox.warning(
                self, "No windings",
                "No windings are loaded in the project.\n"
                "Generate or load a winding first, then re-open Compare."
            )
            return

        data_a, name_a, data_b, name_b = self._get_selected()

        if data_a is None or data_b is None:
            QMessageBox.warning(self, "Select windings",
                                "Please select a winding for both A and B.")
            return

        self.summary_table.setHorizontalHeaderLabels(["Metric", name_a, name_b])
        self._fill_summary(data_a, data_b)

        self.overlay_wf.plot(data_a, data_b, name_a, name_b)
        self._wf_a.plot(data_a)
        self._wf_b.plot(data_b)
        self._mmk_a.plot(data_a)
        self._mmk_b.plot(data_b)
        self._star_a.plot(data_a)
        self._star_b.plot(data_b)
        self._update_force_tab(data_a, data_b, name_a, name_b)

    def _fill_summary(self, data_a, data_b):
        bc_a, _ = data_a.get_basic_characteristics()
        bc_b, _ = data_b.get_basic_characteristics()

        kw1_a = bc_a.get("kw1")
        kw1_b = bc_b.get("kw1")
        kw1_a_val = round(float(np.abs(kw1_a[0])), 4) if kw1_a else "n/a"
        kw1_b_val = round(float(np.abs(kw1_b[0])), 4) if kw1_b else "n/a"

        def _sd(bc):
            v = bc.get("sigma_d")
            return round(float(v), 4) if v is not None else "n/a"

        # ── Force & structural analysis metrics ─────────────────────────────
        def _lcm(a, b):
            import math
            return abs(a*b)//math.gcd(a,b) if a and b else "n/a"
        def _gcd(a, b):
            import math
            return math.gcd(a, b) if a and b else "n/a"
        def _ump(q, p):
            """Unbalanced magnetic pull risk: occurs when GCD(Q,P/2)=1 and p/2 is odd."""
            import math
            if not q or not p: return "n/a"
            pp = p // 2
            if math.gcd(q, pp) == 1 and pp % 2 == 1:
                return "⚠ Risk (GCD=1, odd p)"
            return "Low"
        def _cog_order(q, p):
            return _lcm(q, p)
        def _slot_harm(q, p):
            if not q or not p: return "n/a"
            pp = p // 2
            return f"ν={q//pp - 1}  and  ν={q//pp + 1}" if pp else "n/a"
        def _radial_mode_0(r):
            """Check if r contains mode 0 (rotating force, dangerous for stator)."""
            if not r: return "n/a"
            try:
                r_list = list(r) if hasattr(r,'__iter__') else [r]
                return "⚠ Mode 0 present" if 0 in r_list else "OK"
            except: return "n/a"

        Qa = data_a.get_num_slots(); Pa = 2*data_a.get_num_polepairs()
        Qb = data_b.get_num_slots(); Pb = 2*data_b.get_num_polepairs()
        ra = bc_a.get("r"); rb = bc_b.get("r")

        rows = [
            ("Slots Q",                    Qa, Qb),
            ("Poles P",                    Pa, Pb),
            ("Phases m",                   data_a.get_num_phases(), data_b.get_num_phases()),
            ("Layers",                     data_a.get_num_layers(), data_b.get_num_layers()),
            ("Coil span w",                data_a.get_coilspan(), data_b.get_coilspan()),
            ("Slots / pole / phase  q",    bc_a.get("q"), bc_b.get("q")),
            ("Winding factor kw1 (ph 1)",  kw1_a_val, kw1_b_val),
            ("Symmetric",                  bc_a.get("sym"), bc_b.get("sym")),
            ("Periodicity a",              bc_a.get("a"), bc_b.get("a")),
            ("Periodicity t",              bc_a.get("t"), bc_b.get("t")),
            ("Double-linked leakage σd",   _sd(bc_a), _sd(bc_b)),
            # ── Force & structural ──────────────────────────────────────────
            ("── Force & Structural ──", "", ""),
            ("Radial force modes r",       _fmt(ra), _fmt(rb)),
            ("Mode-0 force risk",          _radial_mode_0(ra), _radial_mode_0(rb)),
            ("Cogging torque order  LCM(Q,P)", _cog_order(Qa,Pa), _cog_order(Qb,Pb)),
            ("Symmetry sections  GCD(Q,P)",    _gcd(Qa,Pa), _gcd(Qb,Pb)),
            ("Slot harmonics  ν_slot",     _slot_harm(Qa,Pa), _slot_harm(Qb,Pb)),
            ("UMP risk",                   _ump(Qa,Pa), _ump(Qb,Pb)),
        ]

        bold = QFont(); bold.setBold(True)
        self.summary_table.setRowCount(len(rows))
        for i, (label, va, vb) in enumerate(rows):
            lbl_item = QTableWidgetItem(label)
            lbl_item.setFont(bold)
            self.summary_table.setItem(i, 0, lbl_item)

            item_a = QTableWidgetItem(_fmt(va))
            item_b = QTableWidgetItem(_fmt(vb))
            self.summary_table.setItem(i, 1, item_a)
            self.summary_table.setItem(i, 2, item_b)

            if _fmt(va) != _fmt(vb):
                item_a.setBackground(_HIGHLIGHT_DIFF)
                item_b.setBackground(_HIGHLIGHT_DIFF)

        self.summary_table.resizeRowsToContents()
