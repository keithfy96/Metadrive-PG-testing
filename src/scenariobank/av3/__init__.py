"""The AV3 port: the converter's `tools/` half that meets the model, as package modules.

Phase 4 Step 6 ports the rig half, `camera_rig`; Step 7 ports the model and the openpilot bridge
beside it. They live here rather than under `tools/` because this repo runs one interpreter --
MetaDrive and the model on one Python 3.10 -- so nothing needs the path-inserted imports and
file exchange the converter used to cross from its 3.8 to its 3.10. Nothing in this package is
imported by the runner unless a run asks for a rig or the AV3 policy; a machine without the
simulator still imports every module here.
"""
