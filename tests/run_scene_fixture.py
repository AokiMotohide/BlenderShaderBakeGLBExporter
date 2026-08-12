"""Blenderファイル上の全Meshを使う任意fixture受入runner。"""

from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
import sys

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "addon"))

import shader_bake_glb_exporter as addon  # noqa: E402
from shader_bake_glb_exporter.glb_export import parse_glb  # noqa: E402
from shader_bake_glb_exporter.job import BakeJob, BakeJobConfig, JobStatus  # noqa: E402


def matrix_values(matrix) -> tuple[float, ...]:
    return tuple(float(matrix[row][column]) for row in range(4) for column in range(4))


def matrices_close(left: tuple[float, ...], right: tuple[float, ...], tolerance: float = 2.0e-4) -> bool:
    return len(left) == len(right) and all(abs(a - b) <= tolerance for a, b in zip(left, right))


def texture_infos(material: dict):
    pbr = material.get("pbrMetallicRoughness", {})
    for name in ("baseColorTexture", "metallicRoughnessTexture"):
        if isinstance(pbr.get(name), dict):
            yield pbr[name]
    for name in ("normalTexture", "occlusionTexture", "emissiveTexture"):
        if isinstance(material.get(name), dict):
            yield material[name]
    for extension in material.get("extensions", {}).values():
        if not isinstance(extension, dict):
            continue
        yield from (info for name, info in extension.items() if name.endswith("Texture") and isinstance(info, dict))


def texture_contract(info: dict) -> tuple:
    transform = info.get("extensions", {}).get("KHR_texture_transform", {})
    return (
        int(transform.get("texCoord", info.get("texCoord", 0))),
        tuple(float(value) for value in transform.get("offset", [0.0, 0.0])),
        float(transform.get("rotation", 0.0)),
        tuple(float(value) for value in transform.get("scale", [1.0, 1.0])),
    )


def run() -> int:
    output_text = os.environ.get("SHADER_BAKE_GLB_FIXTURE_OUTPUT", "")
    if not output_text:
        print("FIXTURE_ERROR: SHADER_BAKE_GLB_FIXTURE_OUTPUT is required")
        return 2
    resolution = int(os.environ.get("SHADER_BAKE_GLB_FIXTURE_RESOLUTION", "512"))
    output = Path(output_text).expanduser().resolve()
    if not hasattr(bpy.types.WindowManager, "shader_bake_glb"):
        addon.register()

    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH" and obj.data.polygons]
    expected_world = {f"{obj.name}__BAKED": matrix_values(obj.matrix_world) for obj in meshes}
    selected_pointers = {obj.as_pointer() for obj in meshes}
    expected_parent = {}
    for obj in meshes:
        if obj.parent is None:
            expected_parent[f"{obj.name}__BAKED"] = None
        elif obj.parent.as_pointer() in selected_pointers:
            expected_parent[f"{obj.name}__BAKED"] = f"{obj.parent.name}__BAKED"
        else:
            expected_parent[f"{obj.name}__BAKED"] = f"{obj.parent.name}__BAKED_HIERARCHY"
    for obj in bpy.context.scene.objects:
        obj.select_set(obj in meshes)
    bpy.context.view_layer.objects.active = meshes[0] if meshes else None

    job = BakeJob(bpy.context, BakeJobConfig(output, resolution), meshes)
    status = job.run_to_completion()
    print(f"FIXTURE_JOB: status={status.value} meshes={len(meshes)} warnings={len(job.warnings)} errors={len(job.errors)}")
    for warning in job.warnings:
        print(f"FIXTURE_WARNING: {warning}")
    for error in job.errors:
        print(f"FIXTURE_ERROR: {error}")
    if status != JobStatus.SUCCEEDED or not output.is_file():
        return 1

    parsed = parse_glb(output)
    document = parsed.document
    alpha = Counter(material.get("alphaMode", "OPAQUE") for material in document.get("materials", []))
    material_contract_counts = Counter(
        len({texture_contract(info) for info in texture_infos(material)})
        for material in document.get("materials", [])
    )
    uv1_primitives = sum(
        "TEXCOORD_1" in primitive.get("attributes", {})
        for mesh in document.get("meshes", [])
        for primitive in mesh.get("primitives", [])
    )
    transform_materials = sum(
        any("KHR_texture_transform" in info.get("extensions", {}) for info in texture_infos(material))
        for material in document.get("materials", [])
    )
    print(
        "FIXTURE_GLB: "
        f"nodes={len(document.get('nodes', []))} meshes={len(document.get('meshes', []))} "
        f"materials={len(document.get('materials', []))} uv1Primitives={uv1_primitives} "
        f"textureTransformMaterials={transform_materials} alpha={dict(alpha)} "
        f"materialTextureContractCounts={dict(material_contract_counts)}"
    )

    before_import = {obj.as_pointer() for obj in bpy.data.objects}
    bpy.ops.import_scene.gltf(filepath=str(output))
    imported = {
        obj.name: obj
        for obj in bpy.data.objects
        if obj.as_pointer() not in before_import and obj.name in expected_world
    }
    transform_mismatches = [
        name
        for name, expected in expected_world.items()
        if name not in imported or not matrices_close(matrix_values(imported[name].matrix_world), expected)
    ]
    hierarchy_mismatches = [
        name
        for name, obj in imported.items()
        if (obj.parent.name if obj.parent else None) != expected_parent[name]
    ]
    print(
        "FIXTURE_ROUNDTRIP: "
        f"importedMeshes={len(imported)} transformMismatches={len(transform_mismatches)} "
        f"hierarchyMismatches={len(hierarchy_mismatches)}"
    )
    if transform_mismatches:
        print("FIXTURE_TRANSFORM_MISMATCH: " + ", ".join(transform_mismatches[:20]))
    if hierarchy_mismatches:
        print("FIXTURE_HIERARCHY_MISMATCH: " + ", ".join(hierarchy_mismatches[:20]))
    if any(count > 1 for count in material_contract_counts) or transform_mismatches or hierarchy_mismatches:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
