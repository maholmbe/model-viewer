# Blender → model-viewer pipeline

A Blender add-on that exports a scene to a static web page built on
[Google model-viewer](https://modelviewer.dev): a rotatable model, hotspots
that open close-up camera views, and animations that play when a hotspot is
clicked.

```
blender_addon/model_viewer_export/   the add-on (Blender 4.4+, tested on 5.2.2)
  core.py                            scene collection + export logic
  glb.py                             reads the exported GLB back (incl. Draco)
  __init__.py                        properties, operators, sidebar UI
  templates/                         index.html, viewer.js, viewer.css
tools/build_demo.py                  builds + exports a demo scene
demo/demo_cabinet.blend              demo scene (generated)
docs/                                export output = the website, served by GitHub Pages
dist/                                installable add-on zip (generated)
```

## Install

Build the zip, then in Blender go to **Edit → Preferences → Get Extensions → ⌄ → Install from Disk** and pick it:

```sh
blender --command extension build --source-dir blender_addon/model_viewer_export --output-dir dist
```

The panel appears under **3D Viewport → Sidebar (N) → Model Viewer**.

## Naming conventions

| Blender object | Name | Meaning |
|---|---|---|
| Camera | `CAM_home` | Start / overview view. The **Back** button returns here. |
| Camera | `CAM_<id>` | Close-up view for hotspot `<id>` |
| Empty | `HS_<id>` | Hotspot. Its position is the hotspot location and its **local +Z** is the surface normal (use the *Single Arrow* display type) |
| Action | `ANIM_<id>` | Plays when hotspot `<id>` is clicked |
| Action | `ANIM_<id>_exit` | *(optional)* Plays when leaving the hotspot. Without it, `ANIM_<id>` plays in reverse. |
| Action | `ANIM_home` | *(optional)* Loops while in the overview |

Example: `HS_door` + `CAM_door` + `ANIM_door` gives a "Door" hotspot that
flies to the door close-up and opens the door.

Anything that isn't `CAM_*` / `HS_*`, a camera or a light, and is visible
and renderable, goes into the GLB.

### Hotspot visibility

Hotspots are fully opaque. They're hidden when the mesh is between the camera
and the hotspot, which is checked with a raycast while the camera moves or an
animation plays. Place the `HS_` empty on or slightly above the surface. If it
sits inside the mesh, it counts as hidden.

### Hotspot settings

Select an `HS_` empty to see the **Active Hotspot** panel:

- **Label / Description**: hotspot label and the text in the info panel
- **Order**: sort order
- **Camera / Animation / Exit Animation**: explicit links that override the naming convention, for example to reuse one camera for two hotspots
- **Play**: Once (holds the last frame), Loop, or Ping-pong
- **Reverse on Exit**: play backwards when leaving (Once only)
- **Delay**: seconds to wait for the camera move before the animation starts
- **Stick to Mesh / Mesh**: pin the hotspot to the mesh surface so it moves with the animation (see below)

### Hotspots that follow the animation

Turn on **Stick to Mesh** for a hotspot on a moving part, such as the crank
of a drill or a door. The export finds the nearest point on the mesh and
writes it as a model-viewer
[surface hotspot](https://modelviewer.dev/examples/annotations/#animatedHotspots).
The hotspot is then pinned to that triangle and follows it when the part
moves, rotates or deforms (skinned meshes work too).

- It sticks to the **Mesh** set in the panel. Otherwise it uses the empty's parent, if that's a mesh, or else the nearest exported mesh. Parenting the `HS_` empty to the part it belongs to is the easiest way to set it up.
- The hotspot is moved onto the surface. The export warns if the empty is far from the mesh.
- Works with and without Draco compression. The position is computed from the exported GLB.
- If several objects share the mesh (linked duplicates), the hotspot can end up on another copy. Make the mesh single-user to avoid that.

### Animations

- Each action has to be **assigned to the object it animates**, either as the active action or as a strip on an NLA track. Actions that aren't assigned don't get exported. The add-on reads the GLB back after export and warns about any that are missing.
- To animate **several objects** from one hotspot, assign the same action to all of them. Blender 4.4+ gives each object its own *slot* and the exporter merges them into one glTF animation. In the demo, `ANIM_lid` moves both `Lid` and `Gadget`.
- The model shows the pose at the **scene start frame** when no animation is playing. The export always runs at `frame_start`.
- model-viewer plays one animation at a time. Opening hotspot B while A is open cross-fades A back to its rest pose.

### Cameras

- Position, direction and focal length are exported exactly. model-viewer always orbits around a target point. The add-on puts that target at the point on the camera's view axis nearest the hotspot, so the user can rotate around the detail after the close-up.
- Camera roll and lens shift aren't supported by model-viewer.
- **Hotspot Camera from View** in the panel creates or updates `CAM_<id>` for the selected hotspot from the current viewport position. **Home Camera from View** does the same for `CAM_home`.
- Camera orbit and zoom limits are widened automatically so close-ups aren't clamped.

### Material variants

Variants are alternative sets of materials, such as colours or finishes, that
the visitor can switch between on the page. They're exported as
`KHR_materials_variants`, and the page shows a button for each variant
(bottom right).

Panel **Model Viewer → Material Variants**:

1. Assign the materials for the first variant in Blender, select the objects that change, and click **+**. This creates a variant from the selected objects' current materials. Double-click to rename it; the name is shown on the button.
2. Assign the materials for the next variant and click **+** again. To change an existing variant, select it, set the materials, select the objects and click **Store Selected**.
3. **Apply** assigns a variant's materials in Blender, to check or edit it. The list under the buttons shows what's stored (object · slot → material). Remove an entry with ×.

Things to know:

- The page loads with the materials the scene has **at export time**. If those match a variant, that button starts out selected. Otherwise an extra **Original** button is added.
- A slot that a variant doesn't mention keeps its default material.
- Only exported objects count. Order the buttons with the arrows.
- Turn the whole feature off with the checkbox in the panel header. *Variant Picker* hides the buttons but still exports the variants.
- Under the hood this uses the glTF exporter's own variant support. The add-on rebuilds that data on every export, so don't use the glTF add-on's own *Material Variants* panel at the same time.
- With *Apply Modifiers*, meshes that have variants are briefly given their modifiers applied during the export (the glTF exporter loses variants on meshes with modifiers otherwise). Everything is restored afterwards. Meshes deformed by an armature are skipped with a warning.

### Environment / lighting

- Lighting comes from the world's **Environment Texture** (HDR or EXR). It's written as `environment.hdr`, downscaled to *Max HDR Width* (default 2048).
- Mapping-node **Z rotation** is baked into the image, because model-viewer can't rotate an environment. X/Y rotation is ignored.
- World **Strength** and Film **Exposure** map to `exposure`. The view transform maps to `tone-mapping` (AgX → agx, Filmic → aces, Standard → none).
- **Lighting**: *HDR* lights the model with the exported HDR, like Blender. *Neutral Studio* uses model-viewer's bright studio light and keeps the HDR only as the background. That's useful when the HDR is dim, like an indoor scene.
- **Exposure** is a brightness multiplier on top of the Blender values. **Tone Mapping** can override the Blender mapping; *Neutral* is model-viewer's default for products.
- Toggles: *Export Environment HDR*, *Show as Background* (whether the background is shown when the page loads) and *Background Toggle Button* (an on/off button on the page). Film → Transparent only affects a plain world colour, not the HDR background.
- Blender lights are **not** exported. model-viewer lights only with the environment.

## Workflow

1. Set the **Output Folder**. The default is `//docs/` (next to the .blend); for the demo scene it's `//../docs/`, the repo's `docs/` folder.
2. Click **Export for Model Viewer**. It writes `model.glb`, `scene.json`, `environment.hdr`, and the viewer files if they don't exist yet.
3. Preview locally. The page has to be served over http; opening the file directly won't work:
   ```sh
   cd docs && python3 -m http.server 8000   # open http://localhost:8000
   ```
4. Commit and push `docs/` (see below).

URLs like `…/#door` open a hotspot directly, and the browser's back button
closes it.

## Hosting on GitHub Pages

In **Settings → Pages**, choose **Deploy from a branch** → `main` → **/docs**.
The site is published at **https://maholmbe.github.io/model-viewer/** and
updates a minute or two after each push (progress shows in the **Actions**
tab). HTTPS and the CDN are included. Keep files under 100 MB.

After a re-export, `scene.json` and the GLB are re-fetched automatically.
The HDR is not cache-busted, because model-viewer only recognises URLs that end
in `.hdr`. GitHub Pages caches files for about 10 minutes, so a changed HDR
can take that long to show up (or force-reload with Ctrl+Shift+R).

## Customising the page

The viewer files are only copied **if missing**, so you can edit
`docs/viewer.css`, `viewer.js` and `index.html` freely. Edit the
files in `templates/` instead if the change should apply to every future
project, and set *Viewer Files → Always Overwrite* to refresh them.

## Extending

`scene.json` is the contract between Blender and the page (it has a
`version` field). To add a feature:

1. Write a `collect_<thing>(ctx)` function in `core.py` that adds data to `ctx.data` (and `ctx.warn(...)` for problems), then add it to `COLLECTORS`.
2. Read the new data in `viewer.js`.

Ideas that fit this structure:

- Several animations at once (`appendAnimation` / `detachAnimation`)
- Switch the material variant from a hotspot
- AR button (`ar ar-modes="webxr scene-viewer quick-look"`)
- Guided tour: step through hotspots in `order` with next/prev buttons
- Multiple languages for labels and descriptions
- Poster image rendered from `CAM_home` for faster first paint

## Demo

```sh
blender -b --factory-startup --python tools/build_demo.py
```

This builds `demo/demo_cabinet.blend` (door, lid + gadget, spinner, three
hotspots, three cameras, Blender's bundled forest HDRI rotated 45°). The
door and lid hotspots stick to their meshes, and the door comes in three
colour variants. It exports to `docs/`, or to the folder given after `--`.
Note that `docs/` currently holds the hand drill, so export the demo
somewhere else, e.g. `-- /tmp/demo`.
