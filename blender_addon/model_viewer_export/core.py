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

The export is split into small "collector" functions that each fill one
part of the scene.json dict. To add a feature, write a collector and add it
to COLLECTORS.
"""

import json
import math
import os
import shutil
import struct
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

SCHEMA_VERSION = 1

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

        hotspots.append({
            "id": hs_id,
            "label": p.label or hs_id.replace("_", " ").capitalize(),
            "description": p.description,
            "position": rounded(pos),
            "normal": rounded(normal),
            "camera": camera,
            "animation": animation,
        })

    for cam_id, cam_obj in cams.items():
        if cam_id not in used_cams:
            ctx.warn("%s is not linked to any hotspot (no %s%s)" % (cam_obj.name, HOTSPOT_PREFIX, cam_id))
    for name in actions:
        if name not in used_actions:
            ctx.warn("Action %s is not linked to any hotspot" % name)

    ctx.data["hotspots"] = hotspots


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
    env = {
        "image": "neutral",
        "skybox": False,
        "exposure": round(2 ** view.exposure, 4),
        "toneMapping": TONE_MAPPING.get(view.view_transform, "neutral"),
        "shadowIntensity": s.shadow_intensity,
        "background": None,
        "backgroundToggle": s.background_toggle,
    }
    ctx.env_job = None

    lights = [o for o in scene.objects if o.type == "LIGHT" and o.visible_get()]
    if lights:
        ctx.warn("%d light(s) are not exported – model-viewer lights the model with the environment only" % len(lights))

    bg, tex, mapping = find_world_nodes(scene.world)
    if bg is not None:
        strength = bg.inputs["Strength"].default_value
        if not bg.inputs["Color"].is_linked:
            env["background"] = linear_to_srgb_hex(bg.inputs["Color"].default_value)

    if s.export_environment and tex is not None and tex.image is not None:
        rot_z = 0.0
        if mapping is not None:
            rot = mapping.inputs["Rotation"].default_value
            if abs(rot[0]) > 1e-4 or abs(rot[1]) > 1e-4:
                ctx.warn("World mapping: only Z rotation is supported (X/Y ignored)")
            rot_z = rot[2] if mapping.vector_type != "TEXTURE" else -rot[2]
        env["image"] = s.environment_filename
        env["skybox"] = s.show_skybox and not scene.render.film_transparent
        env["exposure"] = round(env["exposure"] * strength, 4)
        ctx.env_job = (tex.image, rot_z)
    elif s.export_environment and bg is not None and tex is None:
        ctx.warn("World has no Environment Texture – using model-viewer's neutral lighting")

    if scene.render.film_transparent:
        env["background"] = None
    ctx.data["environment"] = env


COLLECTORS = [
    collect_model,
    collect_home,
    collect_hotspots,
    collect_limits,
    collect_environment,
]


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

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


def read_glb_json(path):
    with open(path, "rb") as f:
        magic, _version, _length = struct.unpack("<4sII", f.read(12))
        if magic != b"glTF":
            raise ValueError("Not a GLB file: %s" % path)
        chunk_len, chunk_type = struct.unpack("<II", f.read(8))
        if chunk_type != 0x4E4F534A:  # 'JSON'
            raise ValueError("First GLB chunk is not JSON")
        return json.loads(f.read(chunk_len))


def verify_animations(ctx):
    gltf = read_glb_json(ctx.out_dir / ctx.settings.model_filename)
    names = [a.get("name", "") for a in gltf.get("animations", [])]
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


def write_environment(ctx):
    if ctx.env_job is None:
        return
    image, rot_z = ctx.env_job
    w, h = image.size
    if w == 0 or h == 0:
        ctx.warn("Environment image %s could not be loaded" % image.name)
        ctx.data["environment"]["image"] = "neutral"
        ctx.data["environment"]["skybox"] = False
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
        verify_animations(ctx)
        write_environment(ctx)
        write_viewer_files(ctx)
        write_json(ctx)
    finally:
        scene.frame_set(prev_frame)
    return ctx
