"""Blender 5.1.1 background modeで実行する統合テスト。"""

from __future__ import annotations

from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import traceback

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "addon"))

import shader_bake_glb_exporter as addon  # noqa: E402
from shader_bake_glb_exporter.glb_export import parse_glb  # noqa: E402
from shader_bake_glb_exporter.job import BakeJob, BakeJobConfig, JobStatus, preflight  # noqa: E402


class TestResults:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed.append(name)
            print(f"PASS: {name}")
        else:
            self.failed.append((name, detail or "条件が成立しません"))
            print(f"FAIL: {name}: {detail}")

    def guarded(self, name: str, action) -> None:
        try:
            action()
            self.check(name, True)
        except Exception as exc:
            self.failed.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"FAIL: {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()


def reset_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if not hasattr(bpy.types.WindowManager, "shader_bake_glb"):
        addon.register()


def make_principled_material(name: str) -> tuple[bpy.types.Material, bpy.types.Node, bpy.types.Node]:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
    tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    return material, principled, output


def make_cube(name: str, material: bpy.types.Material, location=(0.0, 0.0, 0.0)) -> bpy.types.Object:
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.data.materials.append(material)
    return obj


def select_only(*objects: bpy.types.Object) -> None:
    for obj in bpy.context.scene.objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0] if objects else None


def node_signature(material: bpy.types.Material) -> tuple:
    nodes = []
    for node in material.node_tree.nodes:
        defaults = []
        for socket in node.inputs:
            if socket.is_linked:
                continue
            value = getattr(socket, "default_value", None)
            try:
                normalized = tuple(round(float(component), 7) for component in value)
            except TypeError:
                normalized = round(float(value), 7) if isinstance(value, (int, float)) else str(value)
            defaults.append((socket.name, normalized))
        nodes.append((node.name, node.bl_idname, getattr(node, "operation", ""), tuple(defaults)))
    links = sorted((link.from_node.name, link.from_socket.name, link.to_node.name, link.to_socket.name) for link in material.node_tree.links)
    return tuple(sorted(nodes)), tuple(links)


def uv_signature(mesh: bpy.types.Mesh) -> tuple:
    layers = []
    for layer in mesh.uv_layers:
        coords = tuple((round(item.uv.x, 7), round(item.uv.y, 7)) for item in layer.data)
        layers.append((layer.name, layer.active_render, coords))
    return tuple(layers)


def modifier_signature(obj: bpy.types.Object) -> tuple:
    return tuple((modifier.name, modifier.type, bool(modifier.show_viewport), bool(modifier.show_render)) for modifier in obj.modifiers)


def make_comprehensive_scene() -> tuple[bpy.types.Object, bpy.types.Object, bpy.types.Material]:
    material, principled, _ = make_principled_material("ComprehensiveMaterial")
    tree = material.node_tree
    texcoord = tree.nodes.new("ShaderNodeTexCoord")
    mapping = tree.nodes.new("ShaderNodeMapping")
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.noise_dimensions = "3D"
    noise.inputs["Scale"].default_value = 2.75
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.05, 0.2, 0.8, 1.0)
    ramp.color_ramp.elements[1].color = (0.9, 0.25, 0.05, 1.0)
    tree.links.new(texcoord.outputs["Generated"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
    tree.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    tree.links.new(ramp.outputs["Color"], principled.inputs["Base Color"])

    metallic = tree.nodes.new("ShaderNodeValue")
    metallic.outputs[0].default_value = 0.75
    tree.links.new(metallic.outputs[0], principled.inputs["Metallic"])
    roughness = tree.nodes.new("ShaderNodeMath")
    roughness.operation = "MULTIPLY_ADD"
    roughness.inputs[1].default_value = 0.5
    roughness.inputs[2].default_value = 0.2
    tree.links.new(noise.outputs["Fac"], roughness.inputs[0])
    tree.links.new(roughness.outputs[0], principled.inputs["Roughness"])

    normal_image = bpy.data.images.new("SourceNormal", 4, 4, alpha=True, float_buffer=False)
    normal_image.colorspace_settings.name = "Non-Color"
    normal_pixels = array("f", [0.5, 0.5, 1.0, 1.0]) * 16
    normal_image.pixels.foreach_set(normal_pixels)
    normal_tex = tree.nodes.new("ShaderNodeTexImage")
    normal_tex.image = normal_image
    normal_tex.interpolation = "Closest"
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    tree.links.new(normal_tex.outputs["Color"], normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])

    emission = tree.nodes.new("ShaderNodeRGB")
    emission.outputs[0].default_value = (0.8, 0.4, 0.2, 1.0)
    tree.links.new(emission.outputs[0], principled.inputs["Emission Color"])
    principled.inputs["Emission Strength"].default_value = 3.0

    alpha_clip = tree.nodes.new("ShaderNodeMath")
    alpha_clip.operation = "GREATER_THAN"
    alpha_clip.inputs[1].default_value = 0.42
    tree.links.new(noise.outputs["Fac"], alpha_clip.inputs[0])
    tree.links.new(alpha_clip.outputs[0], principled.inputs["Alpha"])

    transmission = tree.nodes.new("ShaderNodeValue")
    transmission.outputs[0].default_value = 0.25
    tree.links.new(transmission.outputs[0], principled.inputs["Transmission Weight"])
    principled.inputs["IOR"].default_value = 1.33

    selected = make_cube("SelectedBakedCube", material)
    modifier = selected.modifiers.new("PreservedBevel", "BEVEL")
    modifier.width = 0.05
    modifier.segments = 2
    unselected = make_cube("UnselectedCube", material, location=(3.0, 0.0, 0.0))
    select_only(selected)
    return selected, unselected, material


def image_payload(parsed, image_index: int) -> bytes:
    image = parsed.document["images"][image_index]
    view = parsed.document["bufferViews"][image["bufferView"]]
    start = int(view.get("byteOffset", 0))
    end = start + int(view["byteLength"])
    return parsed.binary[start:end]


def texture_image_index(document: dict, texture_index: int) -> int:
    source = document["textures"][texture_index].get("source")
    if source is None:
        extension = document["textures"][texture_index].get("extensions", {}).get("EXT_texture_webp", {})
        source = extension.get("source")
    return int(source)


def load_embedded_image(parsed, image_index: int, folder: Path, colorspace: str) -> bpy.types.Image:
    path = folder / f"image_{image_index}.png"
    path.write_bytes(image_payload(parsed, image_index))
    image = bpy.data.images.load(str(path), check_existing=False)
    image.colorspace_settings.name = colorspace
    return image


def image_pixels(image: bpy.types.Image) -> array:
    values = array("f", [0.0]) * (image.size[0] * image.size[1] * 4)
    image.pixels.foreach_get(values)
    return values


def has_temp_data() -> bool:
    prefixes = ("__SHADER_BAKE_GLB",)
    collections = (bpy.data.scenes, bpy.data.collections, bpy.data.objects, bpy.data.meshes, bpy.data.materials, bpy.data.images)
    return any(block.name.startswith(prefixes) for collection in collections for block in collection)


def run() -> TestResults:
    results = TestResults()
    reset_scene()
    temp_root = Path(tempfile.mkdtemp(prefix="shader_bake_glb_tests_"))
    output = temp_root / "comprehensive.glb"
    selected, unselected, source_material = make_comprehensive_scene()

    before_nodes = node_signature(source_material)
    before_uv = uv_signature(selected.data)
    before_modifiers = modifier_signature(selected)
    before_selection = tuple(sorted(obj.name for obj in bpy.context.selected_objects))
    before_active = bpy.context.view_layer.objects.active.name
    before_mode = bpy.context.mode

    job = BakeJob(bpy.context, BakeJobConfig(output, 1024))
    status = job.run_to_completion()
    results.check("包括GLB生成", status == JobStatus.SUCCEEDED and output.is_file(), "; ".join(str(error) for error in job.errors))

    parsed = parse_glb(output) if output.is_file() else None
    document = parsed.document if parsed else {}
    node_names = {node.get("name", "") for node in document.get("nodes", [])}
    results.check(
        "2. 選択MeshだけがGLBへ含まれる",
        len(document.get("meshes", [])) == 1 and "SelectedBakedCube__BAKED" in node_names,
    )
    results.check("3. 選択されていないMeshは含まれない", "UnselectedCube" not in node_names)

    materials = document.get("materials", [])
    pbr = materials[0].get("pbrMetallicRoughness", {}) if materials else {}
    extensions = materials[0].get("extensions", {}) if materials else {}
    results.check("4. Noise TextureとColorRampをBase Colorへベイクできる", "baseColorTexture" in pbr)
    results.check("5. Math NodeをRoughnessへベイクできる", "metallicRoughnessTexture" in pbr)

    if parsed and materials:
        orm_image_index = texture_image_index(document, pbr["metallicRoughnessTexture"]["index"])
        orm_image = load_embedded_image(parsed, orm_image_index, temp_root, "Non-Color")
        orm_values = image_pixels(orm_image)
        valid_metallic = [orm_values[index + 2] for index in range(0, len(orm_values), 4) if orm_values[index + 2] > 0.5]
        valid_roughness = [orm_values[index + 1] for index in range(0, len(orm_values), 4) if orm_values[index + 2] > 0.5]
        results.check("6. MetallicをBチャンネルへ格納する", bool(valid_metallic) and abs(sum(valid_metallic) / len(valid_metallic) - 0.75) < 0.03)
        results.check("7. RoughnessをGチャンネルへ格納する", bool(valid_roughness) and max(valid_roughness) - min(valid_roughness) > 0.05)
        bpy.data.images.remove(orm_image)

        normal_image_index = texture_image_index(document, materials[0]["normalTexture"]["index"])
        normal_image = load_embedded_image(parsed, normal_image_index, temp_root, "Non-Color")
        normal_values = image_pixels(normal_image)
        normal_samples = [
            (normal_values[index], normal_values[index + 1], normal_values[index + 2])
            for index in range(0, len(normal_values), 4)
            if normal_values[index + 2] > 0.8
        ]
        results.check("8. Tangent-space Normal Mapを出力する", bool(normal_samples) and all(sample[2] > 0.8 for sample in normal_samples[:100]))
        bpy.data.images.remove(normal_image)

        emissive_ext = extensions.get("KHR_materials_emissive_strength", {})
        results.check("9. Emissionを正規化してStrengthを分離する", float(emissive_ext.get("emissiveStrength", 0.0)) > 1.0)
        results.check("10. Alpha Clipを維持する", materials[0].get("alphaMode") == "MASK" and abs(float(materials[0].get("alphaCutoff", 0.0)) - 0.42) < 1.0e-4)
        transmission_ext = extensions.get("KHR_materials_transmission", {})
        results.check("11. Transmission Weightを出力する", "transmissionTexture" in transmission_ext)
        ior_ext = extensions.get("KHR_materials_ior", {})
        results.check("12. IOR定数を維持する", abs(float(ior_ext.get("ior", 0.0)) - 1.33) < 1.0e-4)
    else:
        for number, label in ((6, "Metallic"), (7, "Roughness"), (8, "Normal"), (9, "Emission"), (10, "Alpha Clip"), (11, "Transmission"), (12, "IOR")):
            results.check(f"{number}. {label}", False, "包括GLBがありません")

    results.check("15. 元MaterialのNode構成が変化しない", node_signature(source_material) == before_nodes)
    results.check("16. 元MeshのUVが変化しない", uv_signature(selected.data) == before_uv)
    results.check("17. 元ObjectのModifierが変化しない", modifier_signature(selected) == before_modifiers)
    results.check("元の選択、Active Object、Modeを復元する", tuple(sorted(obj.name for obj in bpy.context.selected_objects)) == before_selection and bpy.context.view_layer.objects.active.name == before_active and bpy.context.mode == before_mode)
    results.check("成功時に一時DataBlockが残らない", not has_temp_data())

    # 開始前検証。
    select_only()
    _, empty_errors, _ = preflight(bpy.context, BakeJobConfig(temp_root / "empty.glb", 512))
    results.check("1. 選択Meshが0件なら失敗する", any("選択Meshが0件" in error.reason for error in empty_errors))
    select_only(selected)

    mix_material = bpy.data.materials.new("MixRejected")
    mix_material.use_nodes = True
    mix_tree = mix_material.node_tree
    mix_tree.nodes.clear()
    mix_output = mix_tree.nodes.new("ShaderNodeOutputMaterial")
    mix = mix_tree.nodes.new("ShaderNodeMixShader")
    mix_tree.links.new(mix.outputs[0], mix_output.inputs["Surface"])
    mix_obj = make_cube("MixObject", mix_material, (6.0, 0.0, 0.0))
    select_only(mix_obj)
    _, mix_errors, _ = preflight(bpy.context, BakeJobConfig(temp_root / "mix.glb", 512))
    results.check("13. Mix Shaderを未対応として拒否する", bool(mix_errors))

    displacement_material, _, displacement_output = make_principled_material("DisplacementRejected")
    displacement_value = displacement_material.node_tree.nodes.new("ShaderNodeValue")
    displacement_material.node_tree.links.new(displacement_value.outputs[0], displacement_output.inputs["Displacement"])
    displacement_obj = make_cube("DisplacementObject", displacement_material, (9.0, 0.0, 0.0))
    select_only(displacement_obj)
    _, displacement_errors, _ = preflight(bpy.context, BakeJobConfig(temp_root / "displacement.glb", 512))
    results.check("14. Shader Displacementを拒否する", any("Displacement" in error.reason for error in displacement_errors))

    grouped_material, grouped_principled, _ = make_principled_material("GroupedGenerated")
    group_tree = bpy.data.node_groups.new("GeneratedColorGroup", "ShaderNodeTree")
    group_tree.interface.new_socket(name="Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    group_output = group_tree.nodes.new("NodeGroupOutput")
    group_texcoord = group_tree.nodes.new("ShaderNodeTexCoord")
    group_noise = group_tree.nodes.new("ShaderNodeTexNoise")
    group_tree.links.new(group_texcoord.outputs["Generated"], group_noise.inputs["Vector"])
    group_tree.links.new(group_noise.outputs["Color"], group_output.inputs["Color"])
    group_node = grouped_material.node_tree.nodes.new("ShaderNodeGroup")
    group_node.node_tree = group_tree
    grouped_material.node_tree.links.new(group_node.outputs["Color"], grouped_principled.inputs["Base Color"])
    grouped_obj = make_cube("GroupedObject", grouped_material, (15.0, 0.0, 0.0))
    select_only(grouped_obj)
    _, grouped_errors, _ = preflight(bpy.context, BakeJobConfig(temp_root / "grouped.glb", 512))
    results.check("Node Group内のGenerated座標を検証できる", not grouped_errors, "; ".join(str(error) for error in grouped_errors))

    blend_material, _, _ = make_principled_material("BlendRejected")
    blend_material.surface_render_method = "BLENDED"
    blend_obj = make_cube("BlendObject", blend_material, (18.0, 0.0, 0.0))
    select_only(blend_obj)
    _, blend_errors, _ = preflight(bpy.context, BakeJobConfig(temp_root / "blend.glb", 512))
    results.check("Alpha Blendを拒否する", any("Alpha Blend" in error.reason for error in blend_errors))

    # 失敗とキャンセルは定数材質で高速に検証する。
    constant_material, constant_principled, _ = make_principled_material("ConstantMaterial")
    constant_principled.inputs["Base Color"].default_value = (0.2, 0.4, 0.8, 1.0)
    constant_obj = make_cube("ConstantObject", constant_material, (12.0, 0.0, 0.0))
    select_only(constant_obj)
    failure_output = temp_root / "failure.glb"
    failure_job = BakeJob(bpy.context, BakeJobConfig(failure_output, 512, fail_after_phase="UV生成"))
    failure_status = failure_job.run_to_completion()
    results.check("18. 失敗時に一時データが残らない", failure_status == JobStatus.FAILED and not failure_output.exists() and not has_temp_data())

    cancel_output = temp_root / "cancel.glb"
    cancel_job = BakeJob(bpy.context, BakeJobConfig(cancel_output, 512))
    cancel_job.start()
    cancel_job.request_cancel()
    cancel_status = cancel_job.advance()
    results.check("19. キャンセル時に一時データが残らない", cancel_status == JobStatus.CANCELLED and not cancel_output.exists() and not has_temp_data())

    if output.is_file():
        sentinel = temp_root / "sentinel.glb"
        shutil.copy2(output, sentinel)
        before_hash = hashlib.sha256(sentinel.read_bytes()).hexdigest()
        select_only(constant_obj)
        replace_job = BakeJob(bpy.context, BakeJobConfig(sentinel, 512, fail_after_phase="GLB出力"))
        replace_status = replace_job.run_to_completion()
        after_hash = hashlib.sha256(sentinel.read_bytes()).hexdigest()
        results.check("22. 書き出し中に既存の正常なGLBを破壊しない", replace_status == JobStatus.FAILED and before_hash == after_hash and not has_temp_data())

    if parsed:
        png_ok = True
        for image_index, _ in enumerate(document.get("images", [])):
            payload = image_payload(parsed, image_index)
            width, height, bit_depth = struct.unpack_from(">IIB", payload, 16)
            png_ok = png_ok and width == 1024 and height == 1024 and bit_depth == 8
        results.check("21. 出力画像が1024×1024の8bit PNGで有限値を持つ", png_ok)

        before_objects = {obj.as_pointer() for obj in bpy.data.objects}
        before_materials = {material.as_pointer() for material in bpy.data.materials}
        before_images = {image.as_pointer() for image in bpy.data.images}
        bpy.ops.import_scene.gltf(filepath=str(output))
        imported_objects = [obj for obj in bpy.data.objects if obj.as_pointer() not in before_objects]
        imported_materials = [material for material in bpy.data.materials if material.as_pointer() not in before_materials]
        imported_images = [image for image in bpy.data.images if image.as_pointer() not in before_images]
        results.check("20. GLB再importでMesh、Material、Imageが存在する", any(obj.type == "MESH" for obj in imported_objects) and bool(imported_materials) and bool(imported_images))
    else:
        results.check("20. GLB再importでMesh、Material、Imageが存在する", False, "包括GLBがありません")
        results.check("21. 出力画像が1024×1024の8bit PNGで有限値を持つ", False, "包括GLBがありません")

    forbidden = (
        "Projection" + "Mapping" + "Simulator",
        "Origin" + "Projection",
        "プロジェクション" + "マッピング",
        "シミュ" + "レータ",
    )
    public_ok = True
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "dist" in path.parts or path.suffix.lower() in {".pyc", ".zip"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        public_ok = public_ok and not any(word in text for word in forbidden)
    results.check("公開ファイルに特定用途の記述がない", public_ok)

    integration_output = os.environ.get("SHADER_BAKE_GLB_INTEGRATION_OUTPUT", "")
    if integration_output:
        destination = Path(integration_output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        select_only(constant_obj)
        integration_job = BakeJob(bpy.context, BakeJobConfig(destination, 1024))
        integration_status = integration_job.run_to_completion()
        results.check(
            "統合確認用Opaque GLBを生成する",
            integration_status == JobStatus.SUCCEEDED and destination.is_file(),
            "; ".join(str(error) for error in integration_job.errors),
        )
        if destination.is_file():
            print(f"INTEGRATION_GLB: {destination}")

    shutil.rmtree(temp_root, ignore_errors=True)
    return results


if __name__ == "__main__":
    test_results = run()
    print(f"RESULT: passed={len(test_results.passed)} failed={len(test_results.failed)}")
    for name, detail in test_results.failed:
        print(f"FAILED_TEST: {name}: {detail}")
    if test_results.failed:
        raise SystemExit(1)
