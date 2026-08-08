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


def validate_glb(path: Path, expected_meshes: int, expected_materials: int) -> ParsedGlb:
    """独自writerを持たず、標準exporterの成果物だけを構造検証する。"""

    parsed = parse_glb(path)
    document = parsed.document
    asset = document.get("asset", {})
    if not str(asset.get("version", "")).startswith("2"):
        raise BakeFailure("glTF asset versionが2.0ではありません")
    if len(document.get("meshes", [])) != expected_meshes:
        raise BakeFailure("GLBのMesh数が選択Object数と一致しません")
    if len(document.get("materials", [])) != expected_materials:
        raise BakeFailure("GLBのMaterial数が使用Material Slot数と一致しません")
    for forbidden in ("animations", "cameras", "skins"):
        if document.get(forbidden):
            raise BakeFailure(f"GLBに禁止要素{forbidden}が含まれています")
    root_extensions = document.get("extensions", {})
    if "KHR_lights_punctual" in root_extensions:
        raise BakeFailure("GLBにLightが含まれています")
    if "KHR_draco_mesh_compression" in document.get("extensionsUsed", []):
        raise BakeFailure("GLBにDraco圧縮が含まれています")

    for mesh_index, mesh in enumerate(document.get("meshes", [])):
        primitives = mesh.get("primitives", [])
        if not primitives:
            raise BakeFailure(f"Mesh {mesh_index}にprimitiveがありません")
        for primitive_index, primitive in enumerate(primitives):
            attributes = primitive.get("attributes", {})
            required = {"POSITION", "NORMAL", "TANGENT", "TEXCOORD_0"}
            missing = required.difference(attributes)
            if missing:
                raise BakeFailure(f"Mesh {mesh_index}/{primitive_index}のAttributeが不足しています: {sorted(missing)}")
            if "TEXCOORD_1" in attributes:
                raise BakeFailure("Bake UV以外のUVがGLBに含まれています")
            if primitive.get("targets"):
                raise BakeFailure("GLBにMorph targetが含まれています")
            if "KHR_draco_mesh_compression" in primitive.get("extensions", {}):
                raise BakeFailure("primitiveにDraco圧縮が含まれています")

    for material_index, material in enumerate(document.get("materials", [])):
        pbr = material.get("pbrMetallicRoughness", {})
        if "baseColorTexture" not in pbr or "metallicRoughnessTexture" not in pbr:
            raise BakeFailure(f"Material {material_index}のBase/ORM textureが不足しています")
        if "normalTexture" not in material or "emissiveTexture" not in material:
            raise BakeFailure(f"Material {material_index}のNormal/Emissive textureが不足しています")
        transmission = material.get("extensions", {}).get("KHR_materials_transmission", {})
        if "transmissionTexture" not in transmission:
            raise BakeFailure(f"Material {material_index}のTransmission textureが不足しています")
        if material.get("alphaMode", "OPAQUE") not in {"OPAQUE", "MASK"}:
            raise BakeFailure(f"Material {material_index}がAlpha Blendです")

    _validate_png_images(parsed)
    return parsed


def export_to_temporary_glb(
    context: bpy.types.Context,
    scene: bpy.types.Scene,
    work_objects: list[WorkObject],
    final_path: Path,
) -> PendingGlb:
    """最終パスを変更せず、同一ディレクトリの一時GLBへ書き出す。"""

    final_path = final_path.expanduser().resolve()
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = final_path.with_name(f".{final_path.stem}.{uuid.uuid4().hex}.tmp.glb")
    view_layer = scene.view_layers[0]
    selected = [work.object for work in work_objects]
    for obj in scene.objects:
        obj.select_set(obj in selected)
    view_layer.objects.active = selected[0]
    override = dict(
        scene=scene,
        view_layer=view_layer,
        active_object=selected[0],
        object=selected[0],
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
