"""
mh_body_to_arnold.py
====================
MetaHuman body shader → Arnold aiStandardSurface converter.

Designed for the new MetaHuman for Maya plugin (UE 5.6+, plugin v1.3.x),
which uses a single dx11Shader node named `shader_body` with:
  - base color/normal/cavity textures
  - 3 anim color maps + 3 anim normal maps for wrinkle deltas
  - 10 RGBA mask textures packing 37 zone-specific channels
  - 82 maskWeight inputs driven by FRM_WMmultipliers (DNA rig solver)

VERSION 1 SCOPE
---------------
- Build an aiStandardSurface with correct base color, base normal, SSS, spec, coat.
- Reuse the existing MetaHuman file nodes (no texture duplication).
- Force correct color spaces (sRGB for color, Raw for normal/mask/cavity).
- Override the shadingEngine's surfaceShader so renders pick up the Arnold material.
- Leave the DX11 shader intact: re-running `revert_to_dx11()` restores viewport.

NOT in v1 (planned for v2)
--------------------------
- Per-zone wrinkle compositing (animColorMap × maskChannel × maskWeight).
  The architecture is sketched in `build_wrinkle_compositor_stub()` below.

USAGE
-----
1. Open your MetaHuman scene with `shader_head_shader` present.
2. In Maya's script editor (Python tab):
       import mh_head_to_arnold as conv
       conv.convert()
3. To restore DX11 viewport assignment:
       conv.revert_to_dx11()

Author : built for Maitre's freelance pipeline (Maya + Arnold + MetaHuman 5.6).
"""

import maya.cmds as cmds


# ============================================================================
# CONFIGURATION
# ============================================================================

DX11_SHADER = "shader_body_shader"
ARNOLD_SHADER = "body_aiStandardSurface"

# TEX is populated at runtime by _autodetect_textures() because body texture
# names vary by character (`baseMapFile_body_color` vs `baseMapFile_<charName>_body_color`).
# All file-node lookups in this script flow through TEX.
TEX = {
    "diffuse":   None,
    "normal":    None,
    "cavity":    None,
    "cm":     {1: None, 2: None, 3: None},
    "wm":     {1: None, 2: None, 3: None},
    "masks":  [None] * 37,    # 37 maskChannel_NN entries, by channel index
}

# SSS preset — caucasian skin baseline. Tweak per character.
# subsurfaceType: 0 = diffusion, 1 = randomwalk, 2 = randomwalk_v2 (Arnold 7+)
SSS = {
    "weight":  0.50,
    "color":   (0.95, 0.85, 0.80),  # warm pink
    "radius":  (1.00, 0.20, 0.10),  # cm — red>green>blue penetration
    "scale":   0.15,                # cm
    "type":    2,                   # randomwalk_v2
}

# Skin specular & coat baseline
SKIN_SPEC = {
    "specular":           0.50,
    "specularRoughness":  0.40,
    "specularIOR":        1.40,
    "coat":               0.05,
    "coatRoughness":      0.30,
    "coatIOR":            1.50,
}


# ============================================================================
# UTILITIES
# ============================================================================

def _resolve_dx11_shader():
    """Resolve the actual DX11 shader name in the scene by trying common
    MetaHuman naming variants. Updates the module-level DX11_SHADER if needed.
    Returns the resolved name, or None if no variant exists."""
    global DX11_SHADER
    candidates = [DX11_SHADER]
    base = DX11_SHADER.replace("_shader", "")
    for v in (base, base + "_shader", base + "_mesh_shader"):
        if v not in candidates:
            candidates.append(v)
    for name in candidates:
        if cmds.objExists(name):
            if name != DX11_SHADER:
                print("[mh2arnold] Resolved DX11 shader '{}' (was '{}')".format(name, DX11_SHADER))
                DX11_SHADER = name
            return name
    return None


def _cleanup_previous_run():
    """Remove any orphan/unknown nodes from a previous failed run."""
    stale_names = [
        "body_aiNormalMap",
        "body_specCavityMul",
        "body_sssColorMix",      # legacy aiMix attempt — may exist as unknown
        "body_sssColorBlend",
        "body_DEBUG_surfaceShader",   # debug shader from viewport_debug_on()
        ARNOLD_SHADER,
    ]
    for n in stale_names:
        if cmds.objExists(n):
            try:
                cmds.delete(n)
            except Exception as e:
                cmds.warning("[mh2arnold-body] Could not delete stale node '{}': {}".format(n, e))

    # v2 wrinkle compositor nodes — delete by name prefix
    # These are generated programmatically (~135 nodes), so wildcard cleanup.
    v2_prefixes = [
        "body_lerp_sub_",                         # 6 lerp-delta subtracts (cm/wm)
        "body_delta_sub_", "body_delta_mul_",     # legacy (pre-fix) names — remove if present
        "body_colC_dxm_", "body_colC_w_",         # color contribs
        "body_nrmC_dxm_", "body_nrmC_w_",         # normal contribs
        "body_sum_", "body_final_",               # accumulators
        "head_amp_",                              # intensity amplifiers
    ]
    for prefix in v2_prefixes:
        matches = cmds.ls(prefix + "*") or []
        for m in matches:
            try:
                cmds.delete(m)
            except Exception:
                pass

    # Also kill any node Maya marked as 'unknown' (from aiMix etc.)
    unknown_nodes = cmds.ls(type="unknown") or []
    for u in unknown_nodes:
        if u.startswith("body_"):
            try:
                cmds.lockNode(u, lock=False)
                cmds.delete(u)
                print("[mh2arnold-body] Removed unknown stale node: {}".format(u))
            except Exception:
                pass



def _autodetect_textures():
    """Walk the DX11 shader's connections and populate the TEX dict.

    Body-shader file nodes have variable names (depending on character export):
    e.g. `baseMapFile_body_color`, `baseMapFile_<charName>_body_color`, etc.
    Rather than hard-code them, we follow connections from the DX11 shader's
    well-known input attributes back to the file nodes that drive them.

    Populates:
        TEX["diffuse"], TEX["normal"], TEX["cavity"]
        TEX["cm"][1..3], TEX["wm"][1..3]
        TEX["masks"][0..36]
    """
    if not cmds.objExists(DX11_SHADER):
        cmds.error("[mh2arnold-body] '{}' not found.".format(DX11_SHADER))
        return

    def _src_file(attr):
        plug = DX11_SHADER + "." + attr
        if not cmds.attributeQuery(attr, node=DX11_SHADER, exists=True):
            return None
        srcs = cmds.listConnections(plug, s=True, d=False, type="file") or []
        return srcs[0] if srcs else None

    TEX["diffuse"] = _src_file("DiffuseTexture")
    TEX["normal"]  = _src_file("NormalTexture")
    TEX["cavity"]  = _src_file("CavityTexture")
    for i in (0, 1, 2):
        TEX["cm"][i + 1] = _src_file("animColorMap_{:02d}".format(i))
        TEX["wm"][i + 1] = _src_file("animNormalMap_{:02d}".format(i))
    for i in range(37):
        TEX["masks"][i] = _src_file("maskChannel_{:02d}".format(i))

    print("[mh2arnold-body] Auto-detected textures:")
    print("  diffuse: {}".format(TEX["diffuse"]))
    print("  normal:  {}".format(TEX["normal"]))
    print("  cavity:  {}".format(TEX["cavity"]))
    for i in (1, 2, 3):
        print("  cm{}:    {}".format(i, TEX["cm"][i]))
        print("  wm{}:    {}".format(i, TEX["wm"][i]))
    distinct_masks = sorted(set(m for m in TEX["masks"] if m))
    print("  {} distinct mask file nodes:".format(len(distinct_masks)))
    for m in distinct_masks:
        print("    - {}".format(m))


def _ensure_arnold():
    if not cmds.pluginInfo("mtoa", q=True, loaded=True):
        cmds.loadPlugin("mtoa")


def _set_color_space(node, space):
    """Force a colorSpace on a file node, ignoring the project rules."""
    if not node or not cmds.objExists(node):
        return False
    try:
        cmds.setAttr(node + ".ignoreColorSpaceFileRules", 1)
        cmds.setAttr(node + ".colorSpace", space, type="string")
        return True
    except Exception as e:
        cmds.warning("Could not set colorSpace on {}: {}".format(node, e))
        return False


def _setup_color_spaces():
    """sRGB for color textures, Raw for everything else (normal, mask, cavity).
    Skips entries that are None (not present in scene)."""
    srgb = [TEX["diffuse"]] + list(TEX["cm"].values())
    raw  = [TEX["normal"], TEX["cavity"]] + list(TEX["wm"].values()) + TEX["masks"]
    for n in srgb:
        if n:
            _set_color_space(n, "sRGB")
    for n in raw:
        if n:
            _set_color_space(n, "Raw")


def _create_node(node_type, name, as_what="asUtility"):
    """Create a shading node, deleting any existing one with the same name."""
    if cmds.objExists(name):
        cmds.delete(name)
    kwargs = {as_what: True, "name": name}
    return cmds.shadingNode(node_type, **kwargs)


def _connect(src, dst, force=True):
    """Wrapper around connectAttr with idempotent behavior."""
    cmds.connectAttr(src, dst, force=force)


def _get_shadingengine(shader):
    """Return the shadingEngine that uses this shader as surfaceShader, if any.

    Robust to re-runs of convert(): even when neither DX11 nor the Arnold shader
    is currently connected to the SG (because cleanup just deleted the old one),
    we can still find the SG by Maya's naming convention (`<shader>SG`) or by
    walking the head mesh.
    """
    # 1. Direct connection on outColor (the normal case on first run)
    sgs = cmds.listConnections(shader + ".outColor", type="shadingEngine") or []
    if sgs:
        return sgs[0]

    # 2. Maya-convention name: <shader>SG  (persists across runs)
    sg_name = shader + "SG"
    if cmds.objExists(sg_name) and cmds.nodeType(sg_name) == "shadingEngine":
        return sg_name

    # 3. General history walk on the shader
    sgs = cmds.listConnections(shader, type="shadingEngine") or []
    if sgs:
        return sgs[0]

    # 4. Fall back to walking the head mesh's SG
    for mesh_candidate in ("body_lod0_mesh", "body_lod0_meshShape",
                           "body_lod0_mesh_skinning_geometry",
                           "m_med_nrw_body_lod0_meshShape"):
        if cmds.objExists(mesh_candidate):
            sgs = cmds.listConnections(mesh_candidate, type="shadingEngine") or []
            if sgs:
                return sgs[0]

    return None


# ============================================================================
# AISTANDARDSURFACE BUILD
# ============================================================================

def _build_base_color(ai_shader):
    """Plug baseMapFile_head_color → aiStandardSurface.baseColor."""
    diffuse = TEX["diffuse"]
    if not diffuse or not cmds.objExists(diffuse):
        cmds.warning("[mh2arnold-body] Missing diffuse file node: {}".format(diffuse))
        return
    _connect(diffuse + ".outColor", ai_shader + ".baseColor")
    cmds.setAttr(ai_shader + ".base", 1.0)


def _build_normal(ai_shader):
    """Plug baseMapFile_head_normal → aiNormalMap → aiStandardSurface.normalCamera."""
    normal = TEX["normal"]
    if not normal or not cmds.objExists(normal):
        cmds.warning("[mh2arnold-body] Missing normal file node: {}".format(normal))
        return

    nmap = _create_node("aiNormalMap", "body_aiNormalMap")
    _connect(normal + ".outColor", nmap + ".input")
    # MetaHuman normals are tangent-space, +Y up (DirectX-style green channel).
    # Arnold's aiNormalMap default = tangent space, OpenGL-style. If you see
    # inverted bumps in shaded areas, flip Y by setting invertY=1.
    cmds.setAttr(nmap + ".tangentSpace", 1)
    cmds.setAttr(nmap + ".invertY", 0)   # try 1 if normals look wrong
    cmds.setAttr(nmap + ".colorToSigned", 1)
    _connect(nmap + ".outValue", ai_shader + ".normalCamera")


def _is_valid_texture_path(file_node):
    """True if the file node has a real file path (not empty, not just a directory)."""
    if not file_node or not cmds.objExists(file_node):
        return False
    path = cmds.getAttr(file_node + ".fileTextureName") or ""
    if not path:
        return False
    # Path ending with / or \ means directory only, no actual filename
    if path.endswith("/") or path.endswith("\\"):
        return False
    return True


def _silence_empty_file_node(file_node):
    """Clear the path of a file node that points to an empty directory,
    to stop Arnold's 'Failed to open texture' warnings on every render."""
    if not file_node or not cmds.objExists(file_node):
        return
    path = cmds.getAttr(file_node + ".fileTextureName") or ""
    if path.endswith("/") or path.endswith("\\"):
        try:
            cmds.setAttr(file_node + ".fileTextureName", "", type="string")
            print("[mh2arnold-body] Cleared empty path on '{}' to silence warnings.".format(file_node))
        except Exception as e:
            cmds.warning("[mh2arnold-body] Could not clear path on {}: {}".format(file_node, e))


def _build_cavity_into_specular(ai_shader):
    """Use cavity (if present) to modulate specular weight: small wrinkles damp specular.

    Uses Maya's native multiplyDivide (operation=multiply) instead of aiMultiply
    for max compatibility across MtoA versions.
    """
    cavity = TEX["cavity"]

    if not _is_valid_texture_path(cavity):
        cmds.warning("[mh2arnold-body] Cavity unavailable — using flat specular.")
        _silence_empty_file_node(cavity)
        cmds.setAttr(ai_shader + ".specular", SKIN_SPEC["specular"])
        return

    # spec_weight = baseSpec * cavity_R
    mul = _create_node("multiplyDivide", "body_specCavityMul")
    cmds.setAttr(mul + ".operation", 1)  # multiply
    cmds.setAttr(mul + ".input1X", SKIN_SPEC["specular"])
    _connect(cavity + ".outColorR", mul + ".input2X")
    _connect(mul + ".outputX", ai_shader + ".specular")


def _set_sss_and_spec(ai_shader):
    """Apply SSS / spec / coat presets."""
    cmds.setAttr(ai_shader + ".subsurface",       SSS["weight"])
    cmds.setAttr(ai_shader + ".subsurfaceColor",  *SSS["color"], type="double3")
    cmds.setAttr(ai_shader + ".subsurfaceRadius", *SSS["radius"], type="double3")
    cmds.setAttr(ai_shader + ".subsurfaceScale",  SSS["scale"])
    cmds.setAttr(ai_shader + ".subsurfaceType",   SSS["type"])

    # If diffuse texture is plugged, route its color into subsurfaceColor too
    # for tint consistency (skin scatter tinted by surface albedo).
    # Use Maya native blendColors (rock-solid, no MtoA version surprises).
    diffuse = TEX["diffuse"]
    if diffuse and cmds.objExists(diffuse):
        blend = _create_node("blendColors", "body_sssColorBlend")
        # color1 = base albedo, color2 = warm SSS preset, blender = 0.5 mix
        _connect(diffuse + ".outColor", blend + ".color1")
        cmds.setAttr(blend + ".color2",
                     SSS["color"][0], SSS["color"][1], SSS["color"][2],
                     type="double3")
        cmds.setAttr(blend + ".blender", 0.5)
        _connect(blend + ".output", ai_shader + ".subsurfaceColor")

    cmds.setAttr(ai_shader + ".specularRoughness", SKIN_SPEC["specularRoughness"])
    cmds.setAttr(ai_shader + ".specularIOR",       SKIN_SPEC["specularIOR"])
    cmds.setAttr(ai_shader + ".coat",              SKIN_SPEC["coat"])
    cmds.setAttr(ai_shader + ".coatRoughness",     SKIN_SPEC["coatRoughness"])
    cmds.setAttr(ai_shader + ".coatIOR",           SKIN_SPEC["coatIOR"])

    # ------------------------------------------------------------------------
    # Debug intensity controls — exposed as keyable attrs on the shader.
    # Set to 1.0 for normal/final render, 5-10 to amplify wrinkles for visual
    # identification of which zones fire when. Set to 0 to disable wrinkles.
    # Adjustable live via channel box / attribute editor while scrubbing the rig.
    # ------------------------------------------------------------------------
    for attr_name in ("wrinkleColorIntensity", "wrinkleNormalIntensity"):
        if not cmds.attributeQuery(attr_name, node=ai_shader, exists=True):
            cmds.addAttr(ai_shader, longName=attr_name, attributeType="float",
                         defaultValue=1.0, minValue=0.0, softMaxValue=20.0,
                         keyable=True)


# ============================================================================
# WRINKLE COMPOSITOR — V2
# ============================================================================
# Architecture:
#   For each of the 82 maskWeight_XX inputs on shader_head_shader:
#     1. Parse driver name "head_cmN_color_head_wmM_zoneName_LR" or
#        "head_wmN_normal_head_wmM_zoneName_LR".
#     2. Determine target (color path → cmN map | normal path → wmN map).
#     3. Map (group, zone) → (channel_idx, mask_file, RGBA component) using
#        the deterministic heuristic validated against the dump:
#          - groups in order: wm1 (15 zones), wm2 (10), wm3 (8), wm13 (4) = 37
#          - zones within a group: first-appearance order
#          - 4 components (R/G/B/A) per mask file, sequentially
#     4. Build contribution: (animMap - 0.5) × 2 × maskComp × weight
#     5. Sum all color contribs → add to base diffuse → baseColor
#     6. Sum all normal contribs → add to base normal → aiNormalMap → normalCamera
#
# Number of nodes generated: ~135 (82 contribs × 2 muls + 6 deltas + 4 sums + glue)
# ============================================================================

# Group → ordered list of mask file nodes (each provides 4 RGBA components)
# ----------------------------------------------------------------------------
# HLSL-EXTRACTED MAPPING — THE GROUND TRUTH
# ----------------------------------------------------------------------------
# These tables are extracted directly from the MetaHumanForMaya plugin's
# `dx11_shd_head.fx` HLSL source code (function `f`, the pixel shader).
# Do NOT modify unless the .fx file changes (e.g. plugin update).
#
# Mapping format:
#   HLSL_WEIGHTS_BY_CHANNEL[weight_idx] = (channel_idx, anim_type, anim_map_idx)
#     - channel_idx:     0..36, the maskChannel_NN index in HLSL
#     - anim_type:       "color" or "normal"
#     - anim_map_idx:    1, 2, or 3 (cm1/cm2/cm3 = animColorMap_00/01/02 = TEX["cm"][N])
#
# The 41 color weights and 41 normal weights total 82 — matching the
# 82 maskWeight_00..maskWeight_81 attributes on shader_head_shader.
# ----------------------------------------------------------------------------

HLSL_WEIGHTS_BY_CHANNEL = {
    # ===== COLOR contributions (animColorDelta) =====
    # animColorDelta_00 (cm1) — 19 weights, channels {0..14, 33..36}
    20: (1,  "color", 1), 70: (10, "color", 1), 43: (36, "color", 1),
    66: (12, "color", 1), 45: (34, "color", 1), 12: (4,  "color", 1),
    14: (5,  "color", 1), 44: (33, "color", 1), 42: (35, "color", 1),
    16: (0,  "color", 1), 74: (7,  "color", 1), 64: (11, "color", 1),
    68: (9,  "color", 1), 10: (3,  "color", 1),  8: (2,  "color", 1),
    72: (6,  "color", 1), 21: (14, "color", 1), 76: (8,  "color", 1),
    17: (13, "color", 1),
    # animColorDelta_01 (cm2) — 10 weights, channels {15..24}
    78: (21, "color", 2), 60: (19, "color", 2), 80: (22, "color", 2),
     6: (18, "color", 2),  4: (17, "color", 2), 62: (20, "color", 2),
    38: (24, "color", 2), 36: (23, "color", 2),  2: (16, "color", 2),
     0: (15, "color", 2),
    # animColorDelta_02 (cm3) — 12 weights, channels {25..36}
    30: (26, "color", 3), 52: (35, "color", 3), 32: (30, "color", 3),
    26: (29, "color", 3), 24: (25, "color", 3), 56: (36, "color", 3),
    25: (27, "color", 3), 57: (34, "color", 3), 53: (33, "color", 3),
    31: (28, "color", 3), 50: (32, "color", 3), 40: (31, "color", 3),

    # ===== NORMAL contributions (animNormalDelta) =====
    # animNormalDelta_00 (wm1) — 19 weights
    47: (36, "normal", 1), 15: (5,  "normal", 1), 13: (4,  "normal", 1),
    46: (35, "normal", 1), 73: (6,  "normal", 1), 11: (3,  "normal", 1),
    65: (11, "normal", 1), 75: (7,  "normal", 1), 67: (12, "normal", 1),
     9: (2,  "normal", 1), 49: (34, "normal", 1), 71: (10, "normal", 1),
    48: (33, "normal", 1), 23: (14, "normal", 1), 69: (9,  "normal", 1),
    18: (0,  "normal", 1), 22: (1,  "normal", 1), 77: (8,  "normal", 1),
    19: (13, "normal", 1),
    # animNormalDelta_01 (wm2) — 10 weights
     7: (18, "normal", 2),  3: (16, "normal", 2), 81: (22, "normal", 2),
    79: (21, "normal", 2),  1: (15, "normal", 2),  5: (17, "normal", 2),
    63: (20, "normal", 2), 37: (23, "normal", 2), 61: (19, "normal", 2),
    39: (24, "normal", 2),
    # animNormalDelta_02 (wm3) — 12 weights
    59: (34, "normal", 3), 35: (30, "normal", 3), 54: (35, "normal", 3),
    58: (36, "normal", 3), 29: (29, "normal", 3), 55: (33, "normal", 3),
    28: (27, "normal", 3), 33: (26, "normal", 3), 27: (25, "normal", 3),
    34: (28, "normal", 3), 41: (31, "normal", 3), 51: (32, "normal", 3),
}

# channel_idx → component letter (R/G/B/A), as per HLSL sampling convention.
# The mask file node for each channel is resolved at runtime via TEX["masks"][ch_idx],
# populated by _autodetect_textures() from actual connections to maskChannel_NN inputs.
HLSL_CHANNEL_COMPONENTS = {
     0: "R",  1: "G",  2: "B",  3: "A",
     4: "R",  5: "G",  6: "B",  7: "A",
     8: "R",  9: "G", 10: "B", 11: "A",
    12: "R", 13: "G", 14: "B",
    15: "R", 16: "G", 17: "B", 18: "A",
    19: "R", 20: "G", 21: "B", 22: "A",
    23: "R", 24: "G",
    25: "R", 26: "G", 27: "B", 28: "A",
    29: "R", 30: "G", 31: "B", 32: "A",
    33: "R", 34: "G", 35: "B", 36: "A",
}

# legacy — kept for backwards compat with older calls; no longer used in v3
_GROUP_FILES = {
    "wm1":  ["maskFile_head_wm1_01", "maskFile_head_wm1_02",
             "maskFile_head_wm1_03", "maskFile_head_wm1_04"],
    "wm2":  ["maskFile_head_wm2_01", "maskFile_head_wm2_02",
             "maskFile_head_wm2_03"],
    "wm3":  ["maskFile_head_wm3_01", "maskFile_head_wm3_02"],
    "wm13": ["maskFile_head_wm13_01"],
}
_GROUP_ORDER = ["wm1", "wm2", "wm3", "wm13"]
_COMPONENTS = ["R", "G", "B", "A"]


# ----------------------------------------------------------------------------
# MANUAL MAPPING OVERRIDE
# ----------------------------------------------------------------------------
# When a zone fires on the wrong area of the face (e.g. moving the mouth
# controller activates a brow mask), add an entry here to override the
# heuristic. Format:
#
#     (group, zone_name): (channel_idx, mask_file_node, component_letter)
#
# Component letter is "R", "G", "B", or "A".
# Example:
#     ("wm3", "smile_L"):  (31, "maskFile_head_wm3_02", "B"),
#
# Use diagnose() to inspect the current mapping and identify mismatches.
# ----------------------------------------------------------------------------
ZONE_MAPPING_OVERRIDE = {
    # Add entries below as you identify wrong mappings.
}


def _parse_weight_drivers():
    """Walk the 82 maskWeight_XX inputs and parse their driver names.

    Returns a list of dicts:
        {
            "weight_idx":  int,
            "src_plug":    "FRM_WMmultipliers.head_cmN_color_head_wmM_zoneName",
            "type":        "color" | "normal",
            "map_idx":     1 | 2 | 3,   # cm or wm map index
            "group":       "wm1"|"wm2"|"wm3"|"wm13",
            "zone":        "browsDown_L" etc.
        }
    """
    weights = []
    for i in range(100):  # enough headroom; actual count is 82
        plug = "{}.maskWeight_{:02d}".format(DX11_SHADER, i)
        if not cmds.objExists(plug):
            continue
        srcs = cmds.listConnections(plug, s=True, d=False, p=True) or []
        if not srcs:
            continue
        src = srcs[0]
        attr = src.split(".")[-1]

        # type = color | normal
        if "_color_" in attr:
            wtype = "color"
        elif "_normal_" in attr:
            wtype = "normal"
        else:
            cmds.warning("[mh2arnold-body] Unparsable weight driver: " + attr)
            continue

        # map_idx from prefix: head_cm{N}_color or head_wm{N}_normal
        map_idx = None
        for n in (1, 2, 3):
            prefix = "head_cm{}_color".format(n) if wtype == "color" else "head_wm{}_normal".format(n)
            if attr.startswith(prefix):
                map_idx = n
                rest = attr[len(prefix) + 1:]   # +1 to drop the underscore
                break
        if map_idx is None:
            cmds.warning("[mh2arnold-body] Could not extract map_idx: " + attr)
            continue

        # rest = "head_wm{M}_zoneName"  → group + zone
        group = None
        zone = None
        for g in _GROUP_ORDER:
            tag = "head_{}_".format(g)
            if rest.startswith(tag):
                group = g
                zone = rest[len(tag):]
                break
        if group is None:
            cmds.warning("[mh2arnold-body] Could not extract group: " + attr)
            continue

        weights.append({
            "weight_idx": i,
            "src_plug":   src,
            "type":       wtype,
            "map_idx":    map_idx,
            "group":      group,
            "zone":       zone,
        })
    return weights


def _build_zone_to_channel_mapping(weights):
    """For each (group, zone), assign a unique (channel_idx, mask_file, component).

    Heuristic, validated against the dump:
        - 4 components per file, in order R, G, B, A.
        - Zones within a group are ordered by their first appearance in the
          weight list.
        - Groups in canonical order: wm1, wm2, wm3, wm13.

    Returns: dict { (group, zone): (channel_idx, mask_file_node, component) }.
    """
    # Collect ordered unique zones per group
    zones_by_group = {g: [] for g in _GROUP_ORDER}
    for w in weights:
        if w["zone"] not in zones_by_group[w["group"]]:
            zones_by_group[w["group"]].append(w["zone"])

    mapping = {}
    global_channel = 0
    for group in _GROUP_ORDER:
        files = _GROUP_FILES[group]
        zones = zones_by_group[group]
        for i, zone in enumerate(zones):
            file_idx = i // 4
            comp_idx = i % 4
            if file_idx >= len(files):
                cmds.warning("[mh2arnold-body] Group {} has more zones ({}) than "
                             "available mask files ({}).".format(group, len(zones), len(files) * 4))
                break
            mapping[(group, zone)] = (global_channel, files[file_idx], _COMPONENTS[comp_idx])
            global_channel += 1

    # Apply manual overrides on top of heuristic
    for key, val in ZONE_MAPPING_OVERRIDE.items():
        if key in mapping:
            mapping[key] = val
        else:
            cmds.warning("[mh2arnold-body] Override for unknown zone {}: skipped.".format(key))

    return mapping


def _mask_component_plug(file_node, component):
    """Return the Maya plug for a specific RGBA component of a file node.
    For R/G/B → outColor.X; for A → outAlpha (file nodes have a separate scalar)."""
    if component == "A":
        return file_node + ".outAlpha"
    return "{}.outColor{}".format(file_node, component)


def _build_lerp_delta(anim_file, base_file, suffix):
    """Compute (animMap - baseMap) → vec3 plug.

    Used to drive a true lerp:  final = base + delta × mask × weight
                                      = lerp(base, animMap, mask × weight)

    When mask × weight = 0  → final = base
    When mask × weight = 1  → final = animMap (the wrinkled/expression target)

    Used once per anim color/normal map (cm1/cm2/cm3 paired with diffuse;
    wm1/wm2/wm3 paired with normal) → 6 nodes total.
    """
    sub = _create_node("plusMinusAverage", "body_lerp_sub_" + suffix)
    cmds.setAttr(sub + ".operation", 2)  # subtract
    _connect(anim_file + ".outColor", sub + ".input3D[0]")
    _connect(base_file + ".outColor", sub + ".input3D[1]")
    return sub + ".output3D"


def _broadcast_scalar_to_vec3(mul_node, scalar_plug):
    """Connect a scalar plug to all 3 components of a multiplyDivide.input2."""
    _connect(scalar_plug, mul_node + ".input2X")
    _connect(scalar_plug, mul_node + ".input2Y")
    _connect(scalar_plug, mul_node + ".input2Z")


def _build_contribution(delta_plug, mask_scalar_plug, weight_scalar_plug, idx, kind):
    """contribution_vec3 = delta × mask × weight.

    kind: "col" or "nrm" — for naming only.
    Returns the output plug of the second multiply node.
    """
    # Step 1: delta × mask  (vec3 × scalar broadcast to vec3)
    mul1 = _create_node("multiplyDivide", "head_{}C_dxm_{:02d}".format(kind, idx))
    cmds.setAttr(mul1 + ".operation", 1)
    _connect(delta_plug, mul1 + ".input1")
    _broadcast_scalar_to_vec3(mul1, mask_scalar_plug)

    # Step 2: × weight
    mul2 = _create_node("multiplyDivide", "head_{}C_w_{:02d}".format(kind, idx))
    cmds.setAttr(mul2 + ".operation", 1)
    _connect(mul1 + ".output", mul2 + ".input1")
    _broadcast_scalar_to_vec3(mul2, weight_scalar_plug)

    return mul2 + ".output"


def _sum_contributions(plugs, name):
    """Sum many vec3 plugs via plusMinusAverage. Returns output3D plug."""
    if not plugs:
        return None
    pma = _create_node("plusMinusAverage", "body_sum_" + name)
    cmds.setAttr(pma + ".operation", 1)  # sum
    for i, p in enumerate(plugs):
        _connect(p, "{}.input3D[{}]".format(pma, i))
    return pma + ".output3D"


def _add_base_and_delta(base_outcolor_plug, delta_plug, name):
    """final = base + delta_sum (both vec3). Returns the sum output plug."""
    pma = _create_node("plusMinusAverage", "body_final_" + name)
    cmds.setAttr(pma + ".operation", 1)
    _connect(base_outcolor_plug, pma + ".input3D[0]")
    if delta_plug:
        _connect(delta_plug, pma + ".input3D[1]")
    return pma + ".output3D"


def build_wrinkle_compositor(ai_shader):
    """Build the per-zone wrinkle composition for color and normal, using
    the HLSL-extracted ground-truth mapping (HLSL_WEIGHTS_BY_CHANNEL +
    HLSL_CHANNEL_MAP). This produces a result functionally equivalent to
    what the DX11 viewport shader does in MetaHumanForMaya 1.3.x.

    Pipeline (per contribution):
        delta_NN = animMap_NN.outColor - baseFile.outColor
        contrib  = delta_NN × maskFile.<component> × FRM_WMmultipliers.<weight>

    Pipeline (combine):
        finalBaseColor    = baseDiffuse + Σ (color contribs) × intensity
        finalBaseNormalEnc = baseNormalEnc + Σ (normal contribs) × intensity
        normalCamera      = aiNormalMap(finalBaseNormalEnc)
    """
    if not cmds.objExists(DX11_SHADER):
        cmds.error("[mh2arnold-body] " + DX11_SHADER + " not found.")
        return

    # If the body shader has no anim maps connected (typical — only the head
    # uses wrinkles in MetaHuman), skip the compositor entirely.
    has_any_cm = any(TEX["cm"][i] for i in (1, 2, 3))
    has_any_wm = any(TEX["wm"][i] for i in (1, 2, 3))
    has_any_mask = any(TEX["masks"])
    if not (has_any_cm or has_any_wm) or not has_any_mask:
        print("[mh2arnold-body] No anim maps / masks connected — skipping wrinkle compositor.")
        print("[mh2arnold-body] (Body in MetaHuman typically has no wrinkles; this is normal.)")
        return

    print("[mh2arnold-body] Building wrinkle compositor (v3, HLSL ground-truth)...")

    # Build 6 reusable lerp-delta nodes (cm1/cm2/cm3 vs base diffuse,
    # wm1/wm2/wm3 vs base normal). Each is computed once, reused by all
    # contributions of that map.
    cm_deltas = {}
    base_diffuse = TEX["diffuse"]
    if cmds.objExists(base_diffuse):
        for n, fn in TEX["cm"].items():
            if cmds.objExists(fn):
                cm_deltas[n] = _build_lerp_delta(fn, base_diffuse, "cm{}".format(n))
    wm_deltas = {}
    base_normal = TEX["normal"]
    if cmds.objExists(base_normal):
        for n, fn in TEX["wm"].items():
            if cmds.objExists(fn):
                wm_deltas[n] = _build_lerp_delta(fn, base_normal, "wm{}".format(n))

    color_contribs = []
    normal_contribs = []
    skipped = 0

    # Iterate all 82 weights using the HLSL mapping.
    for weight_idx in range(82):
        if weight_idx not in HLSL_WEIGHTS_BY_CHANNEL:
            cmds.warning("[mh2arnold-body] Weight {} not in HLSL map (?)".format(weight_idx))
            skipped += 1
            continue
        ch_idx, anim_type, anim_map_idx = HLSL_WEIGHTS_BY_CHANNEL[weight_idx]

        # Resolve the mask file (runtime) and component (from HLSL convention)
        if ch_idx not in HLSL_CHANNEL_COMPONENTS:
            skipped += 1
            continue
        comp = HLSL_CHANNEL_COMPONENTS[ch_idx]
        mask_file = TEX["masks"][ch_idx] if ch_idx < len(TEX["masks"]) else None
        if not mask_file or not cmds.objExists(mask_file):
            cmds.warning("[mh2arnold-body] Mask file for ch{} missing — skipping weight {}".format(
                ch_idx, weight_idx))
            skipped += 1
            continue

        # Resolve the source weight plug on FRM_WMmultipliers
        weight_plug = "{}.maskWeight_{:02d}".format(DX11_SHADER, weight_idx)
        srcs = cmds.listConnections(weight_plug, s=True, d=False, p=True) or []
        if not srcs:
            skipped += 1
            continue
        weight_src = srcs[0]

        # Resolve the delta vec3 plug
        if anim_type == "color":
            delta = cm_deltas.get(anim_map_idx)
        else:
            delta = wm_deltas.get(anim_map_idx)
        if delta is None:
            skipped += 1
            continue

        mask_plug = _mask_component_plug(mask_file, comp)

        kind = "col" if anim_type == "color" else "nrm"
        out = _build_contribution(delta, mask_plug, weight_src, weight_idx, kind)

        if anim_type == "color":
            color_contribs.append(out)
        else:
            normal_contribs.append(out)

    print("[mh2arnold-body]   Built {} color contribs, {} normal contribs ({} skipped).".format(
        len(color_contribs), len(normal_contribs), skipped))

    # === COLOR: contribs → sum → × intensity → + base → baseColor ===
    if color_contribs:
        color_sum = _sum_contributions(color_contribs, "color")
        amp_col = _create_node("multiplyDivide", "body_amp_color")
        cmds.setAttr(amp_col + ".operation", 1)
        _connect(color_sum, amp_col + ".input1")
        _broadcast_scalar_to_vec3(amp_col, ai_shader + ".wrinkleColorIntensity")
        final_color = _add_base_and_delta(TEX["diffuse"] + ".outColor",
                                          amp_col + ".output", "color")
        _connect(final_color, ai_shader + ".baseColor")
        print("[mh2arnold-body]   baseColor ← base + (Σ color contribs × wrinkleColorIntensity)")

    # === NORMAL: contribs → sum → × intensity → + base → aiNormalMap.input ===
    if normal_contribs:
        normal_sum = _sum_contributions(normal_contribs, "normal")
        amp_nrm = _create_node("multiplyDivide", "body_amp_normal")
        cmds.setAttr(amp_nrm + ".operation", 1)
        _connect(normal_sum, amp_nrm + ".input1")
        _broadcast_scalar_to_vec3(amp_nrm, ai_shader + ".wrinkleNormalIntensity")
        final_normal = _add_base_and_delta(TEX["normal"] + ".outColor",
                                           amp_nrm + ".output", "normal")
        nmap = "body_aiNormalMap"
        if cmds.objExists(nmap):
            _connect(final_normal, nmap + ".input")
            print("[mh2arnold-body]   aiNormalMap.input ← base + (Σ normal contribs × wrinkleNormalIntensity)")
        else:
            cmds.warning("[mh2arnold-body] aiNormalMap node missing — run convert() first.")

    print("[mh2arnold-body] Wrinkle compositor done.")
    print("[mh2arnold-body] Debug: set head_aiStandardSurface.wrinkleColorIntensity")
    print("[mh2arnold-body]        and wrinkleNormalIntensity to 5-10 to amplify wrinkles.")
    print("[mh2arnold-body] Or call: conv.set_debug_intensity(color=5, normal=5)")


# Backwards-compat alias for older v1 callers
def build_wrinkle_compositor_stub(ai_shader):
    """Deprecated alias — calls the real compositor."""
    build_wrinkle_compositor(ai_shader)


# ============================================================================
# MAIN ENTRY POINTS
# ============================================================================

def convert(with_wrinkles=True):
    """Build the Arnold network and override the shadingEngine assignment.

    Args:
        with_wrinkles: if True (default), also build the v2 wrinkle compositor
                       (~135 nodes, full per-zone color/normal deltas driven by
                       the rig). Set to False to test/iterate the base shader
                       in isolation.
    """
    _ensure_arnold()

    if not _resolve_dx11_shader():
        cmds.error("[mh2arnold-body] DX11 shader not found (tried: shader_body, shader_body_shader, ...).")
        return

    print("[mh2arnold-body] === STARTING CONVERSION ===")
    _cleanup_previous_run()
    _autodetect_textures()
    _setup_color_spaces()
    print("[mh2arnold-body] Color spaces configured.")

    ai = _create_node("aiStandardSurface", ARNOLD_SHADER, as_what="asShader")

    _build_base_color(ai)
    _build_normal(ai)
    _build_cavity_into_specular(ai)
    _set_sss_and_spec(ai)

    # v2 — wrinkle compositor (overrides baseColor and aiNormalMap.input)
    if with_wrinkles:
        build_wrinkle_compositor(ai)

    # Override the shadingEngine's surfaceShader: dx11 → arnold
    sg = _get_shadingengine(DX11_SHADER)
    if sg:
        _connect(ai + ".outColor", sg + ".surfaceShader")
        print("[mh2arnold-body] shadingEngine '{}' surfaceShader -> {}".format(sg, ai))
    else:
        cmds.warning("[mh2arnold-body] No shadingEngine found for {}".format(DX11_SHADER))

    print("[mh2arnold-body] === DONE ===")
    print("[mh2arnold-body] Created shader: {}".format(ai))
    if with_wrinkles:
        print("[mh2arnold-body] Wrinkle deltas: ACTIVE (animated via FRM_WMmultipliers).")
    else:
        print("[mh2arnold-body] Wrinkle deltas: SKIPPED (with_wrinkles=False).")
    print("[mh2arnold-body] Test render to validate.")
    return ai


def revert_to_dx11():
    """Restore the DX11 shader as the shadingEngine's surfaceShader."""
    if not _resolve_dx11_shader():
        cmds.error("[mh2arnold-body] DX11 shader not found.")
        return
    sg = _get_shadingengine(DX11_SHADER)
    if not sg:
        # Look for the SG that has our Arnold shader assigned and find its mesh
        sgs = cmds.listConnections(ARNOLD_SHADER, type="shadingEngine") or []
        if sgs:
            sg = sgs[0]
        else:
            cmds.error("[mh2arnold-body] Cannot locate head shadingEngine.")
            return
    _connect(DX11_SHADER + ".outColor", sg + ".surfaceShader")
    print("[mh2arnold-body] Reverted: {} -> {}".format(DX11_SHADER, sg))


def viewport_debug_on(channel="color"):
    """Swap the head's shadingEngine to a `surfaceShader` (unlit, VP2-friendly)
    that displays the raw wrinkle delta signal in the viewport, in real-time
    as you scrub the rig controllers.

    What you see in the viewport:
        - Pure black     = no wrinkle firing in this area
        - Bright color   = a zone is active here (the brighter, the stronger)

    Workflow for diagnosing a mismatched zone:
        conv.viewport_debug_on("color")     # use color delta channel
        conv.set_debug_intensity(20, 0)     # crank way up to see clearly
        # Now grab a single rig controller (e.g. smile_L) and move it.
        # Watch which face area lights up. That's the zone currently
        # paired with that controller. If wrong, add to ZONE_MAPPING_OVERRIDE.
        conv.viewport_debug_off()           # restore aiStandardSurface

    Args:
        channel: "color" to visualize color deltas (most useful — face
                 areas light up directly with delta hue), or "normal" to
                 visualize normal deltas (less intuitive — normal maps
                 encoded as RGB, but still shows zone activation).
    """
    if channel == "color":
        src_node = "body_amp_color"
    elif channel == "normal":
        src_node = "body_amp_normal"
    else:
        cmds.error("[mh2arnold-body] channel must be 'color' or 'normal'.")
        return

    if not cmds.objExists(src_node):
        cmds.error("[mh2arnold-body] '{}' not found — run convert() first.".format(src_node))
        return

    debug_shader = "body_DEBUG_surfaceShader"
    if not cmds.objExists(debug_shader):
        debug_shader = cmds.shadingNode("surfaceShader", asShader=True,
                                         name=debug_shader)
    _connect(src_node + ".output", debug_shader + ".outColor")

    sg = _get_shadingengine(ARNOLD_SHADER) or _get_shadingengine(DX11_SHADER)
    if not sg:
        cmds.warning("[mh2arnold-body] No shadingEngine found.")
        return
    _connect(debug_shader + ".outColor", sg + ".surfaceShader")

    print("[mh2arnold-body] VIEWPORT DEBUG ON ({}) — head shows raw {} delta.".format(channel, channel))
    print("[mh2arnold-body]   Black = no wrinkle. Bright = zone firing.")
    print("[mh2arnold-body]   Crank intensity with conv.set_debug_intensity({}=20).".format(channel))
    print("[mh2arnold-body]   Restore with conv.viewport_debug_off().")


def viewport_debug_off():
    """Restore the aiStandardSurface as the head's surface shader.

    Reverts the swap done by viewport_debug_on(). The debug surfaceShader
    node is kept in the scene (cheap, no harm) so re-enabling debug is fast.
    """
    if not cmds.objExists(ARNOLD_SHADER):
        cmds.error("[mh2arnold-body] {} not found.".format(ARNOLD_SHADER))
        return
    sg = (_get_shadingengine(ARNOLD_SHADER)
          or _get_shadingengine("body_DEBUG_surfaceShader")
          or _get_shadingengine(DX11_SHADER))
    if not sg:
        cmds.warning("[mh2arnold-body] No shadingEngine found.")
        return
    _connect(ARNOLD_SHADER + ".outColor", sg + ".surfaceShader")
    print("[mh2arnold-body] VIEWPORT DEBUG OFF — aiStandardSurface restored.")


def set_debug_intensity(color=5.0, normal=5.0):
    """Quickly set the wrinkle intensity attrs for visual debugging.

    Args:
        color:  multiplier for the color wrinkle deltas (1.0 = normal,
                5-10 = amplified, 0 = disabled).
        normal: multiplier for the normal wrinkle deltas (same scale).

    Usage:
        conv.set_debug_intensity(5, 5)    # amplify both for debugging
        conv.set_debug_intensity(10, 0)   # only color, isolate which area lights up
        conv.set_debug_intensity(0, 10)   # only normal
        conv.set_debug_intensity(1, 1)    # back to final-render values
    """
    if not cmds.objExists(ARNOLD_SHADER):
        cmds.warning("[mh2arnold-body] {} not found — run convert() first.".format(ARNOLD_SHADER))
        return
    for attr_name, value in (("wrinkleColorIntensity", color),
                              ("wrinkleNormalIntensity", normal)):
        plug = ARNOLD_SHADER + "." + attr_name
        if cmds.attributeQuery(attr_name, node=ARNOLD_SHADER, exists=True):
            cmds.setAttr(plug, value)
        else:
            cmds.warning("[mh2arnold-body] Attribute {} missing — re-run convert().".format(attr_name))
    print("[mh2arnold-body] Wrinkle intensity set: color={}, normal={}".format(color, normal))


def diagnose(zone_filter=None, weight_filter=None):
    """Print the HLSL-extracted weight → channel → mask mapping, with the
    zone names from the user attribute drivers for cross-reference.

    Use this to verify the mapping is plugged in correctly and to identify
    which weight controls which zone of the face.

    Args:
        zone_filter:   substring to match in zone names (e.g. "smile", "blink_R")
        weight_filter: specific weight index (e.g. 18) to inspect only that one
    """
    weights = _parse_weight_drivers()
    weights_by_idx = {w["weight_idx"]: w for w in weights}

    print("=== HLSL weight → channel mapping ({} weights) ===".format(
        len(HLSL_WEIGHTS_BY_CHANNEL)))
    print(" w_idx  type    map  ch  mask_file.comp        zone (from rig)")
    print(" -----  ------  ---  --  ---------------       ----------------")

    for w_idx in sorted(HLSL_WEIGHTS_BY_CHANNEL.keys()):
        if weight_filter is not None and w_idx != weight_filter:
            continue
        ch_idx, anim_type, map_idx = HLSL_WEIGHTS_BY_CHANNEL[w_idx]
        comp = HLSL_CHANNEL_COMPONENTS.get(ch_idx, "?")
        mask_file = TEX["masks"][ch_idx] if ch_idx < len(TEX["masks"]) else None
        short_fn = (mask_file or "?").replace("maskFile_body_", "").replace("maskFile_", "")

        zone = "(no driver)"
        if w_idx in weights_by_idx:
            zone = weights_by_idx[w_idx]["zone"]

        if zone_filter and zone_filter not in zone:
            continue

        print("  {:3d}    {:6s}  cm{}  {:2d}  {:18s}.{}  {}".format(
            w_idx, anim_type, map_idx, ch_idx, short_fn, comp, zone))


# Convenience: allow running as a script
if __name__ == "__main__":
    convert()
