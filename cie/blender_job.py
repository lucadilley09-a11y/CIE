"""Runs INSIDE Blender (background mode). Started by cie/blender.py as:

    blender -b --factory-startup --disable-autoexec --python blender_job.py -- <job_dir>

<job_dir>/job.json holds {"op": ..., "params": {...}}. The result goes to <job_dir>/result.json.
Only bpy, mathutils and the standard library are used. Works on Blender 4.2 LTS through 5.x.
This file never runs code from the job; it only reads parameters.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback

import bpy  # type: ignore[import-not-found]
from mathutils import Matrix, Vector  # type: ignore[import-not-found]

JOB_DIR = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else os.getcwd()
LOG: list[str] = []


def log(msg: str) -> None:
    LOG.append(msg)
    print("[cie]", msg)


# ---------------------------------------------------------------------------
# Scene helpers
# ---------------------------------------------------------------------------


def clear_scene() -> None:
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.images, bpy.data.textures, bpy.data.lights, bpy.data.cameras):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


def import_file(path: str) -> list:
    before = set(bpy.data.objects)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif ext == ".stl":
        # CIE writes Y-up STL; Blender's STL importer assumes Z-up unless told
        if hasattr(bpy.ops.wm, "stl_import"):
            bpy.ops.wm.stl_import(filepath=path, up_axis="Y", forward_axis="NEGATIVE_Z")
        else:
            bpy.ops.import_mesh.stl(filepath=path, axis_up="Y", axis_forward="-Z")
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".blend":
        with bpy.data.libraries.load(path, link=False) as (src, dst):
            dst.objects = list(src.objects)
        for obj in dst.objects:
            if obj is not None:
                bpy.context.scene.collection.objects.link(obj)
    else:
        raise ValueError(f"Unsupported input '{ext}'.")
    new = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new if o.type == "MESH"]
    # drop importer helper empties/cameras/lights so they don't pollute renders and exports
    for o in new:
        if o.type in ("CAMERA", "LIGHT"):
            bpy.data.objects.remove(o, do_unlink=True)
    if not meshes:
        raise ValueError("The file has no mesh objects.")
    return meshes


def bake_transforms(objs: list, weld: bool = True) -> None:
    """Apply parent and object transforms into the mesh data (glTF imports often have scaled parents),
    then join corners that sit on the same spot. CIE (and many exporters) store each triangle with its
    own corners; until they're joined, smoothing, bevels and bakes treat every triangle as separate."""
    import bmesh  # type: ignore[import-not-found]
    for o in objs:
        mw = o.matrix_world.copy()
        o.parent = None
        if o.data.users > 1:
            o.data = o.data.copy()
        o.data.transform(mw)
        o.matrix_world = Matrix.Identity(4)
        if weld:
            bm = bmesh.new()
            bm.from_mesh(o.data)
            before = len(bm.verts)
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
            after = len(bm.verts)
            bm.to_mesh(o.data)
            bm.free()
            o.data.update()
            if after < before:
                log(f"joined {before - after} duplicate corners on {o.name}")
            # Imported files carry fixed per-corner normals that override any smoothing we do later.
            if getattr(o.data, "has_custom_normals", False):
                with bpy.context.temp_override(object=o, active_object=o, selected_objects=[o], selected_editable_objects=[o]):
                    bpy.ops.mesh.customdata_custom_splitnormals_clear()
                log(f"cleared imported custom normals on {o.name}")


def join(objs: list):
    if len(objs) == 1:
        return objs[0]
    ctx = {"active_object": objs[0], "object": objs[0], "selected_objects": objs, "selected_editable_objects": objs}
    with bpy.context.temp_override(**ctx):
        bpy.ops.object.join()
    return objs[0]


def apply_modifiers(obj) -> None:
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    mesh = bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=True, depsgraph=dg)
    old = obj.data
    obj.modifiers.clear()
    obj.data = mesh
    if old.users == 0:
        bpy.data.meshes.remove(old)


def set_smooth(obj, angle_deg: float | None) -> None:
    me = obj.data
    me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))
    if angle_deg is not None:
        if hasattr(me, "set_sharp_from_angle"):  # Blender 4.1+
            me.set_sharp_from_angle(angle=math.radians(angle_deg))
        elif hasattr(me, "use_auto_smooth"):
            me.use_auto_smooth = True
            me.auto_smooth_angle = math.radians(angle_deg)
    me.update()


def bounds(objs: list) -> tuple[Vector, Vector]:
    lo = Vector((1e18, 1e18, 1e18))
    hi = Vector((-1e18, -1e18, -1e18))
    for o in objs:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            lo = Vector((min(lo.x, w.x), min(lo.y, w.y), min(lo.z, w.z)))
            hi = Vector((max(hi.x, w.x), max(hi.y, w.y), max(hi.z, w.z)))
    return lo, hi


def mesh_stats(objs: list) -> dict:
    tris = 0
    verts = 0
    for o in objs:
        me = o.data
        me.calc_loop_triangles()
        tris += len(me.loop_triangles)
        verts += len(me.vertices)
    lo, hi = bounds(objs)
    return {"triangles": tris, "vertices": verts, "size_m": [round(hi.x - lo.x, 4), round(hi.y - lo.y, 4), round(hi.z - lo.z, 4)]}


# ---------------------------------------------------------------------------
# Procedural materials (realistic shading; bake to make them portable)
# ---------------------------------------------------------------------------


def _set(node, names: tuple, value) -> None:
    for n in names:
        if n in node.inputs:
            node.inputs[n].default_value = value
            return


def _rgba(hexstr: str, alpha: float = 1.0) -> tuple:
    h = hexstr.lstrip("#")
    srgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb]
    return (lin[0], lin[1], lin[2], alpha)


MATERIALS = {
    "skin": {"color": "#c98e72", "roughness": 0.45, "sss": 0.25, "sss_radius": (1.0, 0.35, 0.2), "bump": 0.15, "noise": 120.0},
    "wood": {"color": "#8a5a33", "roughness": 0.6, "wave": True, "bump": 0.2},
    "metal": {"color": "#c8c8c8", "roughness": 0.3, "metallic": 1.0, "noise": 30.0, "rough_var": 0.15},
    "gold": {"color": "#f2c25a", "roughness": 0.25, "metallic": 1.0},
    "stone": {"color": "#8a8780", "roughness": 0.85, "voronoi": True, "bump": 0.5, "noise": 8.0},
    "plastic": {"color": "#d04040", "roughness": 0.35},
    "fabric": {"color": "#4a6fa5", "roughness": 0.9, "noise": 300.0, "bump": 0.1},
    "rubber": {"color": "#222222", "roughness": 0.8},
    "glass": {"color": "#e0f0f5", "roughness": 0.02, "transmission": 1.0},
    "eye": {"color": "#f2efe8", "roughness": 0.05, "coat": 1.0},
}


def make_material(name: str, preset: str, color: str | None = None):
    spec = dict(MATERIALS.get(preset, MATERIALS["plastic"]))
    if color:
        spec["color"] = color
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    _set(bsdf, ("Base Color",), _rgba(spec["color"]))
    _set(bsdf, ("Roughness",), spec.get("roughness", 0.5))
    _set(bsdf, ("Metallic",), spec.get("metallic", 0.0))
    if spec.get("sss"):
        _set(bsdf, ("Subsurface Weight", "Subsurface"), spec["sss"])
        _set(bsdf, ("Subsurface Radius",), spec.get("sss_radius", (1.0, 0.2, 0.1)))
        _set(bsdf, ("Subsurface Scale",), 0.02)
    if spec.get("transmission"):
        _set(bsdf, ("Transmission Weight", "Transmission"), spec["transmission"])
    if spec.get("coat"):
        _set(bsdf, ("Coat Weight", "Clearcoat"), spec["coat"])
    links = nt.links
    coord = nt.nodes.new("ShaderNodeTexCoord")
    height = None
    if spec.get("wave"):
        wave = nt.nodes.new("ShaderNodeTexWave")
        wave.inputs["Scale"].default_value = 3.0
        wave.inputs["Distortion"].default_value = 6.0
        links.new(coord.outputs["Object"], wave.inputs["Vector"])
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].color = _rgba(spec["color"])
        dark = [c * 0.55 for c in _rgba(spec["color"])[:3]] + [1.0]
        ramp.color_ramp.elements[1].color = dark
        links.new(wave.outputs["Fac"], ramp.inputs["Fac"])
        links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
        height = wave.outputs["Fac"]
    if spec.get("voronoi"):
        vor = nt.nodes.new("ShaderNodeTexVoronoi")
        vor.inputs["Scale"].default_value = 6.0
        links.new(coord.outputs["Object"], vor.inputs["Vector"])
        height = vor.outputs["Distance"]
    if spec.get("noise"):
        noise = nt.nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = spec["noise"]
        _set(noise, ("Detail",), 8.0)
        links.new(coord.outputs["Object"], noise.inputs["Vector"])
        if spec.get("rough_var"):
            mr = nt.nodes.new("ShaderNodeMapRange")
            mr.inputs["To Min"].default_value = spec["roughness"] - spec["rough_var"]
            mr.inputs["To Max"].default_value = spec["roughness"] + spec["rough_var"]
            links.new(noise.outputs["Fac"], mr.inputs["Value"])
            links.new(mr.outputs["Result"], bsdf.inputs["Roughness"])
        if height is None:
            height = noise.outputs["Fac"]
    if spec.get("bump") and height is not None:
        bump = nt.nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = spec["bump"]
        bump.inputs["Distance"].default_value = 0.01
        links.new(height, bump.inputs["Height"])
        links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return mat


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------


def primitive(spec: dict):
    shape = spec.get("shape", "cube")
    size = spec.get("size", 1.0)
    loc = tuple(spec.get("position", (0, 0, 0)))
    if shape == "cube":
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    elif shape == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, segments=32, ring_count=16, location=loc)
    elif shape == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=1.0, vertices=32, location=loc)
    elif shape == "torus":
        bpy.ops.mesh.primitive_torus_add(location=loc)
    else:
        raise ValueError(f"Unknown cutter shape '{shape}'.")
    obj = bpy.context.active_object
    s = size if isinstance(size, (list, tuple)) else (size, size, size)
    obj.scale = Vector(s)
    rot = spec.get("rotation", (0, 0, 0))
    obj.rotation_euler = tuple(math.radians(r) for r in rot)
    return obj


def to_blender_axes(v):
    """CIE/glTF are Y-up; Blender is Z-up. glTF import already converts meshes, so positions given by
    the caller in CIE space (x, y, z) map to Blender (x, -z, y)."""
    x, y, z = v
    return (x, -z, y)


def do_ops(obj, ops: list) -> list:
    applied = []
    for op in ops:
        kind = op.get("op")
        if kind == "bevel":
            m = obj.modifiers.new("Bevel", "BEVEL")
            m.width = float(op.get("width", 0.02))
            m.segments = int(op.get("segments", 3))
            m.limit_method = "ANGLE"
            m.angle_limit = math.radians(float(op.get("angle", 30)))
            if hasattr(m, "harden_normals"):
                m.harden_normals = bool(op.get("harden_normals", True))  # flat faces stay flat under smooth shading
            if hasattr(m, "profile"):
                m.profile = float(op.get("profile", 0.5))
        elif kind == "boolean":
            cutter_spec = dict(op.get("with") or {})
            if "position" in cutter_spec:
                cutter_spec["position"] = to_blender_axes(cutter_spec["position"])
            if "size" in cutter_spec and isinstance(cutter_spec["size"], (list, tuple)):
                sx, sy, sz = cutter_spec["size"]
                cutter_spec["size"] = (sx, sz, sy)
            if cutter_spec.get("file"):
                cutter = join(import_file(cutter_spec["file"]))
            else:
                cutter = primitive(cutter_spec)
            apply_modifiers(obj)  # finish earlier ops first so the cut sees the real shape
            keep = obj.data.copy()
            operation = {"union": "UNION", "difference": "DIFFERENCE", "intersect": "INTERSECT"}[op.get("operation", "difference")]
            solvers = [s_ for s_ in ("EXACT", "MANIFOLD", "FLOAT", "FAST")
                       if s_ in [e.identifier for e in bpy.types.BooleanModifier.bl_rna.properties["solver"].enum_items]]
            done = False
            for solver in solvers:
                obj.data = keep.copy()
                m = obj.modifiers.new("Boolean", "BOOLEAN")
                m.operation = operation
                m.object = cutter
                m.solver = solver
                if solver == "EXACT":
                    # game models are often several overlapping parts: let the exact solver handle that
                    if hasattr(m, "use_self"):
                        m.use_self = True
                    if hasattr(m, "use_hole_tolerant"):
                        m.use_hole_tolerant = True
                try:
                    apply_modifiers(obj)
                except RuntimeError as exc:
                    log(f"boolean {solver} failed: {exc}")
                    obj.modifiers.clear()
                    continue
                if len(obj.data.polygons) > 0:
                    done = True
                    if solver != solvers[0]:
                        log(f"boolean used the {solver} solver (the exact one returned nothing)")
                    break
                log(f"boolean {solver} removed everything; trying the next solver")
            bpy.data.objects.remove(cutter, do_unlink=True)
            if not done:
                obj.data = keep
                raise ValueError("The boolean removed the whole model with every solver. Check the cutter's position/size "
                                 "(CIE coordinates, metres, Y up), or remesh the model first so it is one closed shell.")
        elif kind == "subdivide":
            m = obj.modifiers.new("Subdivision", "SUBSURF")
            m.levels = m.render_levels = int(op.get("levels", 2))
            if op.get("simple"):
                m.subdivision_type = "SIMPLE"
        elif kind == "remesh":
            m = obj.modifiers.new("Remesh", "REMESH")
            m.mode = "VOXEL"
            m.voxel_size = float(op.get("voxel_size", 0.02))
            if hasattr(m, "use_smooth_shade"):
                m.use_smooth_shade = True
        elif kind == "decimate":
            m = obj.modifiers.new("Decimate", "DECIMATE")
            m.ratio = float(op.get("ratio", 0.5))
        elif kind == "weld":
            m = obj.modifiers.new("Weld", "WELD")
            m.merge_threshold = float(op.get("distance", 0.0001))
        elif kind == "smooth":
            m = obj.modifiers.new("Smooth", "SMOOTH")
            m.factor = float(op.get("factor", 0.5))
            m.iterations = int(op.get("iterations", 5))
        elif kind == "solidify":
            m = obj.modifiers.new("Solidify", "SOLIDIFY")
            m.thickness = float(op.get("thickness", 0.02))
        elif kind == "mirror":
            m = obj.modifiers.new("Mirror", "MIRROR")
            axis = op.get("axis", "x")
            m.use_axis = (axis == "x", axis == "z", axis == "y")  # CIE y-up -> Blender z-up
            m.use_clip = True
        elif kind == "array":
            m = obj.modifiers.new("Array", "ARRAY")
            m.count = int(op.get("count", 3))
            off = to_blender_axes(op.get("offset", (1, 0, 0)))
            m.relative_offset_displace = off
        elif kind == "displace":
            tex = bpy.data.textures.new("cie_noise", type="CLOUDS")
            tex.noise_scale = float(op.get("scale", 0.25))
            tex.noise_depth = int(op.get("detail", 2))
            m = obj.modifiers.new("Displace", "DISPLACE")
            m.texture = tex
            m.strength = float(op.get("strength", 0.03))
            m.mid_level = 0.5
        elif kind == "triangulate":
            obj.modifiers.new("Triangulate", "TRIANGULATE")
        elif kind == "shade_smooth":
            apply_modifiers(obj)
            set_smooth(obj, float(op.get("angle", 30)) if op.get("angle", 30) is not None else None)
            # Weighted normals: big flat faces decide their corners' normals, so large panels next to
            # bevels stay flat instead of showing dark smudges (standard hard-surface shading).
            wn = obj.modifiers.new("WeightedNormal", "WEIGHTED_NORMAL")
            wn.mode = "FACE_AREA"
            wn.weight = 100
            wn.keep_sharp = True
            apply_modifiers(obj)
        elif kind == "shade_flat":
            apply_modifiers(obj)
            obj.data.polygons.foreach_set("use_smooth", [False] * len(obj.data.polygons))
        elif kind == "material":
            mat = make_material(op.get("name", op.get("preset", "mat")), op.get("preset", "plastic"), op.get("color"))
            target = op.get("replace")
            if target:
                for i, slot in enumerate(obj.material_slots):
                    if slot.material and slot.material.name.split(".")[0] == target:
                        obj.material_slots[i].material = mat
            else:
                obj.data.materials.clear()
                obj.data.materials.append(mat)
        else:
            raise ValueError(f"Unknown Blender op '{kind}'.")
        applied.append(kind)
    apply_modifiers(obj)
    return applied


def export(objs: list, path: str, fmt: str) -> str:
    for o in bpy.data.objects:
        o.select_set(o in objs)
    bpy.context.view_layer.objects.active = objs[0]
    if fmt in ("glb", "gltf"):
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB" if fmt == "glb" else "GLTF_SEPARATE",
                                  use_selection=True, export_apply=True)
    elif fmt == "fbx":
        bpy.ops.export_scene.fbx(filepath=path, use_selection=True, apply_unit_scale=True, apply_scale_options="FBX_SCALE_ALL",
                                 path_mode="COPY", embed_textures=True)
    elif fmt == "obj":
        if hasattr(bpy.ops.wm, "obj_export"):
            bpy.ops.wm.obj_export(filepath=path, export_selected_objects=True)
        else:
            bpy.ops.export_scene.obj(filepath=path, use_selection=True)
    elif fmt == "blend":
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
    else:
        raise ValueError(f"Unknown export format '{fmt}'.")
    return path


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def engine_id(name: str) -> str:
    items = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    want = {"eevee": ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"], "cycles": ["CYCLES"], "workbench": ["BLENDER_WORKBENCH"]}[name]
    for w in want:
        if w in items:
            return w
    return items[0]


def setup_stage(objs: list, transparent: bool = True) -> tuple[Vector, float]:
    scene = bpy.context.scene
    lo, hi = bounds(objs)
    centre = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 1e-3)
    world = bpy.data.worlds.new("cie_world") if not scene.world else scene.world
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.05, 0.055, 0.065, 1)
        bg.inputs[1].default_value = 0.35
    for name, energy, direction, size in (("key", 420, (1.0, -1.2, 1.4), 1.2), ("fill", 140, (-1.4, -0.8, 0.6), 2.0), ("rim", 280, (0.2, 1.5, 1.2), 1.0)):
        light = bpy.data.lights.new(name, "AREA")
        light.energy = energy * radius * radius
        light.size = size * radius
        lo_ = bpy.data.objects.new(name, light)
        scene.collection.objects.link(lo_)
        d = Vector(direction).normalized()
        lo_.location = centre + d * radius * 4
        lo_.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    scene.render.film_transparent = transparent
    return centre, radius


def add_camera(centre: Vector, radius: float, yaw: float, pitch: float, lens: float = 50.0):
    cam_data = bpy.data.cameras.new("cie_cam")
    cam_data.lens = lens
    cam = bpy.data.objects.new("cie_cam", cam_data)
    bpy.context.scene.collection.objects.link(cam)
    fov = 2 * math.atan(36 / (2 * lens))
    dist = radius / math.sin(fov / 2) * 1.08
    y, p = math.radians(yaw), math.radians(pitch)
    # yaw 0 looks at the model's front (+Y in Blender after glTF import = CIE +Z... glTF +Z maps to Blender -Y)
    direction = Vector((math.sin(y) * math.cos(p), -math.cos(y) * math.cos(p), math.sin(p)))
    cam.location = centre + direction * dist
    cam.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()
    cam_data.clip_end = dist * 10
    cam_data.clip_start = dist / 1000
    bpy.context.scene.camera = cam
    return cam


def render_views(objs: list, out_dir: str, views: list, engine: str, size: int, samples: int, stem: str) -> list:
    scene = bpy.context.scene
    tried = [engine] + [e for e in ("eevee", "cycles", "workbench") if e != engine]
    centre, radius = setup_stage(objs, True)
    scene.render.resolution_x = scene.render.resolution_y = int(size)
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    if hasattr(scene.view_settings, "view_transform"):
        try:
            scene.view_settings.view_transform = "AgX"
        except TypeError:
            scene.view_settings.view_transform = "Filmic"
    outputs = []
    used = None
    for eng in tried:
        try:
            scene.render.engine = engine_id(eng)
            if eng == "cycles":
                scene.cycles.samples = int(samples)
                scene.cycles.use_denoising = True
                scene.cycles.device = "CPU"
            elif eng == "eevee" and hasattr(scene, "eevee"):
                if hasattr(scene.eevee, "taa_render_samples"):
                    scene.eevee.taa_render_samples = max(16, int(samples))
            outputs = []
            for v in views:
                cam = add_camera(centre, radius, float(v.get("yaw", 35)), float(v.get("pitch", 20)), float(v.get("lens", 50)))
                path = os.path.join(out_dir, f"{stem}_{v.get('name', 'view')}.png")
                scene.render.filepath = path
                bpy.ops.render.render(write_still=True)
                bpy.data.objects.remove(cam, do_unlink=True)
                if not os.path.exists(path):
                    raise RuntimeError("render produced no file")
                outputs.append(path)
            used = eng
            break
        except Exception as exc:  # noqa: BLE001 - try the next engine
            log(f"{eng} render failed: {exc}")
    if used is None:
        raise RuntimeError("Every render engine failed; see the log.")
    if used != engine:
        log(f"Rendered with {used} because {engine} was not available in background mode.")
    return [{"path": p, "engine": used} for p in outputs]


# ---------------------------------------------------------------------------
# Baking
# ---------------------------------------------------------------------------


def smart_uv(obj) -> None:
    bpy.context.view_layer.objects.active = obj
    for o in bpy.data.objects:
        o.select_set(o == obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.02)
    bpy.ops.object.mode_set(mode="OBJECT")


def bake(low, high, out_dir: str, stem: str, size: int, maps: list, extrusion: float | None) -> dict:
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 32
    images = {}
    # one bake target material on the low-poly mesh
    bake_mat = bpy.data.materials.new(f"{stem}_baked")
    bake_mat.use_nodes = True
    nt = bake_mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    for m in maps:
        img = bpy.data.images.new(f"{stem}_{m}", width=size, height=size, alpha=False, float_buffer=False)
        if m == "normal":
            img.colorspace_settings.name = "Non-Color"
        images[m] = img
    # high-poly materials must stay for colour baking; low gets the target material
    low.data.materials.clear()
    low.data.materials.append(bake_mat)
    for o in bpy.data.objects:
        o.select_set(o in (low, high))
    bpy.context.view_layer.objects.active = low
    lo, hi = bounds([low])
    dim = max((hi - lo).length, 1e-3)
    ext = float(extrusion) if extrusion else dim * 0.04  # far enough to catch rounded/bevelled/subdivided detail
    log(f"bake cage extrusion {ext:.4f} m")
    tex_nodes = {}
    for m, img in images.items():
        node = nt.nodes.new("ShaderNodeTexImage")
        node.image = img
        tex_nodes[m] = node
    results = {}
    for m in maps:
        for n in tex_nodes.values():
            n.select = False
        tex_nodes[m].select = True
        nt.nodes.active = tex_nodes[m]
        # max_ray_distance 0 = no limit: subdivided/organic detail can sit well inside the game mesh
        kwargs = {"use_selected_to_active": True, "margin": 8, "cage_extrusion": ext, "max_ray_distance": 0.0}
        if m == "normal":
            bpy.ops.object.bake(type="NORMAL", normal_space="TANGENT", **kwargs)
        elif m == "ao":
            bpy.ops.object.bake(type="AO", **kwargs)
        elif m == "color":
            bpy.ops.object.bake(type="DIFFUSE", pass_filter={"COLOR"}, **kwargs)
        elif m == "roughness":
            bpy.ops.object.bake(type="ROUGHNESS", **kwargs)
        path = os.path.join(out_dir, f"{stem}_{m}.png")
        img = images[m]
        img.filepath_raw = path
        img.file_format = "PNG"
        img.save()
        results[m] = path
        log(f"baked {m}")
    # final material: wire the maps in so exporters carry them
    links = nt.links
    if "color" in tex_nodes:
        if "ao" in tex_nodes:
            mix = nt.nodes.new("ShaderNodeMix") if hasattr(bpy.types, "ShaderNodeMix") else nt.nodes.new("ShaderNodeMixRGB")
            if mix.bl_idname == "ShaderNodeMix":
                mix.data_type = "RGBA"
                mix.blend_type = "MULTIPLY"
                mix.inputs["Factor"].default_value = 0.8
                links.new(tex_nodes["color"].outputs["Color"], mix.inputs[6])
                links.new(tex_nodes["ao"].outputs["Color"], mix.inputs[7])
                links.new(mix.outputs[2], bsdf.inputs["Base Color"])
            else:
                mix.blend_type = "MULTIPLY"
                mix.inputs["Fac"].default_value = 0.8
                links.new(tex_nodes["color"].outputs["Color"], mix.inputs["Color1"])
                links.new(tex_nodes["ao"].outputs["Color"], mix.inputs["Color2"])
                links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
        else:
            links.new(tex_nodes["color"].outputs["Color"], bsdf.inputs["Base Color"])
    if "normal" in tex_nodes:
        nm = nt.nodes.new("ShaderNodeNormalMap")
        links.new(tex_nodes["normal"].outputs["Color"], nm.inputs["Color"])
        links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    if "roughness" in tex_nodes:
        links.new(tex_nodes["roughness"].outputs["Color"], bsdf.inputs["Roughness"])
    for img in images.values():
        img.pack()
    return results


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def op_info(p: dict) -> dict:
    import addon_utils  # type: ignore[import-not-found]
    names = {m.__name__ for m in addon_utils.modules()}
    engines = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    return {"version": bpy.app.version_string, "python": sys.version.split()[0], "engines": engines,
            "gltf": "io_scene_gltf2" in names, "fbx": "io_scene_fbx" in names}


def op_process(p: dict) -> dict:
    clear_scene()
    objs = import_file(p["input"])
    # Don't weld here: models made of touching parts (a crate's rails and posts) would fuse and then
    # tear apart under bevels and booleans. Welding is only for baking, where smooth shading needs it.
    bake_transforms(objs, weld=bool(p.get("weld", False)))
    obj = join(objs) if p.get("join", True) else objs[0]
    before = mesh_stats([obj])
    applied = do_ops(obj, p.get("ops", []))
    geometry = {"bevel", "boolean", "subdivide", "remesh", "decimate", "solidify", "mirror", "array", "displace", "weld"}
    uv_mode = p.get("uv", "auto")
    if uv_mode == "unwrap" or (uv_mode == "auto" and geometry & set(applied)):
        smart_uv(obj)  # old UVs don't fit the new geometry (bevel/boolean faces get zero-area UVs)
        applied.append("uv_unwrap")
    after = mesh_stats([obj])
    files = [export([obj], os.path.join(p["out_dir"], f"{p['stem']}.{fmt}"), fmt) for fmt in p.get("formats", ["glb"])]
    out = {"before": before, "after": after, "applied": applied, "files": files}
    if p.get("render"):
        out["renders"] = render_views([obj], p["out_dir"], p["render"]["views"], p["render"]["engine"], p["render"]["size"],
                                      p["render"].get("samples", 64), p["stem"])
    return out


def op_render(p: dict) -> dict:
    clear_scene()
    objs = import_file(p["input"])
    return {"renders": render_views(objs, p["out_dir"], p["views"], p["engine"], p["size"], p.get("samples", 64), p["stem"]),
            "stats": mesh_stats(objs)}


def op_bake(p: dict) -> dict:
    clear_scene()
    low = join(import_file(p["low"]))
    bake_transforms([low], weld=True)
    if p.get("high"):
        high = join(import_file(p["high"]))
        bake_transforms([high])
    else:
        # the detailed source is a copy of the model itself, optionally made richer
        # (bevel/subdivide/displace, procedural materials); it is baked back onto the low-poly
        high = low.copy()
        high.data = low.data.copy()
        bpy.context.scene.collection.objects.link(high)
        if p.get("high_ops"):
            do_ops(high, p["high_ops"])
    if p.get("material_ops"):
        do_ops(high, p["material_ops"])
    # always unwrap fresh: CIE/box UVs may overlap, which ruins a bake
    smart_uv(low)
    # Bake onto a smooth-shaded game mesh (sharp only at real corners): a flat-shaded low-poly makes the
    # normal map fight every facet, which renders as harsh dark blotches.
    set_smooth(low, float(p.get("smooth_angle", 60)))
    maps = bake(low, high, p["out_dir"], p["stem"], int(p.get("size", 1024)), p.get("maps", ["normal", "ao", "color"]), p.get("extrusion"))
    bpy.data.objects.remove(high, do_unlink=True)
    files = [export([low], os.path.join(p["out_dir"], f"{p['stem']}.{fmt}"), fmt) for fmt in p.get("formats", ["glb"])]
    out = {"maps": maps, "files": files, "stats": mesh_stats([low])}
    if p.get("render"):
        out["renders"] = render_views([low], p["out_dir"], p["render"]["views"], p["render"]["engine"], p["render"]["size"],
                                      p["render"].get("samples", 64), p["stem"])
    return out


OPS = {"info": op_info, "process": op_process, "render": op_render, "bake": op_bake}


def main() -> None:
    t0 = time.time()
    result: dict = {"ok": False}
    try:
        with open(os.path.join(JOB_DIR, "job.json"), encoding="utf-8") as fh:
            job = json.load(fh)
        fn = OPS.get(job.get("op"))
        if fn is None:
            raise ValueError(f"Unknown job op '{job.get('op')}'.")
        result = {"ok": True, "data": fn(job.get("params") or {})}
    except Exception as exc:  # noqa: BLE001 - report every failure back to CIE
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-4000:]}
    result["log"] = LOG
    result["seconds"] = round(time.time() - t0, 2)
    with open(os.path.join(JOB_DIR, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh)


main()
