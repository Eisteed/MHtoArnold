"""
mh_teeth_to_arnold.py
=======================
MetaHuman teeth shader → Arnold aiStandardSurface converter.

Source: dx11_shd_teeth.fx (MetaHumanForMaya plugin v1.3.x).
Same skin-shader template as the head, minus the wrinkle compositor.
Tuned for tooth enamel (translucent, glossy with subtle SSS through enamel,
warm yellowish scatter color, IOR ~1.55 of dental enamel).

USAGE
-----
    import mh_teeth_to_arnold as eye_l
    eye_l.convert()
    teeth.revert_to_dx11()    # restore DX11 viewport assignment

DESIGN NOTES
------------
- File nodes are auto-discovered from connections on the DX11 shader, so this
  works regardless of the MetaHuman character's name (`baseMapFile_eyeLeft_*`,
  `baseMapFile_<charName>_eyeLeft_*`, etc.).
- The DX11 shader is left intact in the scene; only the SG.surfaceShader is
  rerouted. revert_to_dx11() restores the original assignment.
"""

import maya.cmds as cmds


# ============================================================================
# CONFIGURATION
# ============================================================================

DX11_SHADER   = "shader_teeth_shader"
ARNOLD_SHADER = "teeth_aiStandardSurface"

# Eyeball preset — wet, glossy surface. Adjust per character.
# The diffuse texture already contains all the eye color information
# (sclera, iris, pupil, limbus) so we just need a tight specular for highlights.
TEETH_PRESET = {
    # Enamel preset — translucent, slightly yellowish SSS, glossy.
    "subsurface":          0.30,       # subtle translucency through enamel
    "subsurfaceColor":     (0.95, 0.85, 0.70),   # warm cream
    "subsurfaceRadius":    (0.40, 0.30, 0.20),   # cm — short scatter (enamel is dense)
    "subsurfaceScale":     0.10,
    "subsurfaceType":      2,                    # randomwalk_v2
    "specular":            0.85,
    "specularRoughness":   0.20,
    "specularIOR":         1.55,       # dental enamel refractive index
    "coat":                0.10,       # subtle clear coat for that wet enamel sheen
    "coatRoughness":       0.15,
    "coatIOR":             1.50,
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
                print("[mh2arnold-teeth] Resolved DX11 shader '{}' (was '{}')".format(name, DX11_SHADER))
                DX11_SHADER = name
            return name
    return None


def _ensure_arnold():
    if not cmds.pluginInfo("mtoa", q=True, loaded=True):
        cmds.loadPlugin("mtoa")


def _set_color_space(node, space):
    if not node or not cmds.objExists(node):
        return
    try:
        cmds.setAttr(node + ".ignoreColorSpaceFileRules", 1)
        cmds.setAttr(node + ".colorSpace", space, type="string")
    except Exception as e:
        cmds.warning("[mh2arnold-teeth] colorSpace fail on {}: {}".format(node, e))


def _create_node(node_type, name, as_what="asUtility"):
    if cmds.objExists(name):
        cmds.delete(name)
    return cmds.shadingNode(node_type, **{as_what: True, "name": name})


def _connect(src, dst, force=True):
    cmds.connectAttr(src, dst, force=force)


def _autodetect_textures(dx11_shader):
    """Find file nodes connected to the DX11 shader's main texture inputs.

    Returns a dict { 'diffuse': nodeName | None, 'normal': ..., 'spec': ...,
                     'cavity': ..., 'occlusion': ..., 'sssRadius': ... }
    """
    if not cmds.objExists(dx11_shader):
        cmds.error("[mh2arnold-teeth] '{}' not found.".format(dx11_shader))
        return {}

    # DX11 shader attribute names (defined in dx11_shd_teeth.fx)
    attrs = {
        "diffuse":   "DiffuseTexture",
        "normal":    "NormalTexture",
        "spec":      "SpecularTexture",
        "cavity":    "CavityTexture",
        "occlusion": "OcclusionTexture",
        "sssRadius": "ScatteringRadiusTexture",
    }
    found = {}
    for key, attr in attrs.items():
        plug = dx11_shader + "." + attr
        if not cmds.attributeQuery(attr, node=dx11_shader, exists=True):
            found[key] = None
            continue
        srcs = cmds.listConnections(plug, s=True, d=False, type="file") or []
        found[key] = srcs[0] if srcs else None
    return found


def _has_valid_path(file_node):
    if not file_node or not cmds.objExists(file_node):
        return False
    p = cmds.getAttr(file_node + ".fileTextureName") or ""
    return bool(p) and not p.endswith(("/", "\\"))


def _silence_empty(file_node):
    """Blank empty paths to suppress 'Failed to open texture' warnings."""
    if not file_node or not cmds.objExists(file_node):
        return
    p = cmds.getAttr(file_node + ".fileTextureName") or ""
    if p.endswith(("/", "\\")):
        try:
            cmds.setAttr(file_node + ".fileTextureName", "", type="string")
        except Exception:
            pass


def _get_shadingengine(shader):
    """Robust SG lookup. Survives re-runs after cleanup deletes the shader."""
    sgs = cmds.listConnections(shader + ".outColor", type="shadingEngine") or []
    if sgs:
        return sgs[0]
    sg_name = shader + "SG"
    if cmds.objExists(sg_name) and cmds.nodeType(sg_name) == "shadingEngine":
        return sg_name
    sgs = cmds.listConnections(shader, type="shadingEngine") or []
    return sgs[0] if sgs else None


def _cleanup_previous_run():
    for n in (ARNOLD_SHADER, "teeth_aiNormalMap", "teeth_specCavityMul",
              "teeth_DEBUG_surfaceShader"):
        if cmds.objExists(n):
            try:
                cmds.delete(n)
            except Exception:
                pass


# ============================================================================
# MAIN
# ============================================================================

def convert():
    """Build the Arnold network and rewire the eyeLeft shadingEngine."""
    _ensure_arnold()
    if not _resolve_dx11_shader():
        cmds.error("[mh2arnold-teeth] DX11 shader not found (tried multiple variants).")
        return

    print("[mh2arnold-teeth] === STARTING CONVERSION (teeth) ===")
    _cleanup_previous_run()

    tex = _autodetect_textures(DX11_SHADER)
    print("[mh2arnold-teeth] Auto-detected textures:")
    for k, v in tex.items():
        print("  {:10s}: {}".format(k, v or "(none)"))

    # Color space hygiene
    if tex["diffuse"]:
        _set_color_space(tex["diffuse"], "sRGB")
    for key in ("normal", "spec", "cavity", "sssRadius", "occlusion"):
        if tex[key]:
            _set_color_space(tex[key], "Raw")

    # Create the Arnold shader
    ai = _create_node("aiStandardSurface", ARNOLD_SHADER, as_what="asShader")

    # --- BASE COLOR ---
    if _has_valid_path(tex["diffuse"]):
        _connect(tex["diffuse"] + ".outColor", ai + ".baseColor")
        cmds.setAttr(ai + ".base", 1.0)

    # --- NORMAL ---
    if _has_valid_path(tex["normal"]):
        nmap = _create_node("aiNormalMap", "teeth_aiNormalMap")
        _connect(tex["normal"] + ".outColor", nmap + ".input")
        cmds.setAttr(nmap + ".tangentSpace", 1)
        cmds.setAttr(nmap + ".invertY", 0)
        cmds.setAttr(nmap + ".colorToSigned", 1)
        _connect(nmap + ".outValue", ai + ".normalCamera")

    # --- SPECULAR (modulated by cavity if present) ---
    cmds.setAttr(ai + ".specularRoughness", TEETH_PRESET["specularRoughness"])
    cmds.setAttr(ai + ".specularIOR",       TEETH_PRESET["specularIOR"])
    if _has_valid_path(tex["cavity"]):
        mul = _create_node("multiplyDivide", "teeth_specCavityMul")
        cmds.setAttr(mul + ".operation", 1)
        cmds.setAttr(mul + ".input1X", TEETH_PRESET["specular"])
        _connect(tex["cavity"] + ".outColorR", mul + ".input2X")
        _connect(mul + ".outputX", ai + ".specular")
    else:
        _silence_empty(tex["cavity"])
        cmds.setAttr(ai + ".specular", TEETH_PRESET["specular"])

    # If a dedicated specular texture exists, also drive specularColor with it
    # (rare on eyes but cheap insurance).
    if _has_valid_path(tex["spec"]):
        _connect(tex["spec"] + ".outColor", ai + ".specularColor")

    # --- SSS (translucent enamel) ---
    cmds.setAttr(ai + ".subsurface",       TEETH_PRESET["subsurface"])
    cmds.setAttr(ai + ".subsurfaceColor",  *TEETH_PRESET["subsurfaceColor"], type="double3")
    cmds.setAttr(ai + ".subsurfaceRadius", *TEETH_PRESET["subsurfaceRadius"], type="double3")
    cmds.setAttr(ai + ".subsurfaceScale",  TEETH_PRESET["subsurfaceScale"])
    cmds.setAttr(ai + ".subsurfaceType",   TEETH_PRESET["subsurfaceType"])

    # --- COAT ---
    cmds.setAttr(ai + ".coat",          TEETH_PRESET["coat"])
    cmds.setAttr(ai + ".coatRoughness", TEETH_PRESET["coatRoughness"])
    cmds.setAttr(ai + ".coatIOR",       TEETH_PRESET["coatIOR"])

    # Override the shadingEngine surfaceShader
    sg = _get_shadingengine(DX11_SHADER)
    if sg:
        _connect(ai + ".outColor", sg + ".surfaceShader")
        print("[mh2arnold-teeth] SG '{}' surfaceShader -> {}".format(sg, ai))
    else:
        cmds.warning("[mh2arnold-teeth] No shadingEngine found for {}".format(DX11_SHADER))

    print("[mh2arnold-teeth] === DONE ===")
    return ai


def revert_to_dx11():
    if not _resolve_dx11_shader():
        cmds.error("[mh2arnold-teeth] DX11 shader not found.")
        return
    sg = _get_shadingengine(DX11_SHADER) or _get_shadingengine(ARNOLD_SHADER)
    if not sg:
        cmds.error("[mh2arnold-teeth] No shadingEngine to restore.")
        return
    _connect(DX11_SHADER + ".outColor", sg + ".surfaceShader")
    print("[mh2arnold-teeth] Reverted: {} -> {}".format(DX11_SHADER, sg))


if __name__ == "__main__":
    convert()
