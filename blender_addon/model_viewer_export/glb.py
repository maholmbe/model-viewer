"""Minimal GLB reader: JSON, accessors, node transforms and triangle geometry.

Used after export to inspect what was actually written (animation names,
material variants) and to compute surface hotspots, which reference vertex
indices of the *exported* meshes. Draco-compressed primitives are decoded
with the Draco library that ships with Blender's glTF add-on, which yields
the same vertex order as the decoder in the browser.
"""

import ctypes
import importlib
import json
import struct
from pathlib import Path

import numpy as np
from mathutils import Matrix, Quaternion, Vector

COMPONENT_DTYPES = {
    5120: np.int8, 5121: np.uint8, 5122: np.int16,
    5123: np.uint16, 5125: np.uint32, 5126: np.float32,
}
TYPE_SIZES = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
GL_UNSIGNED_INT = 5125
GL_FLOAT = 5126
MODE_TRIANGLES = 4


class GLB:
    def __init__(self, path):
        data = Path(path).read_bytes()
        magic, _version, length = struct.unpack_from("<4sII", data, 0)
        if magic != b"glTF":
            raise ValueError("Not a GLB file: %s" % path)
        self.json = None
        self.bin = b""
        offset = 12
        while offset < length:
            chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
            offset += 8
            chunk = data[offset:offset + chunk_len]
            offset += chunk_len
            if chunk_type == 0x4E4F534A:    # 'JSON'
                self.json = json.loads(chunk)
            elif chunk_type == 0x004E4942:  # 'BIN\0'
                self.bin = chunk
        if self.json is None:
            raise ValueError("GLB has no JSON chunk: %s" % path)

    # --- raw data ---------------------------------------------------------

    def buffer_view(self, index):
        bv = self.json["bufferViews"][index]
        if bv.get("buffer", 0) != 0:
            raise ValueError("External glTF buffers are not supported")
        start = bv.get("byteOffset", 0)
        return self.bin[start:start + bv["byteLength"]], bv.get("byteStride")

    def accessor(self, index):
        acc = self.json["accessors"][index]
        dtype = np.dtype(COMPONENT_DTYPES[acc["componentType"]])
        width = TYPE_SIZES[acc["type"]]
        count = acc["count"]
        if "bufferView" not in acc:
            return np.zeros((count, width), dtype)
        raw, stride = self.buffer_view(acc["bufferView"])
        arr = np.ndarray((count, width), dtype, buffer=raw, offset=acc.get("byteOffset", 0),
                         strides=(stride or dtype.itemsize * width, dtype.itemsize))
        return arr.copy()

    # --- scene graph ------------------------------------------------------

    def node_world_matrices(self):
        """{node index: world Matrix} for the default scene (rest pose)."""
        nodes = self.json.get("nodes", [])
        scenes = self.json.get("scenes", [])
        if not scenes:
            return {}
        roots = scenes[self.json.get("scene", 0)].get("nodes", [])
        result = {}
        stack = [(i, Matrix.Identity(4)) for i in roots]
        while stack:
            index, parent = stack.pop()
            world = parent @ local_matrix(nodes[index])
            result[index] = world
            stack.extend((c, world) for c in nodes[index].get("children", []))
        return result

    def primitive_geometry(self, prim):
        """(positions Nx3, triangles Mx3) of a triangle primitive, or None."""
        if prim.get("mode", MODE_TRIANGLES) != MODE_TRIANGLES:
            return None
        draco = prim.get("extensions", {}).get("KHR_draco_mesh_compression")
        if draco is not None:
            raw, _stride = self.buffer_view(draco["bufferView"])
            return decode_draco(raw, draco["attributes"]["POSITION"])
        pos = self.accessor(prim["attributes"]["POSITION"]).astype(np.float64)
        if "indices" in prim:
            idx = self.accessor(prim["indices"]).reshape(-1).astype(np.int64)
        else:
            idx = np.arange(len(pos), dtype=np.int64)
        return pos, idx[:len(idx) // 3 * 3].reshape(-1, 3)


def local_matrix(node):
    if "matrix" in node:
        m = node["matrix"]  # column-major
        return Matrix([m[0:4], m[4:8], m[8:12], m[12:16]]).transposed()
    t = Vector(node.get("translation", (0, 0, 0)))
    x, y, z, w = node.get("rotation", (0, 0, 0, 1))
    s = node.get("scale", (1, 1, 1))
    return Matrix.LocRotScale(t, Quaternion((w, x, y, z)), Vector(s))


# ---------------------------------------------------------------------------
# Draco
# ---------------------------------------------------------------------------

_draco_dll = None


def _draco_library():
    """Load the Draco bridge library bundled with Blender's glTF add-on."""
    global _draco_dll
    if _draco_dll is not None:
        return _draco_dll
    mod = None
    for name in ("io_scene_gltf2.blender.imp.draco_compression_extension",          # Blender 4.4+
                 "io_scene_gltf2.blender.imp.gltf2_blender_draco_compression_extension"):
        try:
            mod = importlib.import_module(name)
            break
        except ImportError:
            continue
    if mod is None:
        raise RuntimeError("glTF add-on's Draco support not found")
    try:
        path = mod.dll_path("bf_intern_draco_bridge", "Draco")
    except TypeError:  # older add-on versions take no arguments
        path = mod.dll_path()
    dll = ctypes.cdll.LoadLibrary(str(Path(path).resolve()))

    c_void_p, c_bool, c_size_t, c_uint32 = ctypes.c_void_p, ctypes.c_bool, ctypes.c_size_t, ctypes.c_uint32
    sigs = {
        "decoderCreate": (c_void_p, []),
        "decoderRelease": (None, [c_void_p]),
        "decoderDecode": (c_bool, [c_void_p, c_void_p, c_size_t]),
        "decoderGetVertexCount": (c_uint32, [c_void_p]),
        "decoderGetIndexCount": (c_uint32, [c_void_p]),
        "decoderReadAttribute": (c_bool, [c_void_p, c_uint32, c_size_t, ctypes.c_char_p]),
        "decoderGetAttributeByteLength": (c_size_t, [c_void_p, c_uint32]),
        "decoderCopyAttribute": (None, [c_void_p, c_uint32, c_void_p]),
        "decoderReadIndices": (c_bool, [c_void_p, c_size_t]),
        "decoderGetIndicesByteLength": (c_size_t, [c_void_p]),
        "decoderCopyIndices": (None, [c_void_p, c_void_p]),
    }
    for fn, (restype, argtypes) in sigs.items():
        getattr(dll, fn).restype = restype
        getattr(dll, fn).argtypes = argtypes
    _draco_dll = dll
    return dll


def decode_draco(raw, position_id):
    dll = _draco_library()
    decoder = dll.decoderCreate()
    try:
        raw = bytes(raw)
        if not dll.decoderDecode(decoder, raw, len(raw)):
            raise RuntimeError("Draco: could not decode primitive")
        if not dll.decoderReadIndices(decoder, GL_UNSIGNED_INT):
            raise RuntimeError("Draco: could not decode indices")
        buf = ctypes.create_string_buffer(dll.decoderGetIndicesByteLength(decoder))
        dll.decoderCopyIndices(decoder, buf)
        idx = np.frombuffer(buf.raw, dtype=np.uint32).astype(np.int64)

        if not dll.decoderReadAttribute(decoder, position_id, GL_FLOAT, b"VEC3"):
            raise RuntimeError("Draco: could not decode positions")
        buf = ctypes.create_string_buffer(dll.decoderGetAttributeByteLength(decoder, position_id))
        dll.decoderCopyAttribute(decoder, position_id, buf)
        pos = np.frombuffer(buf.raw, dtype=np.float32).reshape(-1, 3).astype(np.float64)
    finally:
        dll.decoderRelease(decoder)
    return pos, idx[:len(idx) // 3 * 3].reshape(-1, 3)
