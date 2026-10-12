"""Model Viewer Export – Blender add-on.

Exports the scene as a GLB plus a scene.json (cameras, hotspots,
animations, environment) that the bundled web viewer reads.
UI: 3D Viewport > Sidebar (N) > Model Viewer.
"""

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)

from . import core


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def _is_camera(_self, obj):
    return obj.type == "CAMERA"


class MV_HotspotProps(bpy.types.PropertyGroup):
    label: StringProperty(name="Label", description="Text shown on the hotspot (default: derived from the name)")
    description: StringProperty(name="Description", description="Text shown in the info panel when the hotspot is open")
    order: IntProperty(name="Order", description="Sort order of hotspots (then by name)", default=0)
    camera: PointerProperty(name="Camera", type=bpy.types.Object, poll=_is_camera,
                            description="Override: close-up camera (default: CAM_<id>)")
    action: PointerProperty(name="Animation", type=bpy.types.Action,
                            description="Override: action to play (default: ANIM_<id>)")
    exit_action: PointerProperty(name="Exit Animation", type=bpy.types.Action,
                                 description="Override: action to play when leaving (default: ANIM_<id>_exit)")
    play_mode: EnumProperty(name="Play", default="ONCE", items=(
        ("ONCE", "Once", "Play once and hold the last frame"),
        ("LOOP", "Loop", "Loop while the hotspot is open"),
        ("PINGPONG", "Ping-pong", "Play back and forth while the hotspot is open"),
    ))
    reverse_on_exit: BoolProperty(name="Reverse on Exit", default=True,
                                  description="Play the animation backwards when leaving (ignored if an exit animation exists)")
    delay: FloatProperty(name="Delay", default=0.0, min=0.0, soft_max=3.0, unit="TIME_ABSOLUTE",
                         description="Seconds to wait for the camera move before the animation starts")


class MV_SceneSettings(bpy.types.PropertyGroup):
    output_dir: StringProperty(name="Output Folder", subtype="DIR_PATH", default="//docs/",
                               options={"PATH_SUPPORTS_BLEND_RELATIVE"},
                               description="Folder for model.glb, scene.json, the HDR and the viewer files")
    title: StringProperty(name="Title", description="Page title (default: .blend file name)")
    alt_text: StringProperty(name="Alt Text", description="Accessible description of the model")
    model_filename: StringProperty(name="Model File", default="model.glb")

    draco: BoolProperty(name="Draco Compression", default=False,
                        description="Compress meshes (smaller files, decoded in the browser)")
    image_format: EnumProperty(name="Textures", default="AUTO", items=(
        ("AUTO", "Automatic", "PNG or JPEG depending on the texture"),
        ("JPEG", "JPEG", "Smaller, no alpha"),
        ("WEBP", "WebP", "Smallest, supported by all modern browsers"),
    ))
    apply_modifiers: BoolProperty(name="Apply Modifiers", default=True)

    export_environment: BoolProperty(name="Export Environment HDR", default=True,
                                     description="Use the world's Environment Texture for lighting (otherwise neutral studio lighting)")
    show_skybox: BoolProperty(name="Show as Background", default=True,
                              description="Show the HDR behind the model (skybox)")
    background_toggle: BoolProperty(name="Background Toggle Button", default=False,
                                    description="Add a button to the page to show/hide the HDR background")
    environment_filename: StringProperty(name="HDR File", default="environment.hdr")
    environment_max_width: IntProperty(name="Max HDR Width", default=2048, min=256, max=16384,
                                       description="Downscale larger HDRs to keep the page fast")
    shadow_intensity: FloatProperty(name="Shadow Intensity", default=1.0, min=0.0, max=2.0)

    viewer_files: EnumProperty(name="Viewer Files", default="IF_MISSING", items=(
        ("IF_MISSING", "Copy if Missing", "Copy index.html/viewer.js/viewer.css only if they don't exist (keeps your edits)"),
        ("ALWAYS", "Always Overwrite", "Replace the viewer files with the add-on's templates"),
        ("NEVER", "Never", "Only write model.glb, scene.json and the HDR"),
    ))


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class MV_OT_export(bpy.types.Operator):
    bl_idname = "model_viewer.export"
    bl_label = "Export for Model Viewer"
    bl_description = "Export GLB, scene.json, environment HDR and viewer files"

    def execute(self, context):
        try:
            ctx = core.run_export(context, context.scene.mv_settings)
        except Exception as e:  # report instead of a traceback popup
            self.report({"ERROR"}, str(e))
            raise
        for w in ctx.warnings:
            self.report({"WARNING"}, w)
        d = ctx.data
        self.report({"INFO"}, "Exported %d hotspot(s), %d animation(s) to %s" % (
            len(d["hotspots"]), len(d.get("animations", [])), ctx.out_dir))
        return {"FINISHED"}


class MV_OT_add_hotspot(bpy.types.Operator):
    bl_idname = "model_viewer.add_hotspot"
    bl_label = "Add Hotspot"
    bl_description = "Add an HS_ empty at the 3D cursor (its +Z axis is the hotspot normal)"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="ID", default="hotspot")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        empty = bpy.data.objects.new(core.HOTSPOT_PREFIX + self.name, None)
        empty.empty_display_type = "SINGLE_ARROW"
        empty.empty_display_size = 0.2
        empty.matrix_world = context.scene.cursor.matrix
        context.collection.objects.link(empty)
        for o in context.selected_objects:
            o.select_set(False)
        empty.select_set(True)
        context.view_layer.objects.active = empty
        return {"FINISHED"}


class MV_OT_camera_from_view(bpy.types.Operator):
    bl_idname = "model_viewer.camera_from_view"
    bl_label = "Camera from View"
    bl_description = ("Create or update the camera for the active hotspot (CAM_<id>) or the home "
                      "camera (CAM_home) from the current viewport view")
    bl_options = {"REGISTER", "UNDO"}

    home: BoolProperty(name="Home Camera", default=False)

    @classmethod
    def poll(cls, context):
        return context.space_data and context.space_data.type == "VIEW_3D"

    def execute(self, context):
        if self.home:
            cam_id = core.HOME_ID
        else:
            obj = context.active_object
            cam_id = obj and core.split_name(obj.name, core.HOTSPOT_PREFIX)
            if not cam_id:
                self.report({"ERROR"}, "Select an HS_ hotspot first")
                return {"CANCELLED"}
        name = core.CAM_PREFIX + cam_id
        cam = bpy.data.objects.get(name)
        if cam is None or cam.type != "CAMERA":
            cam = bpy.data.objects.new(name, bpy.data.cameras.new(name))
            context.scene.collection.objects.link(cam)
        r3d = context.space_data.region_3d
        # Position/rotation only; the camera keeps its own focal length
        # (check the framing through the camera with Numpad 0).
        cam.matrix_world = r3d.view_matrix.inverted()
        self.report({"INFO"}, "Updated %s" % name)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class MV_PT_main(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Model Viewer"
    bl_label = "Model Viewer Export"

    def draw(self, context):
        s = context.scene.mv_settings
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(s, "output_dir")
        layout.prop(s, "title")
        layout.prop(s, "alt_text")
        layout.separator()
        layout.operator(MV_OT_export.bl_idname, icon="EXPORT")


class MV_PT_scene(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Model Viewer"
    bl_label = "Scene Overview"
    bl_parent_id = "MV_PT_main"

    def draw(self, context):
        scene = context.scene
        layout = self.layout
        cams = core.find_cameras(scene)
        actions = core.find_anim_actions()

        row = layout.row()
        row.label(text="Home", icon="HOME")
        row.label(text="", icon="CAMERA_DATA" if core.HOME_ID in cams else "BLANK1")
        row.label(text="", icon="ACTION" if core.ANIM_PREFIX + core.HOME_ID in actions else "BLANK1")

        spots = core.find_hotspots(scene)
        if not spots:
            layout.label(text="No HS_ empties found", icon="INFO")
        for hs_id, obj in spots:
            p = obj.mv_hotspot
            cam = p.camera or cams.get(hs_id)
            act = p.action or actions.get(core.ANIM_PREFIX + hs_id)
            row = layout.row()
            row.operator("object.select_pattern", text=hs_id, icon="EMPTY_SINGLE_ARROW",
                         emboss=False).pattern = obj.name
            row.label(text="", icon="CAMERA_DATA" if cam else "BLANK1")
            row.label(text="", icon="ACTION" if act else "BLANK1")

        col = layout.column(align=True)
        col.operator(MV_OT_add_hotspot.bl_idname, icon="ADD")
        col.operator(MV_OT_camera_from_view.bl_idname, text="Hotspot Camera from View",
                     icon="VIEW_CAMERA").home = False
        col.operator(MV_OT_camera_from_view.bl_idname, text="Home Camera from View",
                     icon="HOME").home = True


class MV_PT_hotspot(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Model Viewer"
    bl_label = "Active Hotspot"
    bl_parent_id = "MV_PT_main"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == "EMPTY" and obj.name.startswith(core.HOTSPOT_PREFIX)

    def draw(self, context):
        p = context.active_object.mv_hotspot
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        for prop in ("label", "description", "order", "camera", "action", "exit_action",
                     "play_mode", "reverse_on_exit", "delay"):
            layout.prop(p, prop)


class MV_PT_export_settings(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Model Viewer"
    bl_label = "Model & Files"
    bl_parent_id = "MV_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = context.scene.mv_settings
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        for prop in ("model_filename", "draco", "image_format", "apply_modifiers", "viewer_files"):
            layout.prop(s, prop)


class MV_PT_environment(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Model Viewer"
    bl_label = "Environment"
    bl_parent_id = "MV_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = context.scene.mv_settings
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(s, "export_environment")
        col = layout.column()
        col.active = s.export_environment
        for prop in ("show_skybox", "background_toggle", "environment_filename", "environment_max_width"):
            col.prop(s, prop)
        layout.prop(s, "shadow_intensity")


CLASSES = (
    MV_HotspotProps,
    MV_SceneSettings,
    MV_OT_export,
    MV_OT_add_hotspot,
    MV_OT_camera_from_view,
    MV_PT_main,
    MV_PT_scene,
    MV_PT_hotspot,
    MV_PT_export_settings,
    MV_PT_environment,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.mv_hotspot = PointerProperty(type=MV_HotspotProps)
    bpy.types.Scene.mv_settings = PointerProperty(type=MV_SceneSettings)


def unregister():
    del bpy.types.Scene.mv_settings
    del bpy.types.Object.mv_hotspot
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
