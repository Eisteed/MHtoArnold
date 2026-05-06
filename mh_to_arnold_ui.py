"""
mh_to_arnold_ui.py
==================
Maya UI panel that launches and tweaks the full MetaHuman → Arnold pipeline.

Drives:
    mh_head_to_arnold.py
    mh_body_to_arnold.py
    mh_eyeLeft_to_arnold.py
    mh_eyeRight_to_arnold.py
    mh_teeth_to_arnold.py

USAGE
-----
Place all 5 mh_*_to_arnold.py files + this file in your Maya scripts folder
(e.g. C:/Users/<you>/Documents/maya/2024/scripts), then in Maya's script
editor (Python tab):

    import mh_to_arnold_ui as ui
    ui.show()

To pick up edits to the converters or this UI without restarting Maya:

    import importlib, mh_to_arnold_ui as ui
    importlib.reload(ui)
    ui.show()

DESIGN
------
- Pure maya.cmds (no PyQt) — works on any Maya version, no extra deps.
- Lazy-imports each converter module so missing files don't break the UI.
- Reads/writes attributes on the converted aiStandardSurface shaders.
- Sliders update attributes live; viewport reflects changes immediately
  (especially in IPR / Arnold RenderView).
"""

import maya.cmds as cmds
import importlib
import os
import sys


# ============================================================================
# SELF-BOOTSTRAP — make sibling .py converters importable regardless of where
# this file lives (e.g. `scripts/MHtoArnold/` instead of `scripts/`).
# ============================================================================
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
    print("[mh-ui] Added to sys.path: {}".format(_THIS_DIR))


# ============================================================================
# CONFIG — shader names this UI controls
# ============================================================================

SHADERS = {
    "head":     {"module": "mh_head_to_arnold",     "ai": "head_aiStandardSurface",
                 "dx11": "shader_head_shader",      "has_wrinkles": True},
    "body":     {"module": "mh_body_to_arnold",     "ai": "body_aiStandardSurface",
                 "dx11": "shader_body_shader",      "has_wrinkles": True},
    "eyeLeft":  {"module": "mh_eyeLeft_to_arnold",  "ai": "eyeLeft_aiStandardSurface",
                 "dx11": "shader_eyeLeft_shader",   "has_wrinkles": False},
    "eyeRight": {"module": "mh_eyeRight_to_arnold", "ai": "eyeRight_aiStandardSurface",
                 "dx11": "shader_eyeRight_shader",  "has_wrinkles": False},
    "teeth":    {"module": "mh_teeth_to_arnold",    "ai": "teeth_aiStandardSurface",
                 "dx11": "shader_teeth_shader",     "has_wrinkles": False},
}

WINDOW_NAME = "mhToArnoldWindow"
WINDOW_TITLE = "MetaHuman → Arnold Pipeline"


# ============================================================================
# UTILITIES
# ============================================================================

def _import(module_name):
    """Import (or reload) a converter module, returning None on failure."""
    try:
        mod = importlib.import_module(module_name)
        importlib.reload(mod)
        return mod
    except Exception as e:
        cmds.warning("[mh-ui] Could not import {}: {}".format(module_name, e))
        return None


def _dx11_exists(part):
    """Tolerant check: look up the configured dx11 name plus common variants."""
    info = SHADERS[part]
    dx = info["dx11"]
    candidates = [dx, dx.replace("_shader", ""), dx + "_shader"]
    # Dedupe while preserving order
    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        if cmds.objExists(c):
            return c
    return None


def _shader_exists(part):
    return cmds.objExists(SHADERS[part]["ai"])


def _set_if(shader, attr, value, vec3=False):
    """Idempotent setAttr: skip if shader doesn't exist or attr is connected
    to something we shouldn't override (rare for shading params)."""
    if not cmds.objExists(shader):
        return
    plug = shader + "." + attr
    if not cmds.attributeQuery(attr, node=shader, exists=True):
        return
    try:
        if vec3:
            cmds.setAttr(plug, value[0], value[1], value[2], type="double3")
        else:
            cmds.setAttr(plug, value)
    except Exception as e:
        cmds.warning("[mh-ui] setAttr {} failed: {}".format(plug, e))


def _get_attr(shader, attr, default=0.0):
    if not cmds.objExists(shader):
        return default
    if not cmds.attributeQuery(attr, node=shader, exists=True):
        return default
    try:
        return cmds.getAttr(shader + "." + attr)
    except Exception:
        return default


# ============================================================================
# CONVERSION ACTIONS
# ============================================================================

def _convert(part):
    info = SHADERS[part]
    if not _dx11_exists(part):
        cmds.warning("[mh-ui] DX11 shader not found for '{}' (tried '{}' and variants).".format(
            part, info["dx11"]))
        return
    mod = _import(info["module"])
    if mod is None:
        return
    try:
        mod.convert()
        print("[mh-ui] Converted '{}'.".format(part))
    except Exception as e:
        cmds.error("[mh-ui] convert() for '{}' failed: {}".format(part, e))
    _refresh_status()


def _convert_all():
    for part in SHADERS:
        if _dx11_exists(part):
            _convert(part)


def _revert(part):
    info = SHADERS[part]
    mod = _import(info["module"])
    if mod is None:
        return
    try:
        mod.revert_to_dx11()
        print("[mh-ui] Reverted '{}' to DX11.".format(part))
    except Exception as e:
        cmds.warning("[mh-ui] revert for '{}': {}".format(part, e))


def _revert_all():
    for part in SHADERS:
        if _dx11_exists(part):
            _revert(part)


# ============================================================================
# SKIN (head + body) — SSS / SPEC / COAT
# ============================================================================

def _on_skin_change(part, *_):
    """Called whenever a slider in the skin tab changes."""
    info = SHADERS[part]
    s = info["ai"]
    if not cmds.objExists(s):
        return

    sss_w   = cmds.floatSliderGrp("sl_{}_sssW".format(part),   q=True, v=True)
    sss_s   = cmds.floatSliderGrp("sl_{}_sssScale".format(part), q=True, v=True)
    rad_r   = cmds.floatSliderGrp("sl_{}_sssRadR".format(part), q=True, v=True)
    rad_g   = cmds.floatSliderGrp("sl_{}_sssRadG".format(part), q=True, v=True)
    rad_b   = cmds.floatSliderGrp("sl_{}_sssRadB".format(part), q=True, v=True)
    spec_w  = cmds.floatSliderGrp("sl_{}_spec".format(part),    q=True, v=True)
    spec_r  = cmds.floatSliderGrp("sl_{}_specRough".format(part), q=True, v=True)
    spec_i  = cmds.floatSliderGrp("sl_{}_specIOR".format(part), q=True, v=True)
    coat_w  = cmds.floatSliderGrp("sl_{}_coat".format(part),    q=True, v=True)
    coat_r  = cmds.floatSliderGrp("sl_{}_coatRough".format(part), q=True, v=True)

    _set_if(s, "subsurface",         sss_w)
    _set_if(s, "subsurfaceScale",    sss_s)
    _set_if(s, "subsurfaceRadius",   (rad_r, rad_g, rad_b), vec3=True)
    _set_if(s, "specular",           spec_w)
    _set_if(s, "specularRoughness",  spec_r)
    _set_if(s, "specularIOR",        spec_i)
    _set_if(s, "coat",               coat_w)
    _set_if(s, "coatRoughness",      coat_r)


def _reset_skin(part):
    """Restore script defaults for that surface type."""
    s = SHADERS[part]["ai"]
    if not cmds.objExists(s):
        cmds.warning("[mh-ui] {} not converted yet.".format(part))
        return
    # Same presets used by the head/body converters
    _set_if(s, "subsurface",         0.50)
    _set_if(s, "subsurfaceScale",    0.15)
    _set_if(s, "subsurfaceRadius",   (1.0, 0.2, 0.1), vec3=True)
    _set_if(s, "specular",           0.50)
    _set_if(s, "specularRoughness",  0.40)
    _set_if(s, "specularIOR",        1.40)
    _set_if(s, "coat",               0.05)
    _set_if(s, "coatRoughness",      0.30)
    _refresh_skin_sliders(part)


def _refresh_skin_sliders(part):
    """Pull current attribute values into the sliders."""
    s = SHADERS[part]["ai"]
    if not cmds.objExists(s):
        return
    sliders = {
        "sssW":      ("subsurface",        None),
        "sssScale":  ("subsurfaceScale",   None),
        "sssRadR":   ("subsurfaceRadius",  0),
        "sssRadG":   ("subsurfaceRadius",  1),
        "sssRadB":   ("subsurfaceRadius",  2),
        "spec":      ("specular",          None),
        "specRough": ("specularRoughness", None),
        "specIOR":   ("specularIOR",       None),
        "coat":      ("coat",              None),
        "coatRough": ("coatRoughness",     None),
    }
    for sl_suffix, (attr, idx) in sliders.items():
        sl_name = "sl_{}_{}".format(part, sl_suffix)
        if not cmds.floatSliderGrp(sl_name, exists=True):
            continue
        val = _get_attr(s, attr, 0.0)
        if idx is not None:
            # Vec3 attribute returned as [(r, g, b)]
            if isinstance(val, list) and val and isinstance(val[0], tuple):
                val = val[0][idx]
            elif isinstance(val, (list, tuple)) and len(val) >= 3:
                val = val[idx]
        cmds.floatSliderGrp(sl_name, e=True, v=val)


# ============================================================================
# WRINKLES (head + body)
# ============================================================================

def _on_wrinkle_intensity(part, *_):
    s = SHADERS[part]["ai"]
    if not cmds.objExists(s):
        return
    col = cmds.floatSliderGrp("sl_{}_wColor".format(part), q=True, v=True)
    nrm = cmds.floatSliderGrp("sl_{}_wNormal".format(part), q=True, v=True)
    _set_if(s, "wrinkleColorIntensity",  col)
    _set_if(s, "wrinkleNormalIntensity", nrm)


def _viewport_debug(part, on, channel="color"):
    info = SHADERS[part]
    mod = _import(info["module"])
    if mod is None:
        return
    try:
        if on:
            mod.viewport_debug_on(channel)
        else:
            mod.viewport_debug_off()
    except Exception as e:
        cmds.warning("[mh-ui] viewport debug ({}, {}): {}".format(part, channel, e))


def _diagnose(part, zone_filter=None):
    info = SHADERS[part]
    mod = _import(info["module"])
    if mod is None:
        return
    print("=" * 60)
    print("Diagnose for {}".format(part))
    print("=" * 60)
    try:
        if zone_filter:
            mod.diagnose(zone_filter=zone_filter)
        else:
            mod.diagnose()
    except Exception as e:
        cmds.warning("[mh-ui] diagnose {}: {}".format(part, e))


def _diagnose_from_field(part):
    field = "tx_diag_{}".format(part)
    text = cmds.textFieldGrp(field, q=True, text=True) if cmds.textFieldGrp(field, exists=True) else ""
    text = (text or "").strip() or None
    _diagnose(part, zone_filter=text)


# ============================================================================
# EYE / TEETH PRESETS
# ============================================================================

def _apply_eye_preset(part):
    s = SHADERS[part]["ai"]
    if not cmds.objExists(s):
        cmds.warning("[mh-ui] {} not converted.".format(part))
        return
    spec  = cmds.floatSliderGrp("sl_{}_spec".format(part),     q=True, v=True)
    rough = cmds.floatSliderGrp("sl_{}_specRough".format(part), q=True, v=True)
    ior   = cmds.floatSliderGrp("sl_{}_specIOR".format(part),  q=True, v=True)
    _set_if(s, "specular",          spec)
    _set_if(s, "specularRoughness", rough)
    _set_if(s, "specularIOR",       ior)


def _reset_eye(part):
    s = SHADERS[part]["ai"]
    if not cmds.objExists(s):
        cmds.warning("[mh-ui] {} not converted.".format(part))
        return
    _set_if(s, "subsurface",         0.0)
    _set_if(s, "specular",           1.0)
    _set_if(s, "specularRoughness",  0.15)
    _set_if(s, "specularIOR",        1.376)
    _set_if(s, "coat",               0.0)
    _refresh_eye_sliders(part)


def _refresh_eye_sliders(part):
    s = SHADERS[part]["ai"]
    if not cmds.objExists(s):
        return
    for suffix, attr in [("spec", "specular"),
                         ("specRough", "specularRoughness"),
                         ("specIOR", "specularIOR")]:
        sl = "sl_{}_{}".format(part, suffix)
        if cmds.floatSliderGrp(sl, exists=True):
            cmds.floatSliderGrp(sl, e=True, v=_get_attr(s, attr, 0.0))


def _on_teeth_change(*_):
    s = SHADERS["teeth"]["ai"]
    if not cmds.objExists(s):
        return
    sss   = cmds.floatSliderGrp("sl_teeth_sss",       q=True, v=True)
    spec  = cmds.floatSliderGrp("sl_teeth_spec",      q=True, v=True)
    rough = cmds.floatSliderGrp("sl_teeth_specRough", q=True, v=True)
    coat  = cmds.floatSliderGrp("sl_teeth_coat",      q=True, v=True)
    sss_r = cmds.colorSliderGrp("sl_teeth_sssColor",  q=True, rgb=True)
    _set_if(s, "subsurface",         sss)
    _set_if(s, "subsurfaceColor",    sss_r, vec3=True)
    _set_if(s, "specular",           spec)
    _set_if(s, "specularRoughness",  rough)
    _set_if(s, "coat",               coat)


def _reset_teeth():
    s = SHADERS["teeth"]["ai"]
    if not cmds.objExists(s):
        return
    _set_if(s, "subsurface",         0.30)
    _set_if(s, "subsurfaceColor",    (0.95, 0.85, 0.70), vec3=True)
    _set_if(s, "subsurfaceRadius",   (0.40, 0.30, 0.20), vec3=True)
    _set_if(s, "subsurfaceScale",    0.10)
    _set_if(s, "specular",           0.85)
    _set_if(s, "specularRoughness",  0.20)
    _set_if(s, "specularIOR",        1.55)
    _set_if(s, "coat",               0.10)
    _refresh_teeth_sliders()


def _refresh_teeth_sliders():
    s = SHADERS["teeth"]["ai"]
    if not cmds.objExists(s):
        return
    cmds.floatSliderGrp("sl_teeth_sss",       e=True, v=_get_attr(s, "subsurface", 0))
    cmds.floatSliderGrp("sl_teeth_spec",      e=True, v=_get_attr(s, "specular", 0))
    cmds.floatSliderGrp("sl_teeth_specRough", e=True, v=_get_attr(s, "specularRoughness", 0))
    cmds.floatSliderGrp("sl_teeth_coat",      e=True, v=_get_attr(s, "coat", 0))
    col = _get_attr(s, "subsurfaceColor", (1, 1, 1))
    if isinstance(col, list) and col and isinstance(col[0], tuple):
        col = col[0]
    cmds.colorSliderGrp("sl_teeth_sssColor", e=True, rgb=col)


# ============================================================================
# STATUS REFRESH
# ============================================================================

def _refresh_status():
    """Update the status label of each part."""
    for part in SHADERS:
        lbl = "lbl_status_{}".format(part)
        if not cmds.text(lbl, exists=True):
            continue
        if _shader_exists(part):
            cmds.text(lbl, e=True, label="✅ converted",
                      backgroundColor=(0.30, 0.55, 0.30))
        elif _dx11_exists(part):
            cmds.text(lbl, e=True, label="⏳ DX11 only",
                      backgroundColor=(0.55, 0.45, 0.20))
        else:
            cmds.text(lbl, e=True, label="✗ not in scene",
                      backgroundColor=(0.40, 0.30, 0.30))
    # Refresh sliders to current values
    for part in ("head", "body"):
        if _shader_exists(part):
            _refresh_skin_sliders(part)
    for part in ("eyeLeft", "eyeRight"):
        if _shader_exists(part):
            _refresh_eye_sliders(part)
    if _shader_exists("teeth"):
        _refresh_teeth_sliders()


# ============================================================================
# UI BUILDING
# ============================================================================

def _build_conversion_section():
    cmds.frameLayout(label="1. CONVERSION", marginHeight=8, marginWidth=8,
                     collapsable=True, collapse=False)
    cmds.columnLayout(adj=True, rowSpacing=4)

    cmds.rowLayout(numberOfColumns=2, columnWidth2=(180, 280),
                   columnAttach=[(1, "both", 4), (2, "both", 4)])
    cmds.button(label="▶  Convert ALL (head + body + eyes + teeth)",
                bgc=(0.25, 0.45, 0.65), height=34, command=lambda *_: _convert_all())
    cmds.button(label="↺  Revert ALL to DX11",
                bgc=(0.55, 0.40, 0.25), height=34, command=lambda *_: _revert_all())
    cmds.setParent("..")

    cmds.separator(height=8, style="in")

    # Per-shader row: status | convert button | revert button
    cmds.rowColumnLayout(numberOfColumns=4,
                         columnWidth=[(1, 80), (2, 130), (3, 130), (4, 130)],
                         columnAttach=[(1, "left", 4), (2, "both", 2),
                                        (3, "both", 2), (4, "both", 2)])
    cmds.text(label="")
    cmds.text(label="Status", font="boldLabelFont")
    cmds.text(label="Convert", font="boldLabelFont")
    cmds.text(label="Revert", font="boldLabelFont")
    for part in SHADERS:
        cmds.text(label=part)
        cmds.text("lbl_status_{}".format(part), label="…")
        cmds.button(label="convert", c=lambda x, p=part: _convert(p))
        cmds.button(label="revert",  c=lambda x, p=part: _revert(p))
    cmds.setParent("..")
    cmds.setParent("..")
    cmds.setParent("..")


def _slider_row(name, label, mn, mx, default, change_cmd, fieldwidth=70):
    """Helper to create a consistent slider row."""
    cmds.floatSliderGrp(name, label=label, field=True, value=default,
                        minValue=mn, maxValue=mx,
                        precision=3, columnWidth=[(1, 130), (2, fieldwidth), (3, 220)],
                        cc=change_cmd, dc=change_cmd)


def _build_skin_section(part):
    """Skin tweaks for head or body (same template, separate state)."""
    cmds.frameLayout(label="{} skin tweaks".format(part).upper(),
                     marginHeight=6, marginWidth=8,
                     collapsable=True, collapse=False)
    cmds.columnLayout(adj=True, rowSpacing=2)

    cb = lambda *_, p=part: _on_skin_change(p)

    cmds.text(label="Subsurface scattering", align="left", font="boldLabelFont")
    _slider_row("sl_{}_sssW".format(part),     "  weight",    0.0,  1.0, 0.50, cb)
    _slider_row("sl_{}_sssScale".format(part), "  scale (cm)", 0.0,  1.0, 0.15, cb)
    _slider_row("sl_{}_sssRadR".format(part),  "  radius R",  0.0,  3.0, 1.00, cb)
    _slider_row("sl_{}_sssRadG".format(part),  "  radius G",  0.0,  3.0, 0.20, cb)
    _slider_row("sl_{}_sssRadB".format(part),  "  radius B",  0.0,  3.0, 0.10, cb)

    cmds.separator(height=4, style="none")
    cmds.text(label="Specular", align="left", font="boldLabelFont")
    _slider_row("sl_{}_spec".format(part),      "  weight",    0.0,  2.0, 0.50, cb)
    _slider_row("sl_{}_specRough".format(part), "  roughness", 0.01, 1.0, 0.40, cb)
    _slider_row("sl_{}_specIOR".format(part),   "  IOR",       1.0,  3.0, 1.40, cb)

    cmds.separator(height=4, style="none")
    cmds.text(label="Coat (sebum sheen)", align="left", font="boldLabelFont")
    _slider_row("sl_{}_coat".format(part),      "  weight",    0.0,  1.0, 0.05, cb)
    _slider_row("sl_{}_coatRough".format(part), "  roughness", 0.01, 1.0, 0.30, cb)

    cmds.separator(height=6, style="none")
    cmds.button(label="↺ Reset {} skin to defaults".format(part),
                c=lambda x, p=part: _reset_skin(p))

    cmds.setParent("..")
    cmds.setParent("..")


def _build_wrinkles_section(part):
    """Wrinkle debug controls for head or body."""
    cmds.frameLayout(label="{} wrinkles".format(part).upper(),
                     marginHeight=6, marginWidth=8,
                     collapsable=True, collapse=False)
    cmds.columnLayout(adj=True, rowSpacing=4)

    cb = lambda *_, p=part: _on_wrinkle_intensity(p)
    _slider_row("sl_{}_wColor".format(part),  "color intensity",  0.0, 20.0, 1.0, cb)
    _slider_row("sl_{}_wNormal".format(part), "normal intensity", 0.0, 20.0, 1.0, cb)

    cmds.separator(height=4, style="none")
    cmds.text(label="Viewport debug (unlit surfaceShader)", align="left",
              font="boldLabelFont")
    cmds.rowLayout(numberOfColumns=4, columnWidth4=(115, 115, 115, 130),
                   columnAttach=[(1, "both", 2), (2, "both", 2),
                                  (3, "both", 2), (4, "both", 2)])
    cmds.button(label="DBG color",  c=lambda x, p=part: _viewport_debug(p, True, "color"))
    cmds.button(label="DBG normal", c=lambda x, p=part: _viewport_debug(p, True, "normal"))
    cmds.button(label="DBG OFF",    c=lambda x, p=part: _viewport_debug(p, False))
    cmds.button(label="↺ intensity = 1",
                c=lambda x, p=part: (cmds.floatSliderGrp("sl_{}_wColor".format(p),  e=True, v=1.0),
                                     cmds.floatSliderGrp("sl_{}_wNormal".format(p), e=True, v=1.0),
                                     _on_wrinkle_intensity(p)))
    cmds.setParent("..")

    cmds.separator(height=4, style="none")
    cmds.text(label="Diagnose mapping (prints to script editor)", align="left",
              font="boldLabelFont")
    cmds.textFieldGrp("tx_diag_{}".format(part),
                      label="zone filter", text="",
                      placeholderText="e.g. blink, smile, browsDown_R",
                      columnWidth=[(1, 90), (2, 280)])
    cmds.button(label="diagnose ▶", c=lambda x, p=part: _diagnose_from_field(p))

    cmds.setParent("..")
    cmds.setParent("..")


def _build_eye_section(part):
    cmds.frameLayout(label="{} tweaks".format(part).upper(),
                     marginHeight=6, marginWidth=8,
                     collapsable=True, collapse=True)
    cmds.columnLayout(adj=True, rowSpacing=2)

    cb = lambda *_, p=part: _apply_eye_preset(p)
    _slider_row("sl_{}_spec".format(part),      "specular",        0.0, 3.0,  1.0,   cb)
    _slider_row("sl_{}_specRough".format(part), "spec roughness",  0.01, 1.0, 0.15,  cb)
    _slider_row("sl_{}_specIOR".format(part),   "spec IOR",        1.0, 2.5,  1.376, cb)
    cmds.separator(height=4, style="none")
    cmds.button(label="↺ Reset eye preset", c=lambda x, p=part: _reset_eye(p))
    cmds.setParent("..")
    cmds.setParent("..")


def _convert_extra(part):
    mod = _import("mh_extras_to_arnold")
    if mod is None:
        return
    try:
        mod.convert(part)
    except Exception as e:
        cmds.warning("[mh-ui] convert extra '{}': {}".format(part, e))
    _refresh_status()


def _convert_all_extras():
    mod = _import("mh_extras_to_arnold")
    if mod is None:
        return
    try:
        mod.convert_all()
    except Exception as e:
        cmds.warning("[mh-ui] convert_all_extras: {}".format(e))
    _refresh_status()


def _revert_extra(part):
    mod = _import("mh_extras_to_arnold")
    if mod is None:
        return
    try:
        mod.revert(part)
    except Exception as e:
        cmds.warning("[mh-ui] revert extra '{}': {}".format(part, e))


def _revert_all_extras():
    mod = _import("mh_extras_to_arnold")
    if mod is None:
        return
    try:
        mod.revert_all()
    except Exception as e:
        cmds.warning("[mh-ui] revert_all_extras: {}".format(e))


_EXTRAS_INFO = [
    # (key, label, brief description shown next to button)
    ("eyeshell",        "eyeshell",        "cornea (transmission, IOR 1.376)"),
    ("saliva",          "saliva",          "mouth wet (transmission, IOR 1.33)"),
    ("eyelashes",       "eyelashes",       "lashes with opacity"),
    ("eyelashesShadow", "eyelashesShadow", "fake contact shadow"),
    ("eyeEdge",         "eyeEdge",         "wet pink lacrimal rim"),
]


def _build_extras_section():
    """5 small MetaHuman extras (blinn-based) — eyeshell/saliva/eyelashes/etc."""
    cmds.frameLayout(label="MH EXTRAS",
                     marginHeight=6, marginWidth=8,
                     collapsable=True, collapse=False)
    cmds.columnLayout(adj=True, rowSpacing=4)

    # Bulk row
    cmds.rowLayout(numberOfColumns=2, columnWidth2=(220, 220),
                   columnAttach=[(1, "both", 4), (2, "both", 4)])
    cmds.button(label="▶  Convert all extras",
                bgc=(0.25, 0.45, 0.65), height=28,
                command=lambda *_: _convert_all_extras())
    cmds.button(label="↺  Revert all extras",
                bgc=(0.55, 0.40, 0.25), height=28,
                command=lambda *_: _revert_all_extras())
    cmds.setParent("..")

    cmds.separator(height=4, style="in")

    # Per-shader rows: status | convert | revert | description
    cmds.rowColumnLayout(numberOfColumns=4,
                         columnWidth=[(1, 130), (2, 90), (3, 80), (4, 200)],
                         columnAttach=[(1, "left", 4), (2, "both", 2),
                                        (3, "both", 2), (4, "left", 4)])
    for key, label, desc in _EXTRAS_INFO:
        cmds.text(label=label, font="boldLabelFont")
        cmds.button(label="convert", c=lambda x, k=key: _convert_extra(k))
        cmds.button(label="revert",  c=lambda x, k=key: _revert_extra(k))
        cmds.text(label=desc, font="smallPlainLabelFont", align="left")
    cmds.setParent("..")
    cmds.setParent("..")
    cmds.setParent("..")


def _build_teeth_section():
    cmds.frameLayout(label="TEETH tweaks",
                     marginHeight=6, marginWidth=8,
                     collapsable=True, collapse=True)
    cmds.columnLayout(adj=True, rowSpacing=2)

    cb = lambda *_: _on_teeth_change()
    _slider_row("sl_teeth_sss",       "SSS weight",    0.0, 1.0,  0.30, cb)
    _slider_row("sl_teeth_spec",      "specular",      0.0, 2.0,  0.85, cb)
    _slider_row("sl_teeth_specRough", "spec rough",    0.01, 1.0, 0.20, cb)
    _slider_row("sl_teeth_coat",      "coat",          0.0, 1.0,  0.10, cb)
    cmds.colorSliderGrp("sl_teeth_sssColor", label="SSS color",
                        rgb=(0.95, 0.85, 0.70), cc=cb,
                        columnWidth=[(1, 130), (2, 70), (3, 220)])
    cmds.separator(height=4, style="none")
    cmds.button(label="↺ Reset teeth preset", c=lambda *_: _reset_teeth())
    cmds.setParent("..")
    cmds.setParent("..")


def _build_window():
    if cmds.window(WINDOW_NAME, exists=True):
        cmds.deleteUI(WINDOW_NAME)
    win = cmds.window(WINDOW_NAME, title=WINDOW_TITLE,
                      widthHeight=(540, 760), sizeable=True)
    main = cmds.scrollLayout(horizontalScrollBarThickness=0)
    cmds.columnLayout(adj=True, rowSpacing=4, columnAttach=("both", 4))

    # Header
    cmds.text(label=WINDOW_TITLE, font="boldLabelFont", height=24)
    cmds.text(label="Convert MetaHuman shaders to aiStandardSurface and tweak live.",
              font="smallPlainLabelFont")
    cmds.separator(height=6, style="in")

    _build_conversion_section()

    cmds.separator(height=8, style="none")
    cmds.text(label="2. SKIN TWEAKS", font="boldLabelFont", align="left", height=20)
    _build_skin_section("head")
    _build_skin_section("body")

    cmds.separator(height=8, style="none")
    cmds.text(label="3. WRINKLES (debug + intensity)", font="boldLabelFont",
              align="left", height=20)
    _build_wrinkles_section("head")
    _build_wrinkles_section("body")

    cmds.separator(height=8, style="none")
    cmds.text(label="4. EYES + TEETH", font="boldLabelFont", align="left", height=20)
    _build_eye_section("eyeLeft")
    _build_eye_section("eyeRight")
    _build_teeth_section()

    cmds.separator(height=8, style="none")
    cmds.text(label="5. MH EXTRAS (cornea, lashes, saliva, edge)",
              font="boldLabelFont", align="left", height=20)
    _build_extras_section()

    cmds.separator(height=10, style="none")
    cmds.button(label="🔄 Refresh status & sliders",
                c=lambda *_: _refresh_status(), height=28)
    cmds.separator(height=6, style="none")
    cmds.text(label="Tip: turn on Arnold IPR (RenderView) to see slider changes live.",
              font="smallPlainLabelFont", align="left")
    cmds.setParent("..")
    cmds.setParent("..")
    return win


# ============================================================================
# ENTRY POINT
# ============================================================================

def show():
    win = _build_window()
    cmds.showWindow(win)
    _refresh_status()


if __name__ == "__main__":
    show()
