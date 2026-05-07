"""
mh_extras_to_arnold.py
======================
Converts the 5 MetaHuman "extras" blinn shaders to aiStandardSurface:

    - shader_eyeshell_shader        → cornea (transmissive dome over eyeball)
    - shader_saliva_shader          → mouth saliva meniscus (water-like)
    - shader_eyelashes_shader       → lashes (opacity-mapped cards)
    - shader_eyelashesShadow_shader → fake contact shadow plane
    - shader_eyeEdge_shader         → wet lacrimal rim (skin + spec)

These are simpler than head/body — no wrinkle compositor, no anim maps,
just attribute-level conversion with type-appropriate presets.

USAGE
-----
    import mh_extras_to_arnold as mhx
    mhx.convert_all()              # do all 5
    mhx.convert_eyeshell()         # one at a time
    mhx.revert_all()               # restore the blinns

DESIGN
------
- The blinn shader is NOT deleted. Only the shadingEngine.surfaceShader is
  rewired to the new Arnold node. revert_*() flips it back.
- Each conversion preserves the blinn's static color and any incoming
  texture connections (e.g. an alpha map on eyelashes' .transparency).
- Cornea / saliva get hard-coded transmissive presets — those are what
  makes the eyes/mouth look believable in render.
"""

import maya.cmds as cmds


# ============================================================================
# CONFIG
# ============================================================================

EXTRAS = {
    "eyeshell":        {"src": "shader_eyeshell_shader",
                        "ai":  "eyeshell_aiStandardSurface"},
    "saliva":          {"src": "shader_saliva_shader",
                        "ai":  "saliva_aiStandardSurface"},
    "eyelashes":       {"src": "shader_eyelashes_shader",
                        "ai":  "eyelashes_aiStandardSurface"},
    "eyelashesShadow": {"src": "shader_eyelashesShadow_shader",
                        "ai":  "eyelashesShadow_aiStandardSurface"},
    "eyeEdge":         {"src": "shader_eyeEdge_shader",
                        "ai":  "eyeEdge_aiStandardSurface"},
}


# ============================================================================
# UTILITIES
# ============================================================================

def _ensure_arnold():
    if not cmds.pluginInfo("mtoa", q=True, loaded=True):
        cmds.loadPlugin("mtoa")


def _resolve_src(part_key):
    """Try the configured name, then variants (e.g. without `_shader` suffix).
    Returns the actual node name if found, else None. Updates EXTRAS if a
    variant matched."""
    info = EXTRAS[part_key]
    candidates = [info["src"]]
    base = info["src"].replace("_shader", "")
    for v in (base, base + "_shader"):
        if v not in candidates:
            candidates.append(v)
    for name in candidates:
        if cmds.objExists(name):
            if name != info["src"]:
                print("[mh-extras] Resolved '{}' (was '{}')".format(name, info["src"]))
                info["src"] = name
            return name
    return None


def _create(node_type, name, as_what="asShader"):
    if cmds.objExists(name):
        cmds.delete(name)
    return cmds.shadingNode(node_type, **{as_what: True, "name": name})


def _connect(src, dst, force=True):
    cmds.connectAttr(src, dst, force=force)


def _get_shadingengine(shader):
    """Robust SG lookup: direct connection, then by Maya naming convention."""
    sgs = cmds.listConnections(shader + ".outColor", type="shadingEngine") or []
    if sgs:
        return sgs[0]
    sg = shader + "SG"
    if cmds.objExists(sg) and cmds.nodeType(sg) == "shadingEngine":
        return sg
    sgs = cmds.listConnections(shader, type="shadingEngine") or []
    return sgs[0] if sgs else None


def _src_plug(node, attr):
    """Return the source plug feeding node.attr, or None if it's a static value."""
    if not cmds.objExists(node):
        return None
    if not cmds.attributeQuery(attr, node=node, exists=True):
        return None
    srcs = cmds.listConnections(node + "." + attr, s=True, d=False, p=True) or []
    return srcs[0] if srcs else None


def _find_upstream_file(blinn_node, prefer_attrs=("color", "transparency")):
    """Locate a file node that feeds this blinn shader.

    Search order:
      1. Direct connection on `prefer_attrs` (color, then transparency)
      2. Walk upstream from those plugs, looking for the first `file` node
      3. As a last resort, scan the entire upstream history of the blinn

    Returns the file node name, or None if nothing found.
    """
    # Try preferred attributes first
    for attr in prefer_attrs:
        plug = _src_plug(blinn_node, attr)
        if not plug:
            continue
        node = plug.split(".")[0]
        if cmds.nodeType(node) == "file":
            return node
        # Walk upstream from this intermediate node
        hist = cmds.listHistory(node, leaf=False) or []
        for h in hist:
            if cmds.nodeType(h) == "file":
                return h
    # Last resort: anything upstream of the blinn
    hist = cmds.listHistory(blinn_node, leaf=False) or []
    for h in hist:
        if cmds.nodeType(h) == "file":
            return h
    return None


def _find_file_by_pattern(patterns):
    """Search ALL file nodes in the scene by node name OR fileTextureName.
    Returns the first file node whose name or texture path contains any of
    the (case-insensitive) patterns. Used as a last-resort fallback when a
    blinn shader has no upstream texture (e.g. MetaHuman lashes where the
    file node is in the scene but not wired to the blinn)."""
    files = cmds.ls(type="file") or []
    # Pass 1: by node name
    for f in files:
        lname = f.lower()
        for p in patterns:
            if p.lower() in lname:
                return f
    # Pass 2: by texture path
    for f in files:
        try:
            path = cmds.getAttr(f + ".fileTextureName") or ""
            lpath = path.lower()
            for p in patterns:
                if p.lower() in lpath:
                    return f
        except Exception:
            continue
    return None


def _get_color(node, attr, default=(1.0, 1.0, 1.0)):
    """Read a color attr value (returns 3-tuple)."""
    if not cmds.objExists(node) or not cmds.attributeQuery(attr, node=node, exists=True):
        return default
    try:
        v = cmds.getAttr(node + "." + attr)
        if isinstance(v, list) and v and isinstance(v[0], tuple):
            return v[0]
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            return tuple(v[:3])
    except Exception:
        pass
    return default


def _override_sg(src_blinn, ai_shader):
    """Rewire the shadingEngine so it uses ai_shader instead of src_blinn."""
    sg = _get_shadingengine(src_blinn)
    if not sg:
        cmds.warning("[mh-extras] No shadingEngine found for {}".format(src_blinn))
        return
    _connect(ai_shader + ".outColor", sg + ".surfaceShader")
    print("[mh-extras] SG '{}' surfaceShader -> {}".format(sg, ai_shader))


def _restore_sg(src_blinn):
    """Reconnect the original blinn to its shadingEngine.surfaceShader."""
    sg = _get_shadingengine(src_blinn)
    if not sg:
        # The Arnold shader might be the one currently bound — find SG via the ai
        for info in EXTRAS.values():
            sgs = cmds.listConnections(info["ai"], type="shadingEngine") or []
            for s in sgs:
                # Match by name proximity
                if src_blinn.replace("shader_", "").replace("_shader", "") in s:
                    sg = s
                    break
            if sg:
                break
    if not sg:
        cmds.warning("[mh-extras] Cannot find SG for {}".format(src_blinn))
        return
    _connect(src_blinn + ".outColor", sg + ".surfaceShader")
    print("[mh-extras] Reverted '{}' -> {}".format(src_blinn, sg))



def _set_shape_aiOpaque(src_blinn, opaque=False):
    """Set aiOpaque on all shapes assigned to src_blinn's shadingEngine.

    aiOpaque=False tells Arnold the surface may have alpha/opacity, so the
    alpha is properly evaluated in render.
    """
    sg = _get_shadingengine(src_blinn)
    if not sg:
        return
    members = cmds.sets(sg, q=True) or []
    shapes = []
    for m in members:
        node = m.split(".")[0]
        if not cmds.objExists(node):
            continue
        if cmds.nodeType(node) in ("mesh", "nurbsSurface"):
            shapes.append(node)
        else:
            rels = cmds.listRelatives(node, s=True, ni=True,
                                       type=("mesh", "nurbsSurface")) or []
            shapes.extend(rels)
    for shape in set(shapes):
        if not cmds.attributeQuery("aiOpaque", node=shape, exists=True):
            try:
                cmds.addAttr(shape, longName="aiOpaque", attributeType="bool",
                             defaultValue=True, keyable=False)
            except Exception as e:
                cmds.warning("[mh-extras] Could not addAttr aiOpaque on {}: {}".format(shape, e))
                continue
        cmds.setAttr(shape + ".aiOpaque", 1 if opaque else 0)
        print("[mh-extras] Set aiOpaque={} on {}".format(int(bool(opaque)), shape))



def _cleanup_orphan_helpers():
    """Remove utility nodes left from previous (older) convert attempts so
    Hypershade stays clean."""
    candidates = [
        "eyelashes_opacity_invert",
        "eyelashesShadow_opacity_invert",
        "Eyelashes_remapAlpha",
        "eyelashes_remapAlpha",
        # Cleanup of older dual-shader attempt (in case scene still has them)
        "eyelashes_viewportLambert",
        "eyelashes_viewportLambert_to_transparency",
        "eyelashesShadow_viewportLambert",
        "eyelashesShadow_viewportLambert_to_transparency",
    ]
    for n in candidates:
        if cmds.objExists(n):
            try:
                cmds.delete(n)
                print("[mh-extras] Cleaned up orphan node: {}".format(n))
            except Exception:
                pass


# ============================================================================
# PER-SHADER CONVERTERS
# ============================================================================

def convert_eyeshell():
    """The cornea — the transparent dome that sits over the eyeball.
    Refractive, IOR=1.376 (cornea/aqueous humor), no diffuse.
    This shader is responsible for the realistic refraction of the iris."""
    _ensure_arnold()
    src = _resolve_src("eyeshell")
    if not src:
        cmds.warning("[mh-extras] {} not in scene.".format(EXTRAS["eyeshell"]["src"]))
        return

    ai = _create("aiStandardSurface", EXTRAS["eyeshell"]["ai"])
    cmds.setAttr(ai + ".base",                0.0)              # no diffuse — pure clear medium
    cmds.setAttr(ai + ".specular",            1.0)
    cmds.setAttr(ai + ".specularRoughness",   0.05)             # very tight highlights
    cmds.setAttr(ai + ".specularIOR",         1.376)            # cornea IOR
    cmds.setAttr(ai + ".transmission",        1.0)              # the magic — full transmission
    cmds.setAttr(ai + ".transmissionColor",   1.0, 1.0, 1.0, type="double3")
    cmds.setAttr(ai + ".thinWalled",          0)                # real volume (curved dome)
    # Cosmetic: a tiny amount of subsurface for the wet "tear film" tinting
    cmds.setAttr(ai + ".subsurface",          0.0)
    _override_sg(src, ai)
    _set_shape_aiOpaque(src, opaque=False)
    return ai


def convert_saliva():
    """Mouth saliva meniscus — water-like, IOR=1.33."""
    _ensure_arnold()
    src = _resolve_src("saliva")
    if not src:
        cmds.warning("[mh-extras] {} not in scene.".format(EXTRAS["saliva"]["src"]))
        return

    ai = _create("aiStandardSurface", EXTRAS["saliva"]["ai"])
    cmds.setAttr(ai + ".base",              0.0)
    cmds.setAttr(ai + ".specular",          1.0)
    cmds.setAttr(ai + ".specularRoughness", 0.10)
    cmds.setAttr(ai + ".specularIOR",       1.33)               # water IOR
    cmds.setAttr(ai + ".transmission",      1.0)
    cmds.setAttr(ai + ".transmissionColor", 1.0, 1.0, 1.0, type="double3")
    cmds.setAttr(ai + ".thinWalled",        0)
    _override_sg(src, ai)
    _set_shape_aiOpaque(src, opaque=False)
    return ai


def convert_eyelashes():
    """Lashes — opacity-mapped hair cards. Dark color, slight translucency.

    The MetaHuman lashes texture (`Eyelashes_Color.png`) encodes the lash
    silhouette directly in the RGB values (black background, colored
    eyelash strands). So we wire the same outColor to BOTH baseColor and
    opacity — no reverse node, no remap. Black regions = transparent."""
    _ensure_arnold()
    src = _resolve_src("eyelashes")
    if not src:
        cmds.warning("[mh-extras] {} not in scene.".format(EXTRAS["eyelashes"]["src"]))
        return

    _cleanup_orphan_helpers()
    ai = _create("aiStandardSurface", EXTRAS["eyelashes"]["ai"])

    # Find the lashes texture anywhere in the blinn's upstream history.
    # MetaHuman wires it to .transparency (sometimes via remap/reverse helpers
    # that we just cleaned up), not always to .color.
    # Strategy 1: walk upstream from the blinn (works if texture is wired
    # to .color or .transparency).
    file_node = _find_upstream_file(src, prefer_attrs=("color", "transparency"))
    # Strategy 2: scene-wide search by name/path. The MetaHuman lashes file
    # is often present in the scene but disconnected from the blinn.
    if not file_node:
        file_node = _find_file_by_pattern(["eyelashes_color", "eyelash_color",
                                            "eyelashes", "eyelash", "lashes"])
        if file_node:
            print("[mh-extras] eyelashes: found '{}' in scene (no blinn connection)".format(file_node))

    # baseColor stays a flat dark value — the texture's RGB is just the
    # alpha mask, not the actual lash color. Wiring it to baseColor would
    # make the lashes look colored where the alpha is white.
    c = _get_color(src, "color", (0.02, 0.02, 0.02))
    cmds.setAttr(ai + ".baseColor", c[0], c[1], c[2], type="double3")

    # Force opacity static value to (1,1,1) BEFORE connecting the texture.
    # MtoA defaults aiStandardSurface.opacity to (0,0,0), which makes VP2 think
    # the surface is fully transparent and falls back to opaque rendering.
    cmds.setAttr(ai + ".opacity", 1, 1, 1, type="double3")
    if file_node:
        _connect(file_node + ".outColor", ai + ".opacity")
        print("[mh-extras] eyelashes: wired {}.outColor -> opacity".format(file_node))
    else:
        cmds.warning("[mh-extras] No eyelashes texture found anywhere in scene - using static fallback.")
        t = _get_color(src, "transparency", (0.0, 0.0, 0.0))
        op = (1.0 - t[0], 1.0 - t[1], 1.0 - t[2])
        cmds.setAttr(ai + ".opacity", op[0], op[1], op[2], type="double3")
    cmds.setAttr(ai + ".base", 1.0)

    cmds.setAttr(ai + ".specular",          0.30)
    cmds.setAttr(ai + ".specularRoughness", 0.40)
    cmds.setAttr(ai + ".specularIOR",       1.45)
    cmds.setAttr(ai + ".thinWalled",        1)            # cards: no volumetric SSS

    _override_sg(src, ai)
    _set_shape_aiOpaque(src, opaque=False)
    return ai


def convert_eyelashesShadow():
    """Fake contact-shadow plane under the eyelashes — black with alpha.

    Same approach as eyelashes: the source blinn's color file (or its
    transparency-tied texture) encodes the alpha mask in RGB. We wire the
    same outColor to opacity. baseColor stays black for a darkening effect."""
    _ensure_arnold()
    src = _resolve_src("eyelashesShadow")
    if not src:
        cmds.warning("[mh-extras] {} not in scene.".format(EXTRAS["eyelashesShadow"]["src"]))
        return

    _cleanup_orphan_helpers()
    ai = _create("aiStandardSurface", EXTRAS["eyelashesShadow"]["ai"])
    cmds.setAttr(ai + ".base",             1.0)
    cmds.setAttr(ai + ".baseColor",        0.0, 0.0, 0.0, type="double3")
    cmds.setAttr(ai + ".diffuseRoughness", 1.0)
    cmds.setAttr(ai + ".specular",         0.0)
    cmds.setAttr(ai + ".coat",             0.0)
    cmds.setAttr(ai + ".thinWalled",       1)

    # Strategy 1: upstream of the blinn
    file_node = _find_upstream_file(src, prefer_attrs=("color", "transparency"))
    # Strategy 2: scene-wide search — try shadow-specific names first,
    # then fall back to lashes (the shadow plane usually reuses the lashes alpha).
    if not file_node:
        file_node = _find_file_by_pattern(["eyelashesshadow", "lashesshadow",
                                            "lashes_shadow", "eyelashes_shadow"])
    if not file_node:
        file_node = _find_file_by_pattern(["eyelashes_color", "eyelash_color",
                                            "eyelashes", "eyelash", "lashes"])

    # Force static opacity to (1,1,1) before connecting (VP2 transparency hint)
    cmds.setAttr(ai + ".opacity", 1, 1, 1, type="double3")
    if file_node:
        _connect(file_node + ".outColor", ai + ".opacity")
        print("[mh-extras] eyelashesShadow: wired {}.outColor -> opacity".format(file_node))
    else:
        cmds.warning("[mh-extras] No lashes/shadow texture found in scene - using static fallback.")
        t = _get_color(src, "transparency", (0.5, 0.5, 0.5))
        op = (1.0 - t[0], 1.0 - t[1], 1.0 - t[2])
        cmds.setAttr(ai + ".opacity", op[0], op[1], op[2], type="double3")

    _override_sg(src, ai)
    _set_shape_aiOpaque(src, opaque=False)
    return ai


def convert_eyeEdge():
    """Wet lacrimal rim — pink flesh, glossy because wet."""
    _ensure_arnold()
    src = _resolve_src("eyeEdge")
    if not src:
        cmds.warning("[mh-extras] {} not in scene.".format(EXTRAS["eyeEdge"]["src"]))
        return

    ai = _create("aiStandardSurface", EXTRAS["eyeEdge"]["ai"])

    # Diffuse: prefer connected texture, else the blinn's color, else pinkish
    color_src = _src_plug(src, "color")
    if color_src:
        _connect(color_src, ai + ".baseColor")
    else:
        c = _get_color(src, "color", (0.65, 0.35, 0.30))
        cmds.setAttr(ai + ".baseColor", c[0], c[1], c[2], type="double3")
    cmds.setAttr(ai + ".base", 1.0)

    # Wet flesh: SSS + glossy spec
    cmds.setAttr(ai + ".subsurface",         0.30)
    cmds.setAttr(ai + ".subsurfaceColor",    0.95, 0.55, 0.45, type="double3")
    cmds.setAttr(ai + ".subsurfaceRadius",   0.40, 0.20, 0.10, type="double3")
    cmds.setAttr(ai + ".subsurfaceScale",    0.05)             # small scatter — thin tissue
    cmds.setAttr(ai + ".subsurfaceType",     2)                # randomwalk_v2

    cmds.setAttr(ai + ".specular",           1.0)
    cmds.setAttr(ai + ".specularRoughness",  0.20)
    cmds.setAttr(ai + ".specularIOR",        1.376)
    cmds.setAttr(ai + ".coat",               0.10)
    cmds.setAttr(ai + ".coatRoughness",      0.15)

    _override_sg(src, ai)
    return ai


# ============================================================================
# BULK ENTRY POINTS
# ============================================================================

_CONVERTERS = {
    "eyeshell":        convert_eyeshell,
    "saliva":          convert_saliva,
    "eyelashes":       convert_eyelashes,
    "eyelashesShadow": convert_eyelashesShadow,
    "eyeEdge":         convert_eyeEdge,
}


def convert(part):
    """Convert a single extra by key (one of EXTRAS.keys())."""
    fn = _CONVERTERS.get(part)
    if fn is None:
        cmds.error("[mh-extras] Unknown part: '{}'. Use one of: {}".format(
            part, ", ".join(_CONVERTERS.keys())))
        return None
    return fn()


def convert_all():
    """Convert all 5 extras. Skips any that aren't present in the scene."""
    print("[mh-extras] === CONVERT ALL EXTRAS ===")
    for key in EXTRAS:
        if _resolve_src(key):
            convert(key)
    print("[mh-extras] === DONE ===")
    print("[mh-extras] TIP: for cleaner VP2 transparency, set")
    print("              Renderer > Viewport 2.0 > options >")
    print("              Transparency Algorithm = 'Depth Peeling'")


def revert(part):
    """Restore the blinn shader for one part."""
    info = EXTRAS.get(part)
    if not info:
        cmds.error("[mh-extras] Unknown part: '{}'.".format(part))
        return
    if not _resolve_src(part):
        cmds.warning("[mh-extras] Source blinn '{}' not found.".format(info["src"]))
        return
    _restore_sg(info["src"])
    # Restore aiOpaque=True on the shape so it goes back to opaque rendering
    _set_shape_aiOpaque(info["src"], opaque=True)


def revert_all():
    """Restore all 5 blinn shaders."""
    print("[mh-extras] === REVERT ALL ===")
    for key in EXTRAS:
        if _resolve_src(key):
            revert(key)
    print("[mh-extras] === DONE ===")


# Convenience aliases (mirror the head/body API)
def revert_to_dx11():
    """Alias for revert_all() to match the rest of the pipeline's API."""
    revert_all()


if __name__ == "__main__":
    convert_all()