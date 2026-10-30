import sys
from unittest.mock import MagicMock

for _m in ["qtpy", "qtpy.QtWidgets", "qtpy.QtCore", "qtpy.QtGui"]:
    sys.modules.setdefault(_m, MagicMock())
sys.modules["qtpy.QtCore"].QThread = object
sys.modules["qtpy.QtCore"].QObject = object
sys.modules["qtpy.QtWidgets"].QDialog = object

from swat_em.dialog_ansys_export import _format_assignment as fa  # noqa: E402

KW = dict(layer="1", n=1, slot=5, phase="A", direction="+",
          polarity="Positive", name="x")


def _t(tmpl, copy, start=1):
    return fa(tmpl, copy=copy, coil_start=start, **KW)


def test_nothing_is_appended():
    assert _t("Layer{layer}", 1) == ("Layer1", "")
    assert _t("Layer{layer}{ci}", 0) == ("Layer1_0", "")
    assert _t("Layer{layer}{ci}", 1) == ("Layer1_0_1", "")
    assert _t("Layer{layer}{ci}", 2) == ("Layer1_0_2", "")


def test_coil_start_toggle():
    assert _t("{phase}_{layer}_{coil}", 0, start=1) == ("A_1_1", "")
    assert _t("{phase}_{layer}_{coil}", 0, start=0) == ("A_1_0", "")
    assert _t("L{layer}_{copy}", 1, start=1) == ("L1_1", "")


def test_slot_rule_and_errors():
    assert _t("Coil{layer}_{slot}", 3) == ("Coil1_5", "")
    assert _t("L{bad}", 0)[1]
    assert _t("", 0)[1]


def test_connect_picks_class_from_design_type():
    import types
    calls = []

    class _Base:
        def __init__(self, **kw):
            calls.append((type(self).__name__, kw))
            self.aedt_version_id = "2026.1"
            self.project_name = kw["project"]
            self.design_name = kw["design"]

    class Maxwell2d(_Base):
        pass

    class Maxwell3d(_Base):
        def __init__(self, **kw):
            if kw["design"] == "twoD":
                raise ValueError("Specified design is not of type Maxwell 3D.")
            super().__init__(**kw)

    mod = types.ModuleType("ansys.aedt.core")
    mod.Maxwell2d, mod.Maxwell3d = Maxwell2d, Maxwell3d
    pkg = types.ModuleType("ansys"); aedt = types.ModuleType("ansys.aedt")
    sys.modules.update({"ansys": pkg, "ansys.aedt": aedt, "ansys.aedt.core": mod})
    from swat_em.dialog_ansys_export import _pyaedt_connect

    class Log:
        def info(self, m): pass
        ok = warn = error = info

    # known 2D design: Maxwell2d directly, no solution_type passed
    mx = _pyaedt_connect("P", "twoD", "Transient", False, False, Log(),
                         design_types={"twoD": "Maxwell 2D"})
    assert type(mx).__name__ == "Maxwell2d" and "solution_type" not in calls[-1][1]
    # unknown type: falls back from 3D to 2D
    mx = _pyaedt_connect("P", "twoD", "Transient", False, False, Log())
    assert type(mx).__name__ == "Maxwell2d"
    mx = _pyaedt_connect("P", "threeD", "Transient", False, False, Log(),
                         design_types={"threeD": "Maxwell 3D"})
    assert type(mx).__name__ == "Maxwell3d"
