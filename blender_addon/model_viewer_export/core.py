"""Scene collection and export logic (no UI code).

Naming conventions (see README.md for the full description):

    CAM_home        camera used as the start / overview view
    CAM_<id>        close-up camera for hotspot <id>
    HS_<id>         empty that marks hotspot <id> (local +Z = surface normal)
    ANIM_home       action looped while in the overview (optional)
    ANIM_<id>       action played when hotspot <id> is clicked
    ANIM_<id>_exit  action played when leaving hotspot <id> (optional,
                    otherwise ANIM_<id> is played in reverse)

Explicit links set in the add-on panel (Object > Model Viewer Hotspot)
override the naming convention.

Material variants are defined in the Material Variants panel and exported
as KHR_materials_variants through the glTF add-on.

The export is split into small "collector" functions that each fill one
part of the scene.json dict. To add a feature, write a collector and add it
to COLLECTORS.
"""

import json
import math
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from .glb import GLB

SCHEMA_VERSION = 3

CAM_PREFIX = "CAM_"
HOTSPOT_PREFIX = "HS_"
ANIM_PREFIX = "ANIM_"
EXIT_SUFFIX = "_exit"
HOME_ID = "home"

TEMPLATE_DIR = Path(__file__).parent / "templates"
VIEWER_FILES = ("index.html", "viewer.js", "viewer.css")

TONE_MAPPING = {
    "AgX": "agx",
    "Filmic": "aces",
    "Khronos PBR Neutral": "neutral",
    "Standard": "none",
}


class ExportContext:
    """Shared state passed to every collector."""

    def __init__(self, context, settings, out_dir):
        self.context = context
        self.scene = context.scene
        self.settings = settings
        self.out_dir = Path(out_dir)
        self.warnings = []
        self.data = {
            "version": SCHEMA_VERSION,
            "generator": "Blender %s / model_viewer_export" % bpy.app.version_string,
            "generatedAt": int(time.time()),
            "title": settings.title or Path(bpy.data.filepath).stem or "Model",
        }

    def warn(self, msg):
        self.warnings.append(msg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_gltf(v):
    """Blender (Z up) -> glTF (Y up) coordinates."""
    return Vector((v.x, v.z, -v.y))


def rounded(v, digits=4):
    return [round(c, digits) for c in v]


def split_name(name, prefix):
    if name.startswith(prefix) and len(name) > len(prefix):
        return name[len(prefix):]
    return None


def find_cameras(scene):
    cams = {}
    for obj in scene.objects:
        cam_id = split_name(obj.name, CAM_PREFIX)
        if cam_id and obj.type == "CAMERA":
            cams[cam_id] = obj
    return cams


def find_hotspots(scene):
    spots = []
    for obj in scene.objects:
        hs_id = split_name(obj.name, HOTSPOT_PREFIX)
        if hs_id and obj.type == "EMPTY":
            spots.append((hs_id, obj))
    spots.sort(key=lambda s: (s[1].mv_hotspot.order, s[0]))
    return spots


def find_anim_actions():
    return {a.name: a for a in bpy.data.actions if a.name.startswith(ANIM_PREFIX)}


def is_helper(obj):
    return obj.name.startswith((CAM_PREFIX, HOTSPOT_PREFIX))


def export_objects(scene):
    """Objects that go into the GLB."""
    objs = []
    for obj in scene.objects:
        if is_helper(obj) or obj.type in {"CAMERA", "LIGHT", "LIGHT_PROBE", "SPEAKER"}:
            continue
        if not obj.visible_get() or obj.hide_render:
            continue
        objs.append(obj)
    return objs


def bounding_sphere(objs):
    pts = []
    for obj in objs:
        if obj.type in {"MESH", "CURVE", "SURFACE", "FONT", "META", "CURVES", "POINTCLOUD", "VOLUME"}:
            pts.extend(to_gltf(obj.matrix_world @ Vector(c)) for c in obj.bound_box)
    if not pts:
        return Vector((0, 0, 0)), 1.0
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    center = (lo + hi) / 2
    return center, max((hi - lo).length / 2, 1e-3)


def vertical_fov_deg(cam_obj, scene):
    cam = cam_obj.data
    r = scene.render
    aspect = (r.resolution_x * r.pixel_aspect_x) / (r.resolution_y * r.pixel_aspect_y)
    fit = cam.sensor_fit
    if fit == "AUTO":
        sensor = cam.sensor_width
        fit = "HORIZONTAL" if aspect >= 1 else "VERTICAL"
    else:
        sensor = cam.sensor_width if fit == "HORIZONTAL" else cam.sensor_height
    half = math.atan(sensor / 2 / cam.lens)
    if fit == "HORIZONTAL":
        return math.degrees(2 * math.atan(math.tan(half) / aspect))
    return math.degrees(2 * half)


def camera_view(ctx, cam_obj, look_at=None):
    """Convert a Blender camera to a model-viewer orbit around a target.

    model-viewer always orbits a target point, so we pick a target on the
    camera's view axis. Its distance comes from (in order): the linked
    hotspot, the camera's DOF focus object / distance, or the model center.
    Position and view direction are preserved exactly; camera roll and lens
    shift are not supported by model-viewer.
    """
    cam = cam_obj.data
    if cam.type != "PERSP":
        ctx.warn("%s: only perspective cameras are supported" % cam_obj.name)
    if abs(cam.shift_x) > 1e-4 or abs(cam.shift_y) > 1e-4:
        ctx.warn("%s: lens shift is ignored by model-viewer" % cam_obj.name)

    mw = cam_obj.matrix_world
    pos = to_gltf(mw.translation)
    fwd = to_gltf((mw.to_3x3() @ Vector((0, 0, -1))).normalized())

    dist = None
    if look_at is not None:
        dist = (look_at - pos).dot(fwd)
    elif cam.dof.focus_object is not None:
        dist = (to_gltf(cam.dof.focus_object.matrix_world.translation) - pos).dot(fwd)
    elif cam.dof.use_dof:
        dist = cam.dof.focus_distance
    if dist is None or dist <= 1e-3:
        dist = max((ctx.bounds_center - pos).dot(fwd), ctx.bounds_radius * 0.5)

    target = pos + fwd * dist
    off = pos - target
    radius = off.length
    theta = math.degrees(math.atan2(off.x, off.z))
    phi = math.degrees(math.acos(max(-1.0, min(1.0, off.y / radius))))
    return {
        "source": cam_obj.name,
        "target": rounded(target),
        "orbit": [round(theta, 3), round(phi, 3), round(radius, 4)],
        "fov": round(vertical_fov_deg(cam_obj, ctx.scene), 3),
    }


def animation_entry(name, mode="ONCE", reverse=True, delay=0.0, exit_name=None):
    return {
        "name": name,
        "mode": mode.lower(),
        "reverseOnExit": bool(reverse) and mode == "ONCE" and exit_name is None,
        "exit": exit_name,
        "delay": round(delay, 3),
    }


# ---------------------------------------------------------------------------
# Collectors – each fills part of ctx.data
# ---------------------------------------------------------------------------

def collect_model(ctx):
    s = ctx.settings
    ctx.export_objs = export_objects(ctx.scene)
    ctx.bounds_center, ctx.bounds_radius = bounding_sphere(ctx.export_objs)
    ctx.data["model"] = {
        "src": s.model_filename,
        "alt": s.alt_text or ctx.data["title"],
        "center": rounded(ctx.bounds_center),
        "radius": round(ctx.bounds_radius, 4),
    }
    if not ctx.export_objs:
        ctx.warn("No visible objects to export")


def collect_home(ctx):
    cams = find_cameras(ctx.scene)
    actions = find_anim_actions()
    home = {"camera": None, "animation": None}
    if HOME_ID in cams:
        home["camera"] = camera_view(ctx, cams[HOME_ID], ctx.bounds_center)
    else:
        ctx.warn("No %s%s camera – the viewer will use model-viewer's default view" % (CAM_PREFIX, HOME_ID))
    home_anim = ANIM_PREFIX + HOME_ID
    if home_anim in actions:
        home["animation"] = animation_entry(home_anim, mode="LOOP", reverse=False)
    ctx.data["home"] = home


def collect_hotspots(ctx):
    cams = find_cameras(ctx.scene)
    actions = find_anim_actions()
    used_cams = {HOME_ID}
    used_actions = {ANIM_PREFIX + HOME_ID}
    hotspots = []
    ctx.surface_jobs = []  # resolved against the GLB after export

    for hs_id, obj in find_hotspots(ctx.scene):
        p = obj.mv_hotspot
        mw = obj.matrix_world
        pos = to_gltf(mw.translation)
        normal = to_gltf((mw.to_3x3() @ Vector((0, 0, 1))).normalized())

        cam_obj = p.camera or cams.get(hs_id)
        camera = None
        if cam_obj is not None:
            camera = camera_view(ctx, cam_obj, pos)
            used_cams.add(split_name(cam_obj.name, CAM_PREFIX) or cam_obj.name)

        action = p.action or actions.get(ANIM_PREFIX + hs_id)
        animation = None
        if action is not None:
            exit_action = p.exit_action or actions.get(action.name + EXIT_SUFFIX)
            animation = animation_entry(
                action.name, p.play_mode, p.reverse_on_exit, p.delay,
                exit_action.name if exit_action else None)
            used_actions.add(action.name)
            if exit_action:
                used_actions.add(exit_action.name)

        hotspot = {
            "id": hs_id,
            "label": p.label or hs_id.replace("_", " ").capitalize(),
            "description": p.description,
            "position": rounded(pos),
            "normal": rounded(normal),
            "surface": None,  # model-viewer data-surface, set by attach_surfaces()
            "camera": camera,
            "animation": animation,
        }
        hotspots.append(hotspot)
        if p.stick_to_mesh:
            ctx.surface_jobs.append((hotspot, obj, surface_target(ctx, obj), pos))

    for cam_id, cam_obj in cams.items():
        if cam_id not in used_cams:
            ctx.warn("%s is not linked to any hotspot (no %s%s)" % (cam_obj.name, HOTSPOT_PREFIX, cam_id))
    for name in actions:
        if name not in used_actions:
            ctx.warn("Action %s is not linked to any hotspot" % name)

    ctx.data["hotspots"] = hotspots


def surface_target(ctx, hs_obj):
    """Mesh object a sticky hotspot attaches to, or None for the nearest mesh."""
    target = hs_obj.mv_hotspot.surface_object
    if target is None and hs_obj.parent is not None and hs_obj.parent.type == "MESH":
        target = hs_obj.parent
    if target is not None and target not in ctx.export_objs:
        ctx.warn("%s: %s is not exported, using the nearest mesh instead" % (hs_obj.name, target.name))
        target = None
    return target


def collect_limits(ctx):
    """Widen model-viewer's default camera limits so close-ups aren't clamped."""
    views = [h["camera"] for h in ctx.data["hotspots"] if h["camera"]]
    if ctx.data["home"]["camera"]:
        views.append(ctx.data["home"]["camera"])
    r = ctx.bounds_radius
    radii = [v["orbit"][2] for v in views] or [r * 2]
    phis = [v["orbit"][1] for v in views] or [75]
    fovs = [v["fov"] for v in views] or [30]
    ctx.data["limits"] = {
        "minOrbit": ["auto", round(max(0.0, min(22.5, min(phis) - 5)), 2), round(min(radii) * 0.5, 4)],
        "maxOrbit": ["auto", round(min(180.0, max(157.5, max(phis) + 5)), 2), round(max(max(radii) * 1.5, r * 4), 4)],
        "minFov": round(min(12.0, min(fovs) * 0.8), 2),
        "maxFov": round(max(45.0, max(fovs) * 1.2), 2),
    }


def find_world_nodes(world):
    """Return (background_node, env_texture_node, mapping_node) from the world."""
    if world is None or world.node_tree is None or not getattr(world, "use_nodes", True):
        return None, None, None
    nodes = world.node_tree.nodes
    out = next((n for n in nodes if n.type == "OUTPUT_WORLD" and n.is_active_output), None)
    if out is None or not out.inputs["Surface"].is_linked:
        return None, None, None
    bg = out.inputs["Surface"].links[0].from_node
    if bg.type != "BACKGROUND":
        return None, None, None
    env = None
    if bg.inputs["Color"].is_linked:
        n = bg.inputs["Color"].links[0].from_node
        if n.type == "TEX_ENVIRONMENT":
            env = n
    mapping = None
    if env is not None and env.inputs["Vector"].is_linked:
        n = env.inputs["Vector"].links[0].from_node
        if n.type == "MAPPING":
            mapping = n
    return bg, env, mapping


def linear_to_srgb_hex(rgb):
    def conv(c):
        c = max(0.0, min(1.0, c))
        c = c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
        return int(round(c * 255))
    return "#%02x%02x%02x" % tuple(conv(c) for c in rgb[:3])


def collect_environment(ctx):
    s = ctx.settings
    scene = ctx.scene
    view = scene.view_settings
    tone = s.tone_mapping if s.tone_mapping != "AUTO" else TONE_MAPPING.get(view.view_transform, "neutral")
    env = {
        "lighting": "neutral",      # HDR file name or "neutral" (model-viewer studio light)
        "skybox": None,             # HDR file name used as background, if any
        "skyboxVisible": False,     # background shown on load
        "backgroundToggle": False,  # show the background on/off button
        "exposure": 2 ** view.exposure * s.exposure,
        "toneMapping": tone,
        "shadowIntensity": s.shadow_intensity,
        "background": None,         # CSS colour behind the model
    }
    ctx.env_job = None

    lights = [o for o in scene.objects if o.type == "LIGHT" and o.visible_get()]
    if lights:
        ctx.warn("%d light(s) are not exported – model-viewer lights the model with the environment only" % len(lights))

    bg, tex, mapping = find_world_nodes(scene.world)
    strength = 1.0
    if bg is not None:
        strength = bg.inputs["Strength"].default_value
        if not bg.inputs["Color"].is_linked and not scene.render.film_transparent:
            env["background"] = linear_to_srgb_hex(bg.inputs["Color"].default_value)

    if s.export_environment and tex is not None and tex.image is not None:
        rot_z = 0.0
        if mapping is not None:
            rot = mapping.inputs["Rotation"].default_value
            if abs(rot[0]) > 1e-4 or abs(rot[1]) > 1e-4:
                ctx.warn("World mapping: only Z rotation is supported (X/Y ignored)")
            rot_z = rot[2] if mapping.vector_type != "TEXTURE" else -rot[2]
        if s.lighting == "HDR":
            env["lighting"] = s.environment_filename
            env["exposure"] *= strength
        env["skybox"] = s.environment_filename
        env["skyboxVisible"] = s.show_skybox
        env["backgroundToggle"] = s.background_toggle
        ctx.env_job = (tex.image, rot_z)
    elif s.export_environment and bg is not None and tex is None:
        ctx.warn("World has no Environment Texture – using model-viewer's neutral lighting")

    env["exposure"] = round(env["exposure"], 4)
    ctx.data["environment"] = env


def collect_variants(ctx):
    """Material variants -> scene.json, plus the per-mesh mapping for the GLB.

    Each variant stores (object, slot, material) entries.
    """
    s = ctx.settings
    ctx.variant_names = []
    ctx.variant_slots = {}  # (object, slot index) -> {material: {variant index}}
    ctx.data["variants"] = None
    if not s.export_variants or not s.variants:
        return

    exported = set(ctx.export_objs)
    current = None
    for v in s.variants:
        name = v.name.strip()
        if not name or name in ctx.variant_names:
            ctx.warn("Skipping variant %r: names must be unique and not empty" % v.name)
            continue
        vi = len(ctx.variant_names)
        ctx.variant_names.append(name)
        matches_scene = len(v.materials) > 0
        for e in v.materials:
            obj = e.object
            if obj is None or obj.type != "MESH" or obj not in exported:
                continue
            if e.slot >= len(obj.material_slots) or e.material is None:
                ctx.warn("Variant %s: %s slot %d no longer exists or has no material" % (name, obj.name, e.slot + 1))
                continue
            if obj.material_slots[e.slot].material != e.material:
                matches_scene = False
            per_slot = ctx.variant_slots.setdefault((obj, e.slot), {})
            if not any(vi in users for users in per_slot.values()):
                per_slot.setdefault(e.material, set()).add(vi)
        if matches_scene and current is None:
            current = name

    if not ctx.variant_slots:
        ctx.warn("Material variants contain no materials of exported objects")
        ctx.variant_names = []
        return
    ctx.data["variants"] = {
        "names": ctx.variant_names,
        # Variant whose materials match the scene at export time, i.e. what
        # the page shows on load (None: the scene's own materials).
        "current": current,
        "picker": s.variant_picker,
    }


COLLECTORS = [
    collect_model,
    collect_home,
    collect_hotspots,
    collect_limits,
    collect_environment,
    collect_variants,
]


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

@contextmanager
def gltf_variants(ctx):
    """Hand the variants to the glTF exporter for the duration of the export.

    The exporter only writes KHR_materials_variants from its own per-mesh
    data (shown in its optional "glTF Material Variants" panel). That data
    is rebuilt from this add-on's variants on every export.
    """
    if not ctx.variant_slots:
        yield
        return
    addon = bpy.context.preferences.addons.get("io_scene_gltf2")
    if addon is None:
        ctx.warn("glTF add-on not found – material variants are not exported")
        yield
        return
    prefs = addon.preferences
    was_enabled = prefs.KHR_materials_variants_ui
    prefs.KHR_materials_variants_ui = True  # registers the exporter's variant properties
    baked = []
    try:
        if ctx.settings.apply_modifiers:
            baked = bake_modifiers(ctx, {obj for obj, _slot in ctx.variant_slots})

        variants = bpy.data.scenes[0].gltf2_KHR_materials_variants_variants  # the exporter reads scene 0
        variants.clear()
        for i, name in enumerate(ctx.variant_names):
            item = variants.add()
            item.variant_idx = i  # before the name: glTF's name-uniqueness check uses it
            item.name = name
        for mesh in bpy.data.meshes:
            if mesh.library is None:
                mesh.gltf2_variant_mesh_data.clear()
                mesh.gltf2_variant_default_materials.clear()

        # glTF maps variants per mesh and slot: merge objects that share a mesh
        per_mesh = {}
        for (obj, slot), per_slot in ctx.variant_slots.items():
            merged = per_mesh.setdefault((obj.data, slot), {})
            for mat, users in per_slot.items():
                for vi in users:
                    other = next((m for m, u in merged.items() if vi in u), None)
                    if other is None:
                        merged.setdefault(mat, set()).add(vi)
                    elif other != mat:
                        ctx.warn("Variant %s: objects sharing mesh %s use different materials in slot %d; "
                                 "glTF can only store one (make the mesh single-user)"
                                 % (ctx.variant_names[vi], obj.data.name, slot + 1))
        for (mesh, slot), merged in per_mesh.items():
            for mat, users in merged.items():
                item = mesh.gltf2_variant_mesh_data.add()
                item.material_slot_index = slot
                item.material = mat
                for vi in sorted(users):
                    item.variants.add().variant.variant_idx = vi  # the exporter only reads the index
        yield
    finally:
        bpy.data.scenes[0].gltf2_KHR_materials_variants_variants.clear()
        for mesh in bpy.data.meshes:
            if mesh.library is None:
                mesh.gltf2_variant_mesh_data.clear()
        restore_modifiers(baked)
        prefs.KHR_materials_variants_ui = was_enabled


def bake_modifiers(ctx, objs):
    """Temporarily replace the mesh of objs by one with the modifiers applied.

    When applying modifiers, the glTF exporter reads a temporary evaluated
    mesh that doesn't carry its variant data. Baking the modifiers first
    makes it read the (baked) mesh itself. restore_modifiers() undoes this.
    """
    depsgraph = ctx.context.evaluated_depsgraph_get()
    baked = []
    for obj in objs:
        mods = [m for m in obj.modifiers if m.show_viewport]
        if not mods:
            continue
        if any(m.type == "ARMATURE" and m.object is not None for m in mods):  # keep skinning
            ctx.warn("%s: material variants on meshes with an Armature modifier and other "
                     "modifiers are not supported" % obj.name)
            continue
        mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph),
                                               preserve_all_data_layers=True, depsgraph=depsgraph)
        baked.append((obj, obj.data, [(m, m.show_viewport) for m in mods]))
        for m in mods:
            m.show_viewport = False
        obj.data = mesh
    return baked


def restore_modifiers(baked):
    for obj, original, mods in baked:
        mesh = obj.data
        obj.data = original
        for m, show in mods:
            m.show_viewport = show
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def write_glb(ctx):
    s = ctx.settings
    view_layer = ctx.context.view_layer
    prev_selected = [o for o in view_layer.objects if o.select_get()]
    prev_active = view_layer.objects.active
    try:
        for o in prev_selected:
            o.select_set(False)
        for o in ctx.export_objs:
            if o.name in view_layer.objects:
                o.select_set(True)
        with gltf_variants(ctx):
            bpy.ops.export_scene.gltf(
                filepath=str(ctx.out_dir / s.model_filename),
                export_format="GLB",
                use_selection=True,
                export_apply=s.apply_modifiers,
                export_yup=True,
                export_cameras=False,
                export_lights=False,
                export_extras=False,
                export_animations=True,
                export_animation_mode="ACTIONS",
                export_merge_animation="ACTION",
                export_draco_mesh_compression_enable=s.draco,
                export_image_format=s.image_format,
            )
    finally:
        for o in view_layer.objects:
            o.select_set(False)
        for o in prev_selected:
            o.select_set(True)
        view_layer.objects.active = prev_active


def verify_animations(ctx, glb):
    names = [a.get("name", "") for a in glb.json.get("animations", [])]
    ctx.data["animations"] = names
    wanted = []
    if ctx.data["home"]["animation"]:
        wanted.append(ctx.data["home"]["animation"])
    wanted += [h["animation"] for h in ctx.data["hotspots"] if h["animation"]]
    for anim in wanted:
        for key in ("name", "exit"):
            name = anim.get(key)
            if name and name not in names:
                ctx.warn("Action %s is missing from the GLB – assign it to an object "
                         "(active action or NLA strip)" % name)


def verify_variants(ctx, glb):
    if not ctx.variant_names:
        return
    ext = glb.json.get("extensions", {}).get("KHR_materials_variants", {})
    written = [v.get("name") for v in ext.get("variants", [])]
    missing = [n for n in ctx.variant_names if n not in written]
    if missing:
        ctx.warn("Material variant(s) missing from the GLB: %s" % ", ".join(missing))
    ctx.data["variants"]["names"] = [n for n in ctx.variant_names if n in written]
    if not ctx.data["variants"]["names"]:
        ctx.data["variants"] = None


def barycentric(p, a, b, c):
    v0, v1, v2 = b - a, c - a, p - a
    d00, d01, d11 = v0.dot(v0), v0.dot(v1), v1.dot(v1)
    d20, d21 = v2.dot(v0), v2.dot(v1)
    denom = d00 * d11 - d01 * d01
    if abs(denom) < 1e-20:
        return 1.0, 0.0, 0.0
    v = (d11 * d20 - d01 * d21) / denom
    w = (d00 * d21 - d01 * d20) / denom
    bary = [max(0.0, 1.0 - v - w), max(0.0, v), max(0.0, w)]
    total = sum(bary) or 1.0
    return tuple(x / total for x in bary)


def attach_surfaces(ctx, glb):
    """Pin "stick to mesh" hotspots to the nearest triangle of the exported mesh.

    model-viewer's data-surface hotspots reference a glTF mesh, primitive,
    three vertex indices and barycentric weights, and follow the mesh while
    it animates. The indices refer to the GLB's (possibly Draco-decoded)
    vertex order, so this runs on the exported file.
    """
    if not ctx.surface_jobs:
        return
    nodes = glb.json.get("nodes", [])
    meshes = glb.json.get("meshes", [])
    world = glb.node_world_matrices()
    mesh_users = {}
    for ni in world:
        if "mesh" in nodes[ni]:
            mesh_users.setdefault(nodes[ni]["mesh"], []).append(ni)

    trees = {}  # node index -> (BVHTree, [(prim index, local tri, world verts)])

    def node_tree(ni):
        if ni not in trees:
            m = np.array(world[ni])
            verts, tris, info = [], [], []
            for pi, prim in enumerate(meshes[nodes[ni]["mesh"]]["primitives"]):
                geom = glb.primitive_geometry(prim)
                if geom is None:
                    continue
                pos, idx = geom
                wpos = pos @ m[:3, :3].T + m[:3, 3]
                base = len(verts)
                verts.extend(map(Vector, wpos))
                tris.extend((idx + base).tolist())
                info.extend((pi, tri) for tri in idx.tolist())
            tree = BVHTree.FromPolygons(verts, tris, all_triangles=True) if tris else None
            trees[ni] = (tree, verts, tris, info)
        return trees[ni]

    for hotspot, obj, target, pos in ctx.surface_jobs:
        candidates = [ni for ni in world if "mesh" in nodes[ni]
                      and (target is None or nodes[ni].get("name") == target.name)]
        best = None
        try:
            for ni in candidates:
                tree, verts, tris, info = node_tree(ni)
                if tree is None:
                    continue
                loc, _normal, ti, dist = tree.find_nearest(pos)
                if loc is not None and (best is None or dist < best[0]):
                    best = (dist, ni, ti, loc)
        except RuntimeError as e:  # Draco decoding failed
            ctx.warn("%s: can't stick to the mesh (%s)" % (obj.name, e))
            continue
        if best is None:
            ctx.warn("%s: no mesh found to stick to" % obj.name)
            continue

        dist, ni, ti, loc = best
        _tree, verts, tris, info = trees[ni]
        prim_index, (a, b, c) = info[ti]
        u, v, w = barycentric(loc, *(verts[i] for i in tris[ti]))
        mesh_index = nodes[ni]["mesh"]
        hotspot["surface"] = "%d %d %d %d %d %.4f %.4f %.4f" % (mesh_index, prim_index, a, b, c, u, v, w)
        hotspot["position"] = rounded(loc)
        if dist > ctx.bounds_radius * 0.05:
            ctx.warn("%s is %.3g m from the surface of %s; it's moved onto the surface"
                     % (obj.name, dist, nodes[ni].get("name", "the mesh")))
        if len(mesh_users.get(mesh_index, [])) > 1:
            ctx.warn("%s: mesh %s is used by several objects; the hotspot may follow another copy "
                     "(make the mesh single-user)" % (obj.name, nodes[ni].get("name", "")))


def write_environment(ctx):
    if ctx.env_job is None:
        return
    image, rot_z = ctx.env_job
    w, h = image.size
    if w == 0 or h == 0:
        ctx.warn("Environment image %s could not be loaded" % image.name)
        env = ctx.data["environment"]
        env.update(lighting="neutral", skybox=None, skyboxVisible=False, backgroundToggle=False)
        return

    px = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(px)
    px = px.reshape(h, w, 4)
    # model-viewer can't rotate the environment, so bake the world's Z
    # rotation into the image (rotation about the vertical axis = a
    # horizontal shift of an equirectangular map).
    shift = int(round(rot_z / (2 * math.pi) * w)) % w
    if shift:
        px = np.roll(px, shift, axis=1)
    px[..., 3] = 1.0

    tmp = bpy.data.images.new("__mv_env_export", w, h, alpha=False, float_buffer=True)
    try:
        tmp.pixels.foreach_set(px.ravel())
        max_w = ctx.settings.environment_max_width
        if w > max_w:
            tmp.scale(max_w, max(1, round(h * max_w / w)))
        tmp.filepath_raw = str(ctx.out_dir / ctx.settings.environment_filename)
        tmp.file_format = "HDR"
        tmp.save()
    finally:
        bpy.data.images.remove(tmp)


def write_viewer_files(ctx):
    mode = ctx.settings.viewer_files
    if mode == "NEVER":
        return
    for name in VIEWER_FILES:
        dst = ctx.out_dir / name
        if mode == "IF_MISSING" and dst.exists():
            continue
        shutil.copyfile(TEMPLATE_DIR / name, dst)


def write_json(ctx):
    with open(ctx.out_dir / "scene.json", "w", encoding="utf-8") as f:
        json.dump(ctx.data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def run_export(context, settings):
    """Run the full export. Returns the ExportContext (data + warnings)."""
    out_dir = bpy.path.abspath(settings.output_dir)
    if not out_dir:
        raise ValueError("Set an output folder first")
    os.makedirs(out_dir, exist_ok=True)

    scene = context.scene
    prev_frame = scene.frame_current
    scene.frame_set(scene.frame_start)
    try:
        ctx = ExportContext(context, settings, out_dir)
        for collect in COLLECTORS:
            collect(ctx)
        write_glb(ctx)
        glb = GLB(ctx.out_dir / settings.model_filename)
        verify_animations(ctx, glb)
        verify_variants(ctx, glb)
        attach_surfaces(ctx, glb)
        write_environment(ctx)
        write_viewer_files(ctx)
        write_json(ctx)
    finally:
        scene.frame_set(prev_frame)
    return ctx
