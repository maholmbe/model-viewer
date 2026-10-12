"""Model Viewer Export – Blender add-on.

Exports the scene as a GLB plus a scene.json (cameras, hotspots,
animations, environment) that the bundled web viewer reads.
UI: 3D Viewport > Sidebar (N) > Model Viewer.
"""

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       IntProperty, PointerProperty, StringProperty)

from . import core


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def _is_camera(_self, obj):
    return obj.type == "CAMERA"


def _is_mesh(_self, obj):
    return obj.type == "MESH"


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
    stick_to_mesh: BoolProperty(name="Stick to Mesh", default=False,
                                description="Pin the hotspot to the nearest point on the mesh surface so it "
                                            "moves along when the mesh is animated")
    surface_object: PointerProperty(name="Mesh", type=bpy.types.Object, poll=_is_mesh,
                                    description="Mesh to stick to (default: the parent mesh, "
                                                "otherwise the nearest mesh)")


class MV_VariantMaterial(bpy.types.PropertyGroup):
    object: PointerProperty(name="Object", type=bpy.types.Object)
    slot: IntProperty(name="Slot", min=0)
    material: PointerProperty(name="Material", type=bpy.types.Material)


class MV_Variant(bpy.types.PropertyGroup):
    # "name" (from PropertyGroup) is the variant name shown on the page
    materials: CollectionProperty(type=MV_VariantMaterial)


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
                                     description="Export the world's Environment Texture (for lighting and/or background)")
    lighting: EnumProperty(name="Lighting", default="HDR", items=(
        ("HDR", "HDR", "Light the model with the exported HDR, like Blender does"),
        ("NEUTRAL", "Neutral Studio", "Light the model with model-viewer's bright neutral studio light; "
                                      "the HDR is only used as background"),
    ))
    show_skybox: BoolProperty(name="Show as Background", default=True,
                              description="Show the HDR behind the model (skybox) when the page loads")
    background_toggle: BoolProperty(name="Background Toggle Button", default=False,
                                    description="Add a button to the page to show/hide the HDR background")
    environment_filename: StringProperty(name="HDR File", default="environment.hdr")
    environment_max_width: IntProperty(name="Max HDR Width", default=2048, min=256, max=16384,
                                       description="Downscale larger HDRs to keep the page fast")
    exposure: FloatProperty(name="Exposure", default=1.0, min=0.0, soft_min=0.1, soft_max=4.0,
                            description="Brightness multiplier on top of Blender's film exposure and world strength")
    tone_mapping: EnumProperty(name="Tone Mapping", default="AUTO", items=(
        ("AUTO", "From Blender", "AgX → agx, Filmic → aces, Khronos PBR Neutral → neutral, Standard → none"),
        ("neutral", "Neutral", "model-viewer default, keeps product colours accurate"),
        ("agx", "AgX", "Soft highlight roll-off, like Blender's AgX"),
        ("aces", "ACES", "Filmic, more contrast"),
        ("none", "None", "No tone mapping, bright areas clip (like Blender's Standard)"),
    ))
    shadow_intensity: FloatProperty(name="Shadow Intensity", default=1.0, min=0.0, max=2.0)

    export_variants: BoolProperty(name="Export Variants", default=True,
                                  description="Export the material variants (KHR_materials_variants)")
    variant_picker: BoolProperty(name="Variant Picker", default=True,
                                 description="Show buttons on the page to switch between variants")
    variants: CollectionProperty(type=MV_Variant)
    active_variant: IntProperty(name="Active Variant", default=0)

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


def _selected_meshes(context):
    return [o for o in context.selected_objects if o.type == "MESH" and len(o.material_slots)]


def store_materials(variant, objs):
    """Replace the variant's entries for objs with their current materials."""
    names = {o.name for o in objs}
    for i in reversed(range(len(variant.materials))):
        e = variant.materials[i]
        if e.object is None or e.object.name in names:
            variant.materials.remove(i)
    count = 0
    for obj in objs:
        for slot_index, slot in enumerate(obj.material_slots):
            if slot.material is not None:
                e = variant.materials.add()
                e.object, e.slot, e.material = obj, slot_index, slot.material
                count += 1
    return count


class _VariantOperator:
    bl_options = {"REGISTER", "UNDO"}

    @staticmethod
    def active(context):
        s = context.scene.mv_settings
        return s.variants[s.active_variant] if 0 <= s.active_variant < len(s.variants) else None

    @classmethod
    def poll(cls, context):
        return cls.active(context) is not None


class MV_OT_variant_add(bpy.types.Operator):
    bl_idname = "model_viewer.variant_add"
    bl_label = "Add Variant"
    bl_description = "Add a material variant from the current materials of the selected objects"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.mv_settings
        names = {v.name for v in s.variants}
        n = len(s.variants) + 1
        while "Variant %d" % n in names:
            n += 1
        v = s.variants.add()
        v.name = "Variant %d" % n
        s.active_variant = len(s.variants) - 1
        store_materials(v, _selected_meshes(context))
        return {"FINISHED"}


class MV_OT_variant_remove(_VariantOperator, bpy.types.Operator):
    bl_idname = "model_viewer.variant_remove"
    bl_label = "Remove Variant"
    bl_description = "Remove the active material variant (materials are kept)"

    def execute(self, context):
        s = context.scene.mv_settings
        s.variants.remove(s.active_variant)
        s.active_variant = max(0, min(s.active_variant, len(s.variants) - 1))
        return {"FINISHED"}


class MV_OT_variant_move(_VariantOperator, bpy.types.Operator):
    bl_idname = "model_viewer.variant_move"
    bl_label = "Move Variant"
    bl_description = "Change the order of the variants (the order of the buttons on the page)"

    direction: EnumProperty(items=(("UP", "Up", ""), ("DOWN", "Down", "")))

    def execute(self, context):
        s = context.scene.mv_settings
        new = s.active_variant + (-1 if self.direction == "UP" else 1)
        if 0 <= new < len(s.variants):
            s.variants.move(s.active_variant, new)
            s.active_variant = new
        return {"FINISHED"}


class MV_OT_variant_store(_VariantOperator, bpy.types.Operator):
    bl_idname = "model_viewer.variant_store"
    bl_label = "Store Selected"
    bl_description = "Store the current materials of the selected objects in the active variant"

    def execute(self, context):
        objs = _selected_meshes(context)
        if not objs:
            self.report({"ERROR"}, "Select the mesh objects whose materials belong to this variant")
            return {"CANCELLED"}
        v = self.active(context)
        count = store_materials(v, objs)
        self.report({"INFO"}, "%s: stored %d material slot(s)" % (v.name, count))
        return {"FINISHED"}


class MV_OT_variant_apply(_VariantOperator, bpy.types.Operator):
    bl_idname = "model_viewer.variant_apply"
    bl_label = "Apply"
    bl_description = "Assign the active variant's materials in Blender, to preview or edit it"

    def execute(self, context):
        for e in self.active(context).materials:
            if e.object is not None and e.slot < len(e.object.material_slots):
                e.object.material_slots[e.slot].material = e.material
        return {"FINISHED"}


class MV_OT_variant_remove_entry(_VariantOperator, bpy.types.Operator):
    bl_idname = "model_viewer.variant_remove_entry"
    bl_label = "Remove Material"
    bl_description = "Remove this material slot from the variant (it then keeps its default material)"

    index: IntProperty()

    def execute(self, context):
        v = self.active(context)
        if 0 <= self.index < len(v.materials):
            v.materials.remove(self.index)
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
                     "play_mode", "reverse_on_exit", "delay", "stick_to_mesh"):
            layout.prop(p, prop)
        row = layout.row()
        row.active = p.stick_to_mesh
        row.prop(p, "surface_object")


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
        for prop in ("lighting", "show_skybox", "background_toggle", "environment_filename", "environment_max_width"):
            col.prop(s, prop)
        for prop in ("exposure", "tone_mapping", "shadow_intensity"):
            layout.prop(s, prop)


class MV_UL_variants(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index=0):
        layout.prop(item, "name", text="", emboss=False, icon="MATERIAL")
        layout.label(text=str(len(item.materials)))


class MV_PT_variants(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Model Viewer"
    bl_label = "Material Variants"
    bl_parent_id = "MV_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.prop(context.scene.mv_settings, "export_variants", text="")

    def draw(self, context):
        s = context.scene.mv_settings
        layout = self.layout
        layout.active = s.export_variants
        layout.prop(s, "variant_picker")

        row = layout.row()
        row.template_list("MV_UL_variants", "", s, "variants", s, "active_variant", rows=3)
        col = row.column(align=True)
        col.operator(MV_OT_variant_add.bl_idname, text="", icon="ADD")
        col.operator(MV_OT_variant_remove.bl_idname, text="", icon="REMOVE")
        col.separator()
        col.operator(MV_OT_variant_move.bl_idname, text="", icon="TRIA_UP").direction = "UP"
        col.operator(MV_OT_variant_move.bl_idname, text="", icon="TRIA_DOWN").direction = "DOWN"

        if not (0 <= s.active_variant < len(s.variants)):
            layout.label(text="Select objects, set their materials, then +", icon="INFO")
            return
        v = s.variants[s.active_variant]
        row = layout.row(align=True)
        row.operator(MV_OT_variant_store.bl_idname, icon="IMPORT")
        row.operator(MV_OT_variant_apply.bl_idname, icon="HIDE_OFF")

        box = layout.box()
        if not v.materials:
            box.label(text="No materials stored", icon="INFO")
        for i, e in enumerate(v.materials):
            row = box.row(align=True)
            obj_name = e.object.name if e.object else "(deleted)"
            row.label(text="%s · %d" % (obj_name, e.slot + 1), icon="OBJECT_DATA")
            row.prop(e, "material", text="")
            row.operator(MV_OT_variant_remove_entry.bl_idname, text="", icon="X", emboss=False).index = i


CLASSES = (
    MV_HotspotProps,
    MV_VariantMaterial,
    MV_Variant,
    MV_SceneSettings,
    MV_OT_export,
    MV_OT_add_hotspot,
    MV_OT_camera_from_view,
    MV_OT_variant_add,
    MV_OT_variant_remove,
    MV_OT_variant_move,
    MV_OT_variant_store,
    MV_OT_variant_apply,
    MV_OT_variant_remove_entry,
    MV_UL_variants,
    MV_PT_main,
    MV_PT_scene,
    MV_PT_hotspot,
    MV_PT_variants,
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
