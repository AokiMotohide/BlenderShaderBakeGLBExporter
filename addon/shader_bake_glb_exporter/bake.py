"""元データを変更せず、作業コピーだけでPBRテクスチャを生成する。"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
import math
from typing import Iterable
import uuid

import bpy

from .material_validation import AlphaContract, MaterialAnalysis, analyze_material


BAKE_UV_NAME = "GLB_BAKE_UV"
CHANNELS = ("Base Color", "Metallic", "Roughness", "Normal", "Emissive", "Alpha", "Transmission")


class BakeFailure(RuntimeError):
    """Bake結果またはBlender Operatorの失敗。"""


class TempDataRegistry:
    """Jobが作成したDataBlockだけを所有し、全終了経路で削除する。"""

    def __init__(self) -> None:
        self.scenes: list[bpy.types.Scene] = []
        self.collections: list[bpy.types.Collection] = []
        self.objects: list[bpy.types.Object] = []
        self.meshes: list[bpy.types.Mesh] = []
        self.materials: list[bpy.types.Material] = []
        self.images: list[bpy.types.Image] = []

    def track(self, block):
        if isinstance(block, bpy.types.Scene):
            self.scenes.append(block)
        elif isinstance(block, bpy.types.Collection):
            self.collections.append(block)
        elif isinstance(block, bpy.types.Object):
            self.objects.append(block)
        elif isinstance(block, bpy.types.Mesh):
            self.meshes.append(block)
        elif isinstance(block, bpy.types.Material):
            self.materials.append(block)
        elif isinstance(block, bpy.types.Image):
            self.images.append(block)
        return block

    def _remove(self, blocks: list, collection, block) -> None:
        if block in blocks:
            blocks.remove(block)
        try:
            collection.remove(block, do_unlink=True)
        except (ReferenceError, RuntimeError):
            pass

    def remove_object(self, block: bpy.types.Object) -> None:
        self._remove(self.objects, bpy.data.objects, block)

    def remove_mesh(self, block: bpy.types.Mesh) -> None:
        self._remove(self.meshes, bpy.data.meshes, block)

    def remove_material(self, block: bpy.types.Material) -> None:
        self._remove(self.materials, bpy.data.materials, block)

    def remove_image(self, block: bpy.types.Image) -> None:
        self._remove(self.images, bpy.data.images, block)

    def remove_collection(self, block: bpy.types.Collection) -> None:
        self._remove(self.collections, bpy.data.collections, block)

    def remove_scene(self, block: bpy.types.Scene) -> None:
        self._remove(self.scenes, bpy.data.scenes, block)

    def cleanup(self) -> None:
        """依存順に削除する。既存DataBlockは登録されないため対象外になる。"""

        for block in list(reversed(self.objects)):
            self.remove_object(block)
        for block in list(reversed(self.collections)):
            self._remove(self.collections, bpy.data.collections, block)
        for block in list(reversed(self.scenes)):
            self._remove(self.scenes, bpy.data.scenes, block)
        for block in list(reversed(self.materials)):
            self.remove_material(block)
        for block in list(reversed(self.images)):
            self.remove_image(block)
        for block in list(reversed(self.meshes)):
            self.remove_mesh(block)


@dataclass
class RawChannel:
    name: str
    resolution: int
    image: bpy.types.Image | None = None
    constant: tuple[float, float, float, float] | None = None


@dataclass
class MaterialBakeResult:
    raw: dict[str, RawChannel] = field(default_factory=dict)
    images: dict[str, bpy.types.Image] = field(default_factory=dict)
    emission_strength: float = 1.0


@dataclass
class MaterialSlotWork:
    slot_index: int
    source_material: bpy.types.Material
    source_analysis: MaterialAnalysis
    result: MaterialBakeResult = field(default_factory=MaterialBakeResult)
    final_material: bpy.types.Material | None = None


@dataclass
class WorkObject:
    original: bpy.types.Object
    object: bpy.types.Object
    bake_uv_name: str
    slots: list[MaterialSlotWork]


def create_job_scene(
    scene: bpy.types.Scene,
    registry: TempDataRegistry,
) -> tuple[bpy.types.Scene, bpy.types.Collection]:
    """元Sceneへ一時Collectionだけを接続し、別SceneのDepsgraphを作らない。"""

    token = uuid.uuid4().hex[:8]
    collection = registry.track(bpy.data.collections.new(f"__SHADER_BAKE_GLB_COLLECTION_{token}"))
    scene.collection.children.link(collection)
    scene.render.engine = "CYCLES"
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_depth = "8"
    scene.render.image_settings.color_mode = "RGBA"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    return scene, collection


def used_material_indices(mesh: bpy.types.Mesh) -> list[int]:
    return sorted({polygon.material_index for polygon in mesh.polygons})


def _unique_uv_name(mesh: bpy.types.Mesh) -> str:
    if mesh.uv_layers.get(BAKE_UV_NAME) is None:
        return BAKE_UV_NAME
    index = 1
    while mesh.uv_layers.get(f"{BAKE_UV_NAME}_{index}") is not None:
        index += 1
    return f"{BAKE_UV_NAME}_{index}"


def create_work_object(
    context: bpy.types.Context,
    original: bpy.types.Object,
    scene: bpy.types.Scene,
    collection: bpy.types.Collection,
    registry: TempDataRegistry,
) -> WorkObject:
    """Modifier適用済みMeshとSlot固有Materialを作り、元DataBlockを共有しない。"""

    depsgraph = context.evaluated_depsgraph_get()
    evaluated = original.evaluated_get(depsgraph)
    mesh = registry.track(
        bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
    )
    if not mesh.polygons:
        raise BakeFailure(f"{original.name}: FaceがないMeshはベイクできません")
    for vertex in mesh.vertices:
        if not all(math.isfinite(float(component)) for component in vertex.co):
            raise BakeFailure(f"{original.name}: Mesh座標にNaNまたはInfがあります")

    work_object = registry.track(bpy.data.objects.new(original.name, mesh))
    work_object.matrix_world = evaluated.matrix_world.copy()
    collection.objects.link(work_object)

    used = used_material_indices(mesh)
    source_slots: list[MaterialSlotWork] = []
    slot_count = max(len(original.material_slots), max(used, default=-1) + 1)
    # new_from_objectは元Material参照を保持するため、作業専用コピーへ置換する。
    mesh.materials.clear()
    for slot_index in range(slot_count):
        original_material = original.material_slots[slot_index].material if slot_index < len(original.material_slots) else None
        if original_material is None:
            placeholder = registry.track(bpy.data.materials.new(f"__UNUSED_SLOT_{slot_index}"))
            mesh.materials.append(placeholder)
            if slot_index in used:
                raise BakeFailure(f"{original.name}: 使用中のMaterial Slot {slot_index}にMaterialがありません")
            continue
        copied = registry.track(original_material.copy())
        copied.name = f"{original.name}__slot_{slot_index}__source"
        mesh.materials.append(copied)
        if slot_index in used:
            analysis = analyze_material(copied, original.name)
            source_slots.append(MaterialSlotWork(slot_index, copied, analysis))

    bake_uv_name = _unique_uv_name(mesh)
    source_active_render = next((layer.name for layer in mesh.uv_layers if layer.active_render), None)
    bake_uv = mesh.uv_layers.new(name=bake_uv_name, do_init=False)
    if source_active_render and mesh.uv_layers.get(source_active_render):
        mesh.uv_layers.get(source_active_render).active_render = True
        bake_uv.active_render = False
    return WorkObject(original, work_object, bake_uv_name, source_slots)


def unwrap_work_object(context: bpy.types.Context, scene: bpy.types.Scene, work: WorkObject) -> None:
    view_layer = scene.view_layers[0]
    obj = work.object
    for other in scene.objects:
        other.select_set(False)
    obj.select_set(True)
    view_layer.objects.active = obj
    obj.data.uv_layers.active = obj.data.uv_layers.get(work.bake_uv_name)
    override = dict(
        scene=scene,
        view_layer=view_layer,
        active_object=obj,
        object=obj,
        selected_objects=[obj],
        selected_editable_objects=[obj],
    )
    try:
        with context.temp_override(**override):
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            result = bpy.ops.uv.smart_project(
                angle_limit=math.radians(66.0),
                margin_method="FRACTION",
                island_margin=1.0 / 64.0,
                correct_aspect=True,
                scale_to_bounds=False,
            )
            bpy.ops.object.mode_set(mode="OBJECT")
        if "FINISHED" not in result:
            raise BakeFailure(f"{work.original.name}: UV生成に失敗しました")
    finally:
        if obj.mode != "OBJECT":
            with context.temp_override(**override):
                bpy.ops.object.mode_set(mode="OBJECT")


def _new_image(
    registry: TempDataRegistry,
    name: str,
    resolution: int,
    *,
    float_buffer: bool,
    colorspace: str,
    alpha: bool = True,
) -> bpy.types.Image:
    image = registry.track(
        bpy.data.images.new(name=name, width=resolution, height=resolution, alpha=alpha, float_buffer=float_buffer)
    )
    image.colorspace_settings.name = colorspace
    image.file_format = "PNG"
    return image


def _socket_default_rgba(socket: bpy.types.NodeSocket) -> tuple[float, float, float, float]:
    value = socket.default_value
    if isinstance(value, (int, float)):
        scalar = float(value)
        return scalar, scalar, scalar, 1.0
    values = tuple(float(component) for component in value)
    return (values + (1.0, 1.0, 1.0, 1.0))[:4]


def _connect_or_copy(tree: bpy.types.NodeTree, source: bpy.types.NodeSocket, destination: bpy.types.NodeSocket) -> None:
    if getattr(source, "is_output", False):
        tree.links.new(source, destination)
    elif source.is_linked:
        tree.links.new(source.links[0].from_socket, destination)
    else:
        default = source.default_value
        try:
            destination.default_value = default
        except TypeError:
            if isinstance(default, (int, float)):
                destination.default_value = (float(default),) * 4


def _raw_constant(material: bpy.types.Material, channel: str, resolution: int) -> RawChannel | None:
    analysis = analyze_material(material, "<作業用>")
    principled = analysis.principled_node
    socket_name = {
        "Base Color": "Base Color",
        "Metallic": "Metallic",
        "Roughness": "Roughness",
        "Transmission": "Transmission Weight",
    }.get(channel)
    if socket_name:
        socket = principled.inputs[socket_name]
        if not socket.is_linked:
            return RawChannel(channel, resolution, constant=_socket_default_rgba(socket))
    if channel == "Alpha":
        if analysis.alpha.mode == "OPAQUE":
            return RawChannel(channel, resolution, constant=(1.0, 1.0, 1.0, 1.0))
        source = analysis.alpha.source_socket
        if source is not None and not getattr(source, "is_output", False) and not source.is_linked:
            return RawChannel(channel, resolution, constant=_socket_default_rgba(source))
    if channel == "Normal" and not principled.inputs["Normal"].is_linked:
        return RawChannel(channel, resolution, constant=(0.5, 0.5, 1.0, 1.0))
    if channel == "Emissive":
        color = principled.inputs["Emission Color"]
        strength = principled.inputs["Emission Strength"]
        if not color.is_linked and not strength.is_linked:
            rgba = _socket_default_rgba(color)
            scale = float(strength.default_value)
            return RawChannel(channel, resolution, constant=(rgba[0] * scale, rgba[1] * scale, rgba[2] * scale, 1.0))
    return None


def _configure_emission_evaluation(material: bpy.types.Material, channel: str) -> bpy.types.Node:
    analysis = analyze_material(material, "<作業用>")
    tree = material.node_tree
    output = analysis.output_node
    principled = analysis.principled_node
    for link in list(output.inputs["Surface"].links):
        tree.links.remove(link)
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.name = "__SHADER_BAKE_GLB_EVALUATION"
    emission.inputs["Strength"].default_value = 1.0

    if channel == "Emissive":
        _connect_or_copy(tree, principled.inputs["Emission Color"], emission.inputs["Color"])
        _connect_or_copy(tree, principled.inputs["Emission Strength"], emission.inputs["Strength"])
    elif channel == "Alpha":
        source = analysis.alpha.source_socket
        if source is None:
            emission.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        else:
            _connect_or_copy(tree, source, emission.inputs["Color"])
    else:
        socket_name = {
            "Base Color": "Base Color",
            "Metallic": "Metallic",
            "Roughness": "Roughness",
            "Transmission": "Transmission Weight",
        }[channel]
        _connect_or_copy(tree, principled.inputs[socket_name], emission.inputs["Color"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return emission


def _make_dummy_material(registry: TempDataRegistry, image: bpy.types.Image, name: str) -> bpy.types.Material:
    material = registry.track(bpy.data.materials.new(name))
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    image_node = tree.nodes.new("ShaderNodeTexImage")
    image_node.image = image
    image_node.select = True
    tree.nodes.active = image_node
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return material


def _read_pixels(image: bpy.types.Image) -> array:
    values = array("f", [0.0]) * (image.size[0] * image.size[1] * 4)
    image.pixels.foreach_get(values)
    return values


def _validate_raw(raw: RawChannel) -> None:
    values: Iterable[float]
    if raw.constant is not None:
        values = raw.constant
    elif raw.image is not None:
        if tuple(raw.image.size) != (raw.resolution, raw.resolution):
            raise BakeFailure(f"{raw.name}: 画像寸法が{raw.resolution}×{raw.resolution}ではありません")
        values = _read_pixels(raw.image)
    else:
        raise BakeFailure(f"{raw.name}: Bake結果がありません")

    allow_hdr = raw.name == "Emissive"
    for value in values:
        scalar = float(value)
        if not math.isfinite(scalar):
            raise BakeFailure(f"{raw.name}: NaNまたはInfを検出しました")
        if scalar < -1.0e-6:
            raise BakeFailure(f"{raw.name}: 負値を検出しました")
        if not allow_hdr and scalar > 1.0 + 1.0e-5:
            raise BakeFailure(f"{raw.name}: 0～1範囲外の値を検出しました")


def bake_channel(
    context: bpy.types.Context,
    scene: bpy.types.Scene,
    work: WorkObject,
    slot: MaterialSlotWork,
    channel: str,
    resolution: int,
    registry: TempDataRegistry,
) -> RawChannel:
    """Object×Material×Channelを1単位として評価する。"""

    constant = _raw_constant(slot.source_material, channel, resolution)
    if constant is not None:
        _validate_raw(constant)
        slot.result.raw[channel] = constant
        return constant

    token = uuid.uuid4().hex[:8]
    raw_image = _new_image(
        registry,
        f"__SHADER_BAKE_GLB_RAW_{channel}_{token}",
        resolution,
        float_buffer=True,
        colorspace="Non-Color",
    )
    discard = _new_image(registry, f"__SHADER_BAKE_GLB_DISCARD_{token}", 1, float_buffer=True, colorspace="Non-Color")
    # Slot固有の作業Materialをそのまま評価用に使う。元Materialは別DataBlockであり、
    # Material差し替えをBakeごとに繰り返さないことでDepsgraphを安定させる。
    evaluation = slot.source_material
    evaluation_analysis = analyze_material(evaluation, "<作業用>")
    original_surface_source = evaluation_analysis.output_node.inputs["Surface"].links[0].from_socket
    evaluation_node = None
    if channel != "Normal":
        evaluation_node = _configure_emission_evaluation(evaluation, channel)
    for node in evaluation.node_tree.nodes:
        node.select = False
    target_node = evaluation.node_tree.nodes.new("ShaderNodeTexImage")
    target_node.name = "__SHADER_BAKE_GLB_TARGET"
    target_node.image = raw_image
    target_node.select = True
    evaluation.node_tree.nodes.active = target_node

    bake_object = work.object
    mesh = bake_object.data
    discard_nodes = []
    for material_index, other_material in enumerate(mesh.materials):
        if material_index == slot.slot_index or other_material is None:
            continue
        for node in other_material.node_tree.nodes:
            node.select = False
        discard_node = other_material.node_tree.nodes.new("ShaderNodeTexImage")
        discard_node.name = f"__SHADER_BAKE_GLB_DISCARD_TARGET_{token}_{material_index}"
        discard_node.image = discard
        discard_node.select = True
        other_material.node_tree.nodes.active = discard_node
        discard_nodes.append((other_material, discard_node))

    view_layer = scene.view_layers[0]
    for other in scene.objects:
        other.select_set(False)
    bake_object.select_set(True)
    view_layer.objects.active = bake_object
    override = dict(
        scene=scene,
        view_layer=view_layer,
        active_object=bake_object,
        object=bake_object,
        selected_objects=[bake_object],
        selected_editable_objects=[bake_object],
    )
    bake_type = "NORMAL" if channel == "Normal" else "EMIT"
    try:
        with context.temp_override(**override):
            result = bpy.ops.object.bake(
                type=bake_type,
                normal_space="TANGENT",
                target="IMAGE_TEXTURES",
                save_mode="INTERNAL",
                use_clear=True,
                margin=resolution // 64,
                margin_type="EXTEND",
                uv_layer=work.bake_uv_name,
            )
        if "FINISHED" not in result:
            raise BakeFailure(f"{work.original.name} / {slot.source_material.name} / {channel}: Bakeに失敗しました")
        raw = RawChannel(channel, resolution, image=raw_image)
        _validate_raw(raw)
        slot.result.raw[channel] = raw
        return raw
    finally:
        if channel != "Normal":
            output = next(
                node
                for node in evaluation.node_tree.nodes
                if node.bl_idname == "ShaderNodeOutputMaterial" and node.is_active_output
            )
            for link in list(output.inputs["Surface"].links):
                evaluation.node_tree.links.remove(link)
            evaluation.node_tree.links.new(original_surface_source, output.inputs["Surface"])
        if target_node.id_data is not None:
            evaluation.node_tree.nodes.remove(target_node)
        if evaluation_node is not None and evaluation_node.id_data is not None:
            evaluation.node_tree.nodes.remove(evaluation_node)
        for other_material, discard_node in discard_nodes:
            if discard_node.id_data is not None:
                other_material.node_tree.nodes.remove(discard_node)
        view_layer.update()


def _raw_values(raw: RawChannel) -> tuple[array | None, tuple[float, float, float, float] | None]:
    return (_read_pixels(raw.image), None) if raw.image is not None else (None, raw.constant)


def _sample(values: array | None, constant: tuple[float, float, float, float] | None, pixel: int, component: int) -> float:
    if values is not None:
        return float(values[pixel * 4 + component])
    assert constant is not None
    return float(constant[component])


def _write_final_image(
    registry: TempDataRegistry,
    name: str,
    resolution: int,
    colorspace: str,
    pixels: array,
) -> bpy.types.Image:
    image = _new_image(registry, name, resolution, float_buffer=False, colorspace=colorspace)
    image.pixels.foreach_set(pixels)
    image.update()
    # Blender 5.1.1のglTF exporterが複数のdirty generated imageを並列変換すると
    # 不正参照するため、完成画像を先にpackして不変のPNG sourceへ確定する。
    image.pack()
    return image


def _release_raw(registry: TempDataRegistry, result: MaterialBakeResult, *names: str) -> None:
    for name in names:
        raw = result.raw.pop(name, None)
        # 評価MaterialのImage Textureが参照している間は削除しない。
        # Job終了後、ObjectとMaterialを先に外してからregistry.cleanupが削除する。
        _ = raw


def combine_ready_images(slot: MaterialSlotWork, resolution: int, registry: TempDataRegistry, stem: str) -> None:
    """必要なChannelが揃った時点で最終8bit画像へまとめ、float画像を解放する。"""

    result = slot.result
    count = resolution * resolution
    if "base_alpha" not in result.images and {"Base Color", "Alpha"}.issubset(result.raw):
        base_values, base_constant = _raw_values(result.raw["Base Color"])
        alpha_values, alpha_constant = _raw_values(result.raw["Alpha"])
        pixels = array("f", [0.0]) * (count * 4)
        for pixel in range(count):
            offset = pixel * 4
            pixels[offset] = _sample(base_values, base_constant, pixel, 0)
            pixels[offset + 1] = _sample(base_values, base_constant, pixel, 1)
            pixels[offset + 2] = _sample(base_values, base_constant, pixel, 2)
            pixels[offset + 3] = _sample(alpha_values, alpha_constant, pixel, 0)
        result.images["base_alpha"] = _write_final_image(registry, f"{stem}_BaseColorAlpha", resolution, "sRGB", pixels)
        _release_raw(registry, result, "Base Color", "Alpha")

    if "orm" not in result.images and {"Metallic", "Roughness"}.issubset(result.raw):
        metallic_values, metallic_constant = _raw_values(result.raw["Metallic"])
        rough_values, rough_constant = _raw_values(result.raw["Roughness"])
        pixels = array("f", [0.0]) * (count * 4)
        for pixel in range(count):
            offset = pixel * 4
            pixels[offset] = 1.0
            pixels[offset + 1] = _sample(rough_values, rough_constant, pixel, 0)
            pixels[offset + 2] = _sample(metallic_values, metallic_constant, pixel, 0)
            pixels[offset + 3] = 1.0
        result.images["orm"] = _write_final_image(registry, f"{stem}_ORM", resolution, "Non-Color", pixels)
        _release_raw(registry, result, "Metallic", "Roughness")

    if "normal" not in result.images and "Normal" in result.raw:
        values, constant = _raw_values(result.raw["Normal"])
        pixels = array("f", [0.0]) * (count * 4)
        for pixel in range(count):
            offset = pixel * 4
            pixels[offset] = _sample(values, constant, pixel, 0)
            pixels[offset + 1] = _sample(values, constant, pixel, 1)
            pixels[offset + 2] = _sample(values, constant, pixel, 2)
            pixels[offset + 3] = 1.0
        result.images["normal"] = _write_final_image(registry, f"{stem}_Normal", resolution, "Non-Color", pixels)
        _release_raw(registry, result, "Normal")

    if "emissive" not in result.images and "Emissive" in result.raw:
        values, constant = _raw_values(result.raw["Emissive"])
        maximum = 0.0
        for pixel in range(count):
            maximum = max(maximum, *(_sample(values, constant, pixel, component) for component in range(3)))
        strength = maximum if maximum > 1.0 else 1.0
        pixels = array("f", [0.0]) * (count * 4)
        for pixel in range(count):
            offset = pixel * 4
            for component in range(3):
                pixels[offset + component] = _sample(values, constant, pixel, component) / strength
            pixels[offset + 3] = 1.0
        result.emission_strength = strength
        result.images["emissive"] = _write_final_image(registry, f"{stem}_Emissive", resolution, "sRGB", pixels)
        _release_raw(registry, result, "Emissive")

    if "transmission" not in result.images and "Transmission" in result.raw:
        values, constant = _raw_values(result.raw["Transmission"])
        pixels = array("f", [0.0]) * (count * 4)
        for pixel in range(count):
            offset = pixel * 4
            value = _sample(values, constant, pixel, 0)
            pixels[offset] = pixels[offset + 1] = pixels[offset + 2] = value
            pixels[offset + 3] = 1.0
        result.images["transmission"] = _write_final_image(registry, f"{stem}_Transmission", resolution, "Non-Color", pixels)
        _release_raw(registry, result, "Transmission")


def _image_node(tree: bpy.types.NodeTree, image: bpy.types.Image, name: str) -> bpy.types.Node:
    node = tree.nodes.new("ShaderNodeTexImage")
    node.name = name
    node.label = name
    node.image = image
    node.interpolation = "Linear"
    return node


def rebuild_material(slot: MaterialSlotWork, registry: TempDataRegistry, object_name: str) -> bpy.types.Material:
    """ベイク済み画像だけを参照するglTF exporter互換Materialを構築する。"""

    images = slot.result.images
    required = {"base_alpha", "orm", "normal", "emissive", "transmission"}
    missing = required.difference(images)
    if missing:
        raise BakeFailure(f"{object_name}: 最終画像が不足しています: {', '.join(sorted(missing))}")

    material = registry.track(bpy.data.materials.new(f"{object_name}__slot_{slot.slot_index}__baked"))
    material.use_nodes = True
    material.use_backface_culling = slot.source_material.use_backface_culling
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
    tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    base = _image_node(tree, images["base_alpha"], "Base Color + Alpha")
    tree.links.new(base.outputs["Color"], principled.inputs["Base Color"])
    if slot.source_analysis.alpha.mode == "CLIP":
        clip = tree.nodes.new("ShaderNodeMath")
        clip.operation = "GREATER_THAN"
        clip.inputs[1].default_value = slot.source_analysis.alpha.cutoff
        tree.links.new(base.outputs["Alpha"], clip.inputs[0])
        tree.links.new(clip.outputs[0], principled.inputs["Alpha"])
        material.alpha_threshold = slot.source_analysis.alpha.cutoff
    else:
        principled.inputs["Alpha"].default_value = 1.0

    orm = _image_node(tree, images["orm"], "ORM")
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(orm.outputs["Color"], separate.inputs["Color"])
    tree.links.new(separate.outputs["Green"], principled.inputs["Roughness"])
    tree.links.new(separate.outputs["Blue"], principled.inputs["Metallic"])

    normal = _image_node(tree, images["normal"], "Normal")
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    normal_map.space = "TANGENT"
    normal_map.uv_map = BAKE_UV_NAME
    tree.links.new(normal.outputs["Color"], normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])

    emissive = _image_node(tree, images["emissive"], "Emissive")
    tree.links.new(emissive.outputs["Color"], principled.inputs["Emission Color"])
    principled.inputs["Emission Strength"].default_value = slot.result.emission_strength

    transmission = _image_node(tree, images["transmission"], "Transmission")
    tree.links.new(transmission.outputs["Color"], principled.inputs["Transmission Weight"])
    principled.inputs["IOR"].default_value = slot.source_analysis.ior
    slot.final_material = material
    return material


def finalize_work_object(work: WorkObject) -> None:
    """書き出しコピーだけからソースUVを除去し、Bake UVをTEXCOORD_0へ固定する。"""

    mesh = work.object.data
    bake_uv = mesh.uv_layers.get(work.bake_uv_name)
    if bake_uv is None:
        raise BakeFailure(f"{work.original.name}: Bake UVが見つかりません")
    for layer in list(mesh.uv_layers):
        if layer != bake_uv:
            mesh.uv_layers.remove(layer)
    bake_uv.name = BAKE_UV_NAME
    bake_uv.active = True
    bake_uv.active_render = True
    for slot in work.slots:
        if slot.final_material is None:
            raise BakeFailure(f"{work.original.name}: Material再構築が完了していません")
        mesh.materials[slot.slot_index] = slot.final_material
