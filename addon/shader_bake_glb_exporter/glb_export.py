"""Blender標準exporterを呼び、GLB 2.0を検証して原子的に確定する。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import struct
import uuid

import bpy

from .bake import BakeFailure, WorkObject


JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942


@dataclass(frozen=True)
class ParsedGlb:
    document: dict
    binary: bytes


@dataclass(frozen=True)
class PendingGlb:
    temporary_path: Path
    final_path: Path
    expected_meshes: int
    expected_materials: int


def _finite_json(value, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise BakeFailure(f"GLB JSONにNaNまたはInfがあります: {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            _finite_json(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _finite_json(child, f"{path}[{index}]")


def parse_glb(path: Path) -> ParsedGlb:
    data = path.read_bytes()
    if len(data) < 20:
        raise BakeFailure("GLBが短すぎます")
    magic, version, declared_length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2 or declared_length != len(data):
        raise BakeFailure("GLB 2.0ヘッダーが不正です")
    offset = 12
    document = None
    binary = b""
    while offset + 8 <= len(data):
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        end = offset + chunk_length
        if end > len(data):
            raise BakeFailure("GLB chunk境界が不正です")
        chunk = data[offset:end]
        if chunk_type == JSON_CHUNK:
            document = json.loads(chunk.decode("utf-8").rstrip("\x00 \t\r\n"))
        elif chunk_type == BIN_CHUNK:
            binary = chunk
        offset = end
    if offset != len(data) or document is None:
        raise BakeFailure("GLB chunk構成が不正です")
    _finite_json(document)
    return ParsedGlb(document, binary)


def _validate_png_images(parsed: ParsedGlb) -> None:
    document = parsed.document
    buffer_views = document.get("bufferViews", [])
    images = document.get("images", [])
    if not images:
        raise BakeFailure("GLBに画像がありません")
    for index, image in enumerate(images):
        if image.get("mimeType") != "image/png":
            raise BakeFailure(f"画像{index}がPNGではありません")
        view_index = image.get("bufferView")
        if not isinstance(view_index, int) or not 0 <= view_index < len(buffer_views):
            raise BakeFailure(f"画像{index}のbufferViewが不正です")
        view = buffer_views[view_index]
        start = int(view.get("byteOffset", 0))
        end = start + int(view.get("byteLength", 0))
        payload = parsed.binary[start:end]
        if len(payload) < 33 or payload[:8] != b"\x89PNG\r\n\x1a\n":
            raise BakeFailure(f"画像{index}のPNGヘッダーが不正です")
        width, height, bit_depth = struct.unpack_from(">IIB", payload, 16)
        if width not in {512, 1024, 2048} or height != width:
            raise BakeFailure(f"画像{index}の寸法が契約外です: {width}×{height}")
        if bit_depth != 8:
            raise BakeFailure(f"画像{index}が8bit PNGではありません")


def _validate_node_hierarchy(document: dict) -> None:
    """glTF nodeの参照、TRS/matrix排他、階層の単一親と非循環を検証する。"""

    nodes = document.get("nodes", [])
    meshes = document.get("meshes", [])
    parents: dict[int, int] = {}
    for node_index, node in enumerate(nodes):
        has_matrix = "matrix" in node
        has_trs = any(name in node for name in ("translation", "rotation", "scale"))
        if has_matrix and has_trs:
            raise BakeFailure(f"Node {node_index}がmatrixとTRSを同時に持っています")
        components = (("matrix", 16), ("translation", 3), ("rotation", 4), ("scale", 3))
        for name, length in components:
            if name not in node:
                continue
            value = node[name]
            if not isinstance(value, list) or len(value) != length:
                raise BakeFailure(f"Node {node_index}の{name}が不正です")
            if not all(isinstance(component, (int, float)) and math.isfinite(float(component)) for component in value):
                raise BakeFailure(f"Node {node_index}の{name}に非有限値があります")
        mesh_index = node.get("mesh")
        if mesh_index is not None and (not isinstance(mesh_index, int) or not 0 <= mesh_index < len(meshes)):
            raise BakeFailure(f"Node {node_index}のMesh参照が不正です")
        children = node.get("children", [])
        if not isinstance(children, list) or len(children) != len(set(children)):
            raise BakeFailure(f"Node {node_index}のchildrenが不正です")
        for child in children:
            if not isinstance(child, int) or not 0 <= child < len(nodes) or child == node_index:
                raise BakeFailure(f"Node {node_index}の子Node参照が不正です")
            if child in parents:
                raise BakeFailure(f"Node {child}が複数の親を持っています")
            parents[child] = node_index

    visited: set[int] = set()
    visiting: set[int] = set()

    def visit(node_index: int) -> None:
        if node_index in visiting:
            raise BakeFailure("Node階層が循環しています")
        if node_index in visited:
            return
        visiting.add(node_index)
        for child in nodes[node_index].get("children", []):
            visit(child)
        visiting.remove(node_index)
        visited.add(node_index)

    scenes = document.get("scenes", [])
    if not scenes:
        raise BakeFailure("GLBにSceneがありません")
    for scene_index, scene in enumerate(scenes):
        roots = scene.get("nodes", [])
        if not isinstance(roots, list):
            raise BakeFailure(f"Scene {scene_index}のroot Nodeが不正です")
        for root in roots:
            if not isinstance(root, int) or not 0 <= root < len(nodes) or root in parents:
                raise BakeFailure(f"Scene {scene_index}のroot Node参照が不正です")
            visit(root)
    if len(visited) != len(nodes):
        raise BakeFailure("Sceneから到達できないNodeがあります")


def _material_texture_infos(material: dict):
    pbr = material.get("pbrMetallicRoughness", {})
    for name in ("baseColorTexture", "metallicRoughnessTexture"):
        info = pbr.get(name)
        if isinstance(info, dict):
            yield f"pbrMetallicRoughness.{name}", info
    for name in ("normalTexture", "occlusionTexture", "emissiveTexture"):
        info = material.get(name)
        if isinstance(info, dict):
            yield name, info
    for extension_name, extension in material.get("extensions", {}).items():
        if not isinstance(extension, dict):
            continue
        for name, info in extension.items():
            if name.endswith("Texture") and isinstance(info, dict):
                yield f"extensions.{extension_name}.{name}", info


def _texture_transform_signature(info: dict, material_index: int, slot_name: str) -> tuple:
    tex_coord = info.get("texCoord", 0)
    if not isinstance(tex_coord, int) or tex_coord < 0:
        raise BakeFailure(f"Material {material_index}の{slot_name}.texCoordが不正です")
    transform = info.get("extensions", {}).get("KHR_texture_transform", {})
    if not isinstance(transform, dict):
        raise BakeFailure(f"Material {material_index}の{slot_name}.KHR_texture_transformが不正です")

    def vector(name: str, default: tuple[float, float]) -> tuple[float, float]:
        value = transform.get(name, list(default))
        if not isinstance(value, list) or len(value) != 2:
            raise BakeFailure(f"Material {material_index}の{slot_name}.{name}が不正です")
        result = tuple(float(component) for component in value)
        if not all(math.isfinite(component) for component in result):
            raise BakeFailure(f"Material {material_index}の{slot_name}.{name}に非有限値があります")
        return result

    offset = vector("offset", (0.0, 0.0))
    scale = vector("scale", (1.0, 1.0))
    rotation = float(transform.get("rotation", 0.0))
    if not math.isfinite(rotation):
        raise BakeFailure(f"Material {material_index}の{slot_name}.rotationに非有限値があります")
    override = transform.get("texCoord", tex_coord)
    if not isinstance(override, int) or override < 0:
        raise BakeFailure(f"Material {material_index}の{slot_name}.texCoord overrideが不正です")
    return override, offset, rotation, scale


def _validate_factor(value, length: int, minimum: float, maximum: float, label: str) -> None:
    values = value if isinstance(value, list) else [value]
    if len(values) != length:
        raise BakeFailure(f"{label}の要素数が不正です")
    for component in values:
        number = float(component)
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise BakeFailure(f"{label}が有効範囲外です")


def validate_glb(path: Path, expected_meshes: int, expected_materials: int) -> ParsedGlb:
    """独自writerを持たず、標準exporterの成果物だけを構造検証する。"""

    parsed = parse_glb(path)
    document = parsed.document
    asset = document.get("asset", {})
    if not str(asset.get("version", "")).startswith("2"):
        raise BakeFailure("glTF asset versionが2.0ではありません")
    if len(document.get("meshes", [])) != expected_meshes:
        raise BakeFailure("GLBのMesh数が選択Object数と一致しません")
    material_count = len(document.get("materials", []))
    # 標準exporterは同値材質の統合や未参照slotの省略を行えるため、
    # 使用slot数との完全一致ではなく上限とprimitive参照を検証する。
    if material_count == 0 or material_count > expected_materials:
        raise BakeFailure(
            f"GLBのMaterial数が不正です: actual={material_count}, maximum={expected_materials}"
        )
    for forbidden in ("animations", "cameras", "skins"):
        if document.get(forbidden):
            raise BakeFailure(f"GLBに禁止要素{forbidden}が含まれています")
    root_extensions = document.get("extensions", {})
    if "KHR_lights_punctual" in root_extensions:
        raise BakeFailure("GLBにLightが含まれています")
    if "KHR_draco_mesh_compression" in document.get("extensionsUsed", []):
        raise BakeFailure("GLBにDraco圧縮が含まれています")

    _validate_node_hierarchy(document)

    for mesh_index, mesh in enumerate(document.get("meshes", [])):
        primitives = mesh.get("primitives", [])
        if not primitives:
            raise BakeFailure(f"Mesh {mesh_index}にprimitiveがありません")
        for primitive_index, primitive in enumerate(primitives):
            attributes = primitive.get("attributes", {})
            # glTF 2.0ではTANGENTは任意であり、無い場合は受信側が
            # POSITION/NORMAL/TEXCOORDからMikkTSpaceを再生成できる。
            required = {"POSITION", "NORMAL", "TEXCOORD_0"}
            missing = required.difference(attributes)
            if missing:
                raise BakeFailure(f"Mesh {mesh_index}/{primitive_index}のAttributeが不足しています: {sorted(missing)}")
            if "TEXCOORD_1" in attributes:
                raise BakeFailure("Bake UV以外のUVがGLBに含まれています")
            if primitive.get("targets"):
                raise BakeFailure("GLBにMorph targetが含まれています")
            if "KHR_draco_mesh_compression" in primitive.get("extensions", {}):
                raise BakeFailure("primitiveにDraco圧縮が含まれています")
            material_index = primitive.get("material")
            if not isinstance(material_index, int) or not 0 <= material_index < material_count:
                raise BakeFailure(f"Mesh {mesh_index}/{primitive_index}のMaterial参照が不正です")

    for material_index, material in enumerate(document.get("materials", [])):
        pbr = material.get("pbrMetallicRoughness", {})
        extensions = material.get("extensions", {})
        is_unlit = "KHR_materials_unlit" in extensions
        if "baseColorTexture" not in pbr or (not is_unlit and "metallicRoughnessTexture" not in pbr):
            raise BakeFailure(f"Material {material_index}のBase/ORM textureが不足しています")
        if not is_unlit and ("normalTexture" not in material or "emissiveTexture" not in material):
            raise BakeFailure(f"Material {material_index}のNormal/Emissive textureが不足しています")
        alpha_mode = material.get("alphaMode", "OPAQUE")
        if alpha_mode not in {"OPAQUE", "MASK", "BLEND"}:
            raise BakeFailure(f"Material {material_index}のalphaModeが不正です: {alpha_mode}")
        if alpha_mode == "MASK":
            cutoff = float(material.get("alphaCutoff", 0.5))
            if not math.isfinite(cutoff) or not 0.0 <= cutoff <= 1.0:
                raise BakeFailure(f"Material {material_index}のalphaCutoffが不正です")

        _validate_factor(pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0]), 4, 0.0, 1.0, f"Material {material_index}のbaseColorFactor")
        _validate_factor(pbr.get("metallicFactor", 1.0), 1, 0.0, 1.0, f"Material {material_index}のmetallicFactor")
        _validate_factor(pbr.get("roughnessFactor", 1.0), 1, 0.0, 1.0, f"Material {material_index}のroughnessFactor")
        _validate_factor(material.get("emissiveFactor", [0.0, 0.0, 0.0]), 3, 0.0, 1.0, f"Material {material_index}のemissiveFactor")
        ior = extensions.get("KHR_materials_ior", {}).get("ior", 1.5)
        if not isinstance(ior, (int, float)) or not math.isfinite(float(ior)) or float(ior) < 1.0:
            raise BakeFailure(f"Material {material_index}のIORが不正です")

        signatures = set()
        for slot_name, info in _material_texture_infos(material):
            texture_index = info.get("index")
            if not isinstance(texture_index, int) or not 0 <= texture_index < len(document.get("textures", [])):
                raise BakeFailure(f"Material {material_index}の{slot_name} texture参照が不正です")
            signature = _texture_transform_signature(info, material_index, slot_name)
            if signature[0] != 0:
                raise BakeFailure(f"Material {material_index}の{slot_name}がBake UV以外を参照しています")
            signatures.add(signature)
        if len(signatures) > 1:
            raise BakeFailure(f"Material {material_index}内でtexture slotのUV set/transformが統一されていません")

        required_extension_textures = {
            "KHR_materials_transmission": ("transmissionTexture",),
            "KHR_materials_specular": ("specularTexture", "specularColorTexture"),
            "KHR_materials_clearcoat": ("clearcoatTexture", "clearcoatRoughnessTexture", "clearcoatNormalTexture"),
            "KHR_materials_sheen": ("sheenColorTexture", "sheenRoughnessTexture"),
            "KHR_materials_anisotropy": ("anisotropyTexture",),
            "KHR_materials_volume": ("thicknessTexture",),
        }
        for extension_name, texture_names in required_extension_textures.items():
            extension = extensions.get(extension_name)
            if extension is None:
                continue
            if not any(name in extension for name in texture_names):
                raise BakeFailure(f"Material {material_index}の{extension_name} textureが不足しています")

    _validate_png_images(parsed)
    return parsed


def export_to_temporary_glb(
    context: bpy.types.Context,
    scene: bpy.types.Scene,
    work_objects: list[WorkObject],
    hierarchy_objects: list[bpy.types.Object],
    final_path: Path,
) -> PendingGlb:
    """最終パスを変更せず、同一ディレクトリの一時GLBへ書き出す。"""

    final_path = final_path.expanduser().resolve()
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = final_path.with_name(f".{final_path.stem}.{uuid.uuid4().hex}.tmp.glb")
    view_layer = scene.view_layers[0]
    selected = list(dict.fromkeys(hierarchy_objects))
    active = work_objects[0].object
    for obj in scene.objects:
        obj.select_set(obj in selected)
    view_layer.objects.active = active
    override = dict(
        scene=scene,
        view_layer=view_layer,
        active_object=active,
        object=active,
        selected_objects=selected,
        selected_editable_objects=selected,
    )
    try:
        with context.temp_override(**override):
            result = bpy.ops.export_scene.gltf(
                filepath=str(temporary),
                export_format="GLB",
                use_selection=True,
                export_materials="EXPORT",
                export_texcoords=True,
                export_normals=True,
                export_tangents=True,
                export_animations=False,
                export_skins=False,
                export_morph=False,
                export_morph_normal=False,
                export_morph_tangent=False,
                export_cameras=False,
                export_lights=False,
                export_draco_mesh_compression_enable=False,
                export_use_gltfpack=False,
                export_yup=True,
                export_extras=False,
                export_attributes=False,
                export_image_format="AUTO",
            )
        if "FINISHED" not in result or not temporary.is_file():
            raise BakeFailure("Blender標準glTF exporterがGLBを生成しませんでした")
        return PendingGlb(
            temporary,
            final_path,
            len(work_objects),
            sum(len(work.slots) for work in work_objects),
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def validate_and_commit(pending: PendingGlb) -> ParsedGlb:
    """全検証成功後だけ既存GLBを原子的に置換する。"""

    try:
        parsed = validate_glb(pending.temporary_path, pending.expected_meshes, pending.expected_materials)
        os.replace(pending.temporary_path, pending.final_path)
        return parsed
    except Exception:
        pending.temporary_path.unlink(missing_ok=True)
        raise
