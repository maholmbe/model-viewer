"""Build a demo scene that follows the naming conventions and export it.

    blender -b --factory-startup --python tools/build_demo.py -- [out_dir]

Writes demo/demo_cabinet.blend and exports to docs/ (or out_dir).
"""

import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "blender_addon"))
import model_viewer_export  # noqa: E402
from model_viewer_export import core  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
SITE = Path(argv[0]).resolve() if argv else ROOT / "docs"


def material(name, color, metallic=0.0, roughness=0.5):
    mat = bpy.data.materials.new(name)
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*color, 1)
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    return mat


def box(name, size, location, mat, parent=None):
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(mat)
    if parent:
        obj.parent = parent
        obj.matrix_parent_inverse = parent.matrix_world.inverted()
    return obj


def empty(name, location, rotation=(0, 0, 0), kind="PLAIN_AXES", size=0.2):
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = kind
    obj.empty_display_size = size
    obj.location = location
    obj.rotation_euler = rotation
    bpy.context.collection.objects.link(obj)
    return obj


def camera(name, location, look_at, lens=50):
    cam = bpy.data.objects.new(name, bpy.data.cameras.new(name))
    cam.data.lens = lens
    cam.location = location
    cam.rotation_euler = (Vector(look_at) - Vector(location)).to_track_quat("-Z", "Y").to_euler()
    bpy.context.collection.objects.link(cam)
    return cam


def keys(obj, path, frames):
    for frame, value in frames:
        setattr(obj, path, value)
        obj.keyframe_insert(path, frame=frame)


def build():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    model_viewer_export.register()
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 48

    wood = material("Wood", (0.42, 0.25, 0.12), roughness=0.6)
    door_mat = material("Door", (0.08, 0.25, 0.55), roughness=0.35)
    metal = material("Metal", (0.9, 0.9, 0.92), metallic=1.0, roughness=0.25)
    accent = material("Accent", (0.9, 0.35, 0.1), roughness=0.4)

    box("Body", (1.0, 0.6, 1.0), (0, 0, 0.5), wood)
    box("Floor", (3, 3, 0.02), (0, 0, -0.01), material("Floor", (0.8, 0.8, 0.8), roughness=0.9))

    # Door hinged on the left front edge
    pivot = empty("DoorPivot", (-0.45, -0.315, 0.5))
    door = box("Door", (0.9, 0.03, 0.9), (0, -0.315, 0.5), door_mat, parent=pivot)
    box("DoorKnob", (0.05, 0.05, 0.12), (0.35, -0.345, 0.5), metal, parent=door)

    # Lid + gadget share one action (two slots) -> one glTF animation
    lid = box("Lid", (1.0, 0.6, 0.04), (0, 0, 1.02), wood)
    gadget = box("Gadget", (0.25, 0.25, 0.25), (0, 0, 0.6), accent)

    spinner = box("Spinner", (0.15, 0.15, 0.3), (0.8, -0.3, 0.15), accent)

    # --- Actions -----------------------------------------------------------
    keys(pivot, "rotation_euler", [(1, (0, 0, 0)), (30, (0, 0, math.radians(-110)))])
    pivot.animation_data.action.name = "ANIM_door"

    keys(lid, "location", [(1, (0, 0, 1.02)), (24, (0, 0, 1.45))])
    act = lid.animation_data.action
    act.name = "ANIM_lid"
    gadget.animation_data_create()
    gadget.animation_data.action = act
    keys(gadget, "location", [(1, (0, 0, 0.6)), (12, (0, 0, 0.6)), (36, (0, 0, 1.15))])

    prefs = bpy.context.preferences.edit
    prefs.keyframe_new_interpolation_type = "LINEAR"
    keys(spinner, "rotation_euler", [(1, (0, 0, 0)), (48, (0, 0, math.radians(360)))])
    spinner.animation_data.action.name = "ANIM_home"
    prefs.keyframe_new_interpolation_type = "BEZIER"

    # --- Hotspots (+Z of the empty = normal) -------------------------------
    hs_door = empty("HS_door", (0.2, -0.335, 0.75), (math.radians(90), 0, 0), "SINGLE_ARROW")
    hs_door.mv_hotspot.label = "Door"
    hs_door.mv_hotspot.description = "The door swings open on its left hinge."
    hs_door.mv_hotspot.delay = 0.4

    hs_lid = empty("HS_lid", (0.3, 0.1, 1.04), (0, 0, 0), "SINGLE_ARROW")
    hs_lid.mv_hotspot.label = "Lid"
    hs_lid.mv_hotspot.description = "The lid lifts and the gadget rises out of the box."
    hs_lid.mv_hotspot.order = 1

    hs_spin = empty("HS_spinner_info", (0.8, -0.38, 0.3), (math.radians(90), 0, 0), "SINGLE_ARROW")
    hs_spin.mv_hotspot.label = "Spinner"
    hs_spin.mv_hotspot.description = "Info-only hotspot: no camera, no animation."
    hs_spin.mv_hotspot.order = 2

    # --- Cameras -------------------------------------------------------------
    camera("CAM_home", (2.6, -3.4, 2.0), (0, 0, 0.55), lens=50)
    camera("CAM_door", (1.1, -1.9, 1.1), (0.0, -0.3, 0.6), lens=40)
    camera("CAM_lid", (0.9, -1.4, 2.2), (0, 0, 1.15), lens=45)

    # --- World -------------------------------------------------------------
    world = bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    bg = nt.nodes["Background"]
    tex = nt.nodes.new("ShaderNodeTexEnvironment")
    hdr = Path(bpy.utils.system_resource("DATAFILES", path="studiolights/world")) / "forest.exr"
    tex.image = bpy.data.images.load(str(hdr))
    mapping = nt.nodes.new("ShaderNodeMapping")
    coord = nt.nodes.new("ShaderNodeTexCoord")
    mapping.inputs["Rotation"].default_value[2] = math.radians(45)
    nt.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
    nt.links.new(tex.outputs["Color"], bg.inputs["Color"])
    bg.inputs["Strength"].default_value = 1.0

    s = scene.mv_settings
    s.title = "Demo Cabinet"
    s.alt_text = "A wooden cabinet with a blue door and a lifting lid"
    s.background_toggle = True
    s.viewer_files = "ALWAYS"

    blend = ROOT / "demo" / "demo_cabinet.blend"
    blend.parent.mkdir(exist_ok=True)
    s.output_dir = "//../docs/" if not argv else str(SITE) + "/"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    return scene


build()
ctx = core.run_export(bpy.context, bpy.context.scene.mv_settings)
print("\n=== Export finished:", ctx.out_dir)
print("animations in GLB:", ctx.data["animations"])
for h in ctx.data["hotspots"]:
    print("hotspot", h["id"], "camera:", bool(h["camera"]), "anim:", h["animation"] and h["animation"]["name"])
for w in ctx.warnings:
    print("WARNING:", w)
