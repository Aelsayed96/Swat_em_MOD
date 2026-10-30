**SWAT-EM – ANSYS Maxwell Winding Export** (v0.1.0)

A dialog inside SWAT-EM that turns a designed winding into a ready-to-use winding setup in ANSYS Maxwell, with no manual coil-by-coil work.

- **Connects** to a running AEDT session (2D or 3D design, detected automatically) and lets you pick the project, design and AEDT version.
- **Creates** the turns design variables per phase and layer, the coil assignments, the per-phase windings, and links the coils into each winding.
- **Naming rules you control:** the coil name (default `{phase}_{layer}_{coil}`) and the assignment object (default `Layer{layer}{ci}`) are written as templates, with a live preview, a 0/1 coil-numbering toggle and custom layer labels.
- **Model symmetry:** one list offers the full machine, 1/N (even) models, and 1/2N (odd) models, showing only the options valid for the winding.
- **Options:** CW/CCW direction and target EMF toggles, open or closed coil, and replacing boundaries that already exist so re-applying works.
- **Checks before assigning:** it lists missing geometry objects with similar-name suggestions and gives hints on failures.
- **Control:** every table cell is editable, with a live generated Python script, Stop and Resume buttons, and a step delay.
