"""元データを変更せず、作業コピーだけでPBRテクスチャを生成する。"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
import math
from typing import Callable, Iterable
import uuid

import bpy

from .material_validation import AlphaContract, MaterialAnalysis, analyze_material


BAKE_UV_NAME = "GLB_BAKE_UV"
CORE_CHANNELS = ("Base Color", "Metallic", "Roughness", "Normal", "Emissive", "Alpha")
EXTENSION_CHANNELS = {
    "transmission": ("Transmission",),
    "specular": ("Specular", "Specular Tint"),
    "clearcoat": ("Coat", "Coat Roughness", "Coat Normal"),
    "sheen": ("Sheen Weight", "Sheen Tint", "Sheen Roughness"),
    "anisotropy": ("Anisotropic", "Anisotropic Rotation"),
    "occlusion": ("Occlusion",),
    "volume": ("Thickness",),
}
# 従来のimport互換。実際のJobは材質ごとに必要Channelを選ぶ。
CHANNELS = CORE_CHANNELS


def channels_for_analysis(analysis: MaterialAnalysis) -> tuple[str, ...]:
    if analysis.strategy == "UNLIT":
        return ("Base Color", "Alpha")
    channels = list(CORE_CHANNELS)
    if analysis.strategy == "FALLBACK":
        channels.append("Transmission")
        return tuple(channels)
    for extension in sorted(analysis.active_extensions):
        channels.extend(EXTENSION_CHANNELS.get(extension, ()))
    return tuple(channels)


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
        self.node_groups: list[bpy.types.NodeTree] = []

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
        elif isinstance(block, bpy.types.NodeTree):
            self.node_groups.append(block)
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
        for block in list(reversed(self.node_groups)):
            self._remove(self.node_groups, bpy.data.node_groups, block)
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
    alpha_mode: str | None = None
    alpha_cutoff: float = 0.5
    detected_transmission: bool = False


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
    warn: Callable[[str, str, str], None] | None = None,
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
            placeholder = registry.track(bpy.data.materials.new(f"__SHADER_BAKE_GLB_MISSING_SLOT_{slot_index}"))
            placeholder.use_nodes = True
            principled = next(node for node in placeholder.node_tree.nodes if node.bl_idname == "ShaderNodeBsdfPrincipled")
            principled.inputs["Base Color"].default_value = (0.8, 0.8, 0.8, 1.0)
            principled.inputs["Roughness"].default_value = 0.5
            mesh.materials.append(placeholder)
            if slot_index in used:
                if warn:
                    warn(original.name, f"Slot {slot_index}", "Material未割当のため既定PBR材質へ置換しました")
                analysis = analyze_material(placeholder, original.name)
                source_slots.append(MaterialSlotWork(slot_index, placeholder, analysis))
            continue
        copied = registry.track(original_material.copy())
        copied.name = f"{original.name}__slot_{slot_index}__source"
        mesh.materials.append(copied)
        if slot_index in used:
            analysis = analyze_material(copied, original.name)
            if not copied.use_nodes or copied.node_tree is None:
                viewport = tuple(float(v) for v in copied.diffuse_color)
                copied.use_nodes = True
                tree = copied.node_tree
                principled = next(node for node in tree.nodes if node.bl_idname == "ShaderNodeBsdfPrincipled")
                principled.inputs["Base Color"].default_value = viewport
                principled.inputs["Alpha"].default_value = viewport[3]
                principled.inputs["Roughness"].default_value = float(getattr(copied, "roughness", 0.5))
                principled.inputs["Metallic"].default_value = float(getattr(copied, "metallic", 0.0))
            elif analysis.output_node is None or analysis.output_node.inputs.get("Surface") is None or not analysis.output_node.inputs["Surface"].is_linked:
                viewport = tuple(float(v) for v in copied.diffuse_color)
                tree = copied.node_tree
                tree.nodes.clear()
                output = tree.nodes.new("ShaderNodeOutputMaterial")
                principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
                principled.inputs["Base Color"].default_value = viewport
                principled.inputs["Alpha"].default_value = viewport[3]
                principled.inputs["Roughness"].default_value = float(getattr(copied, "roughness", 0.5))
                principled.inputs["Metallic"].default_value = float(getattr(copied, "metallic", 0.0))
                tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
            if warn:
                for reason in analysis.fallback_reasons:
                    warn(original.name, original_material.name, reason)
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


def _raw_constant(slot: MaterialSlotWork, channel: str, resolution: int) -> RawChannel | None:
    analysis = slot.source_analysis
    principled = analysis.principled_node
    if analysis.strategy == "FALLBACK":
        if channel == "Metallic":
            return RawChannel(channel, resolution, constant=(0.0, 0.0, 0.0, 1.0))
        return None
    if principled is None:
        return None
    socket_name = {
        "Base Color": "Base Color",
        "Metallic": "Metallic",
        "Roughness": "Roughness",
        "Transmission": "Transmission Weight",
        "Specular": "Specular IOR Level",
        "Specular Tint": "Specular Tint",
        "Coat": "Coat Weight",
        "Coat Roughness": "Coat Roughness",
        "Sheen Weight": "Sheen Weight",
        "Sheen Tint": "Sheen Tint",
        "Sheen Roughness": "Sheen Roughness",
        "Anisotropic": "Anisotropic",
        "Anisotropic Rotation": "Anisotropic Rotation",
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
    if channel == "Coat Normal" and not principled.inputs["Coat Normal"].is_linked:
        return RawChannel(channel, resolution, constant=(0.5, 0.5, 1.0, 1.0))
    if channel == "Emissive":
        color = principled.inputs["Emission Color"]
        strength = principled.inputs["Emission Strength"]
        if not color.is_linked and not strength.is_linked:
            rgba = _socket_default_rgba(color)
            scale = float(strength.default_value)
            return RawChannel(channel, resolution, constant=(rgba[0] * scale, rgba[1] * scale, rgba[2] * scale, 1.0))
    if channel == "Occlusion" and analysis.occlusion_socket is not None and not analysis.occlusion_socket.is_linked:
        return RawChannel(channel, resolution, constant=_socket_default_rgba(analysis.occlusion_socket))
    if channel == "Thickness" and analysis.thickness_socket is not None and not analysis.thickness_socket.is_linked:
        return RawChannel(channel, resolution, constant=_socket_default_rgba(analysis.thickness_socket))
    return None


def _configure_emission_evaluation(material: bpy.types.Material, analysis: MaterialAnalysis, channel: str) -> bpy.types.Node:
    tree = material.node_tree
    output = analysis.output_node
    principled = analysis.principled_node
    if output is None or principled is None:
        raise BakeFailure(f"{material.name}: PBR評価入口がありません")
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
    elif channel == "Occlusion" and analysis.occlusion_socket is not None:
        _connect_or_copy(tree, analysis.occlusion_socket, emission.inputs["Color"])
    elif channel == "Thickness" and analysis.thickness_socket is not None:
        _connect_or_copy(tree, analysis.thickness_socket, emission.inputs["Color"])
    else:
        socket_name = {
            "Base Color": "Base Color",
            "Metallic": "Metallic",
            "Roughness": "Roughness",
            "Transmission": "Transmission Weight",
            "Specular": "Specular IOR Level",
            "Specular Tint": "Specular Tint",
            "Coat": "Coat Weight",
            "Coat Roughness": "Coat Roughness",
            "Sheen Weight": "Sheen Weight",
            "Sheen Tint": "Sheen Tint",
            "Sheen Roughness": "Sheen Roughness",
            "Anisotropic": "Anisotropic",
            "Anisotropic Rotation": "Anisotropic Rotation",
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


def _channel_default(slot: MaterialSlotWork, channel: str, resolution: int) -> RawChannel:
    viewport = tuple(float(v) for v in slot.source_material.diffuse_color)
    defaults = {
        "Base Color": (viewport[0], viewport[1], viewport[2], 1.0),
        "Metallic": (0.0, 0.0, 0.0, 1.0),
        "Roughness": (0.5, 0.5, 0.5, 1.0),
        "Normal": (0.5, 0.5, 1.0, 1.0),
        "Emissive": (0.0, 0.0, 0.0, 1.0),
        "Alpha": (viewport[3] if len(viewport) > 3 else 1.0,) * 3 + (1.0,),
        "Transmission": (0.0, 0.0, 0.0, 1.0),
        "Specular": (0.5, 0.5, 0.5, 1.0),
        "Specular Tint": (1.0, 1.0, 1.0, 1.0),
        "Coat": (0.0, 0.0, 0.0, 1.0),
        "Coat Roughness": (0.03, 0.03, 0.03, 1.0),
        "Coat Normal": (0.5, 0.5, 1.0, 1.0),
        "Sheen Weight": (0.0, 0.0, 0.0, 1.0),
        "Sheen Tint": (1.0, 1.0, 1.0, 1.0),
        "Sheen Roughness": (0.5, 0.5, 0.5, 1.0),
        "Anisotropic": (0.0, 0.0, 0.0, 1.0),
        "Anisotropic Rotation": (0.0, 0.0, 0.0, 1.0),
        "Occlusion": (1.0, 1.0, 1.0, 1.0),
        "Thickness": (0.0, 0.0, 0.0, 1.0),
    }
    values = tuple(min(1.0, max(0.0, float(v))) for v in defaults[channel])
    return RawChannel(channel, resolution, constant=values)


def bake_channel(
    context: bpy.types.Context,
    scene: bpy.types.Scene,
    work: WorkObject,
    slot: MaterialSlotWork,
    channel: str,
    resolution: int,
    registry: TempDataRegistry,
    warn: Callable[[str, str, str], None] | None = None,
) -> RawChannel:
    """Object×Material×Channelを1単位として評価する。"""

    constant = _raw_constant(slot, channel, resolution)
    if constant is not None:
        _validate_raw(constant)
        slot.result.raw[channel] = constant
        return constant

    token = uuid.uuid4().hex[:8]
    raw_image = None
    discard = None
    target_node = None
    discard_nodes = []
    evaluation_node = None
    original_surface_source = None
    normal_restore: tuple[bpy.types.NodeSocket | None, object] | None = None
    evaluation = slot.source_material
    analysis = slot.source_analysis
    view_layer = scene.view_layers[0]
    try:
        raw_image = _new_image(
            registry,
            f"__SHADER_BAKE_GLB_RAW_{channel}_{token}",
            resolution,
            float_buffer=True,
            colorspace="Non-Color",
        )
        discard = _new_image(registry, f"__SHADER_BAKE_GLB_DISCARD_{token}", 1, float_buffer=True, colorspace="Non-Color")
        if evaluation.node_tree is None:
            raise BakeFailure("評価用NodeTreeがありません")
        if analysis.strategy == "PBR":
            if analysis.output_node is None or analysis.principled_node is None:
                raise BakeFailure("PBR評価入口がありません")
            original_surface_source = analysis.output_node.inputs["Surface"].links[0].from_socket
            if channel == "Coat Normal":
                normal_socket = analysis.principled_node.inputs["Normal"]
                old_source = normal_socket.links[0].from_socket if normal_socket.is_linked else None
                old_default = normal_socket.default_value[:]
                for link in list(normal_socket.links):
                    evaluation.node_tree.links.remove(link)
                coat_socket = analysis.principled_node.inputs["Coat Normal"]
                if coat_socket.is_linked:
                    evaluation.node_tree.links.new(coat_socket.links[0].from_socket, normal_socket)
                else:
                    normal_socket.default_value = coat_socket.default_value
                normal_restore = (old_source, old_default)
            elif channel not in {"Normal"}:
                evaluation_node = _configure_emission_evaluation(evaluation, analysis, channel)
        for node in evaluation.node_tree.nodes:
            node.select = False
        target_node = evaluation.node_tree.nodes.new("ShaderNodeTexImage")
        target_node.name = "__SHADER_BAKE_GLB_TARGET"
        target_node.image = raw_image
        target_node.select = True
        evaluation.node_tree.nodes.active = target_node

        bake_object = work.object
        mesh = bake_object.data
        for material_index, other_material in enumerate(mesh.materials):
            if material_index == slot.slot_index or other_material is None or other_material.node_tree is None:
                continue
            for node in other_material.node_tree.nodes:
                node.select = False
            discard_node = other_material.node_tree.nodes.new("ShaderNodeTexImage")
            discard_node.name = f"__SHADER_BAKE_GLB_DISCARD_TARGET_{token}_{material_index}"
            discard_node.image = discard
            discard_node.select = True
            other_material.node_tree.nodes.active = discard_node
            discard_nodes.append((other_material, discard_node))

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
        kwargs = dict(target="IMAGE_TEXTURES", save_mode="INTERNAL", use_clear=True, margin=resolution // 64, margin_type="EXTEND", uv_layer=work.bake_uv_name)
        if analysis.strategy in {"FALLBACK", "UNLIT"}:
            bake_type = {
                "Base Color": "EMIT" if analysis.strategy == "UNLIT" else "DIFFUSE",
                "Roughness": "ROUGHNESS",
                "Normal": "NORMAL",
                "Emissive": "EMIT",
                "Alpha": "COMBINED",
                "Transmission": "TRANSMISSION",
            }[channel]
            if channel in {"Base Color", "Transmission"}:
                kwargs["pass_filter"] = {"COLOR"}
            elif channel == "Alpha":
                kwargs["pass_filter"] = {"COLOR", "DIFFUSE", "GLOSSY", "TRANSMISSION", "EMIT"}
        else:
            bake_type = "NORMAL" if channel in {"Normal", "Coat Normal"} else "EMIT"
        with context.temp_override(**override):
            result = bpy.ops.object.bake(type=bake_type, normal_space="TANGENT", **kwargs)
        if "FINISHED" not in result:
            raise BakeFailure(f"{work.original.name} / {slot.source_material.name} / {channel}: Bakeに失敗しました")
        raw = RawChannel(channel, resolution, image=raw_image)
        _validate_raw(raw)
        slot.result.raw[channel] = raw
        return raw
    except Exception as exc:
        fallback = _channel_default(slot, channel, resolution)
        slot.result.raw[channel] = fallback
        if warn:
            warn(work.original.name, slot.source_material.name, f"{channel}のBakeに失敗したため既定値へ置換しました: {exc}")
        return fallback
    finally:
        if analysis.strategy == "PBR" and evaluation.node_tree is not None:
            if evaluation_node is not None and analysis.output_node is not None and original_surface_source is not None:
                for link in list(analysis.output_node.inputs["Surface"].links):
                    evaluation.node_tree.links.remove(link)
                evaluation.node_tree.links.new(original_surface_source, analysis.output_node.inputs["Surface"])
            if normal_restore is not None and analysis.principled_node is not None:
                normal_socket = analysis.principled_node.inputs["Normal"]
                for link in list(normal_socket.links):
                    evaluation.node_tree.links.remove(link)
                old_source, old_default = normal_restore
                if old_source is not None:
                    evaluation.node_tree.links.new(old_source, normal_socket)
                else:
                    normal_socket.default_value = old_default
        if target_node is not None and target_node.id_data is not None:
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


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _combine_scalar_image(
    slot: MaterialSlotWork,
    registry: TempDataRegistry,
    resolution: int,
    raw_name: str,
    image_name: str,
    stem: str,
) -> None:
    result = slot.result
    if image_name in result.images or raw_name not in result.raw:
        return
    values, constant = _raw_values(result.raw[raw_name])
    pixels = array("f", [0.0]) * (resolution * resolution * 4)
    for pixel in range(resolution * resolution):
        value = _clamp01(_sample(values, constant, pixel, 0))
        offset = pixel * 4
        pixels[offset] = pixels[offset + 1] = pixels[offset + 2] = value
        pixels[offset + 3] = 1.0
    result.images[image_name] = _write_final_image(registry, f"{stem}_{image_name}", resolution, "Non-Color", pixels)
    _release_raw(registry, result, raw_name)


def combine_ready_images(slot: MaterialSlotWork, resolution: int, registry: TempDataRegistry, stem: str) -> None:
    """必要なChannelが揃った時点で最終8bit画像へまとめ、float画像を解放する。"""

    result = slot.result
    count = resolution * resolution
    fallback = slot.source_analysis.strategy == "FALLBACK"
    base_factor = slot.source_analysis.base_color_factor if slot.source_analysis.strategy == "PBR" else (1.0, 1.0, 1.0, 1.0)
    metallic_factor = slot.source_analysis.metallic_factor if slot.source_analysis.strategy == "PBR" else 1.0
    roughness_factor = slot.source_analysis.roughness_factor if slot.source_analysis.strategy == "PBR" else 1.0

    def remove_factor(value: float, factor: float) -> float:
        # factor=0ではtexture値が外観へ寄与しないため、白へ正規化する。
        return 1.0 if factor <= 1.0e-6 else _clamp01(value / factor)

    base_ready = {"Base Color", "Alpha"}.issubset(result.raw) and (not fallback or "Transmission" in result.raw)
    if "base_alpha" not in result.images and base_ready:
        base_values, base_constant = _raw_values(result.raw["Base Color"])
        alpha_values, alpha_constant = _raw_values(result.raw["Alpha"])
        transmission_values, transmission_constant = _raw_values(result.raw["Transmission"]) if fallback else (None, None)
        normal_values, normal_constant = _raw_values(result.raw["Normal"]) if "Normal" in result.raw else (None, (0.5, 0.5, 1.0, 1.0))
        valid_alpha: list[float] = []
        pixels = array("f", [0.0]) * (count * 4)
        for pixel in range(count):
            offset = pixel * 4
            alpha = _clamp01(_sample(alpha_values, alpha_constant, pixel, 0))
            if _sample(normal_values, normal_constant, pixel, 2) > 0.05:
                valid_alpha.append(alpha)
            for component in range(3):
                value = _sample(base_values, base_constant, pixel, component)
                if fallback and transmission_values is not None and abs(value) <= 1.0e-6:
                    value = _sample(transmission_values, transmission_constant, pixel, component)
                if fallback and alpha > 1.0e-6:
                    value /= alpha
                pixels[offset + component] = remove_factor(value, base_factor[component])
            pixels[offset + 3] = remove_factor(alpha, base_factor[3])
        result.images["base_alpha"] = _write_final_image(registry, f"{stem}_BaseColorAlpha", resolution, "sRGB", pixels)
        _release_raw(registry, result, "Base Color", "Alpha")
        if fallback:
            if valid_alpha and all(value >= 1.0 - 1.0e-4 for value in valid_alpha):
                result.alpha_mode = "OPAQUE"
            elif valid_alpha and all(value <= 1.0e-4 or value >= 1.0 - 1.0e-4 for value in valid_alpha):
                result.alpha_mode = "MASK"
                result.alpha_cutoff = 0.5
            else:
                result.alpha_mode = "BLEND"
        else:
            result.alpha_mode = slot.source_analysis.alpha.mode
            result.alpha_cutoff = slot.source_analysis.alpha.cutoff

    orm_required = {"Metallic", "Roughness"}
    if "occlusion" in slot.source_analysis.active_extensions:
        orm_required.add("Occlusion")
    if "orm" not in result.images and orm_required.issubset(result.raw):
        metallic_values, metallic_constant = _raw_values(result.raw["Metallic"])
        rough_values, rough_constant = _raw_values(result.raw["Roughness"])
        occlusion_values, occlusion_constant = _raw_values(result.raw["Occlusion"]) if "Occlusion" in result.raw else (None, (1.0, 1.0, 1.0, 1.0))
        pixels = array("f", [0.0]) * (count * 4)
        for pixel in range(count):
            offset = pixel * 4
            pixels[offset] = _clamp01(_sample(occlusion_values, occlusion_constant, pixel, 0))
            pixels[offset + 1] = remove_factor(_sample(rough_values, rough_constant, pixel, 0), roughness_factor)
            pixels[offset + 2] = remove_factor(_sample(metallic_values, metallic_constant, pixel, 0), metallic_factor)
            pixels[offset + 3] = 1.0
        result.images["orm"] = _write_final_image(registry, f"{stem}_ORM", resolution, "Non-Color", pixels)
        _release_raw(registry, result, "Metallic", "Roughness", "Occlusion")

    if "normal" not in result.images and "Normal" in result.raw:
        values, constant = _raw_values(result.raw["Normal"])
        pixels = array("f", [0.0]) * (count * 4)
        for pixel in range(count):
            offset = pixel * 4
            pixels[offset] = _clamp01(_sample(values, constant, pixel, 0))
            pixels[offset + 1] = _clamp01(_sample(values, constant, pixel, 1))
            pixels[offset + 2] = _clamp01(_sample(values, constant, pixel, 2))
            pixels[offset + 3] = 1.0
        result.images["normal"] = _write_final_image(registry, f"{stem}_Normal", resolution, "Non-Color", pixels)
        _release_raw(registry, result, "Normal")

    if "emissive" not in result.images and "Emissive" in result.raw:
        values, constant = _raw_values(result.raw["Emissive"])
        direct_emission = slot.source_analysis.strategy == "PBR" and (
            slot.source_analysis.emission_color_factor != (1.0, 1.0, 1.0)
            or abs(slot.source_analysis.emission_strength_factor - 1.0) > 1.0e-6
        )
        if direct_emission:
            emission_factors = tuple(
                value * slot.source_analysis.emission_strength_factor
                for value in slot.source_analysis.emission_color_factor
            )
            strength = slot.source_analysis.emission_strength_factor
        else:
            maximum = 0.0
            for pixel in range(count):
                maximum = max(maximum, *(_sample(values, constant, pixel, component) for component in range(3)))
            strength = maximum if maximum > 1.0 else 1.0
            emission_factors = (strength, strength, strength)
        pixels = array("f", [0.0]) * (count * 4)
        for pixel in range(count):
            offset = pixel * 4
            for component in range(3):
                pixels[offset + component] = remove_factor(
                    _sample(values, constant, pixel, component),
                    emission_factors[component],
                )
            pixels[offset + 3] = 1.0
        result.emission_strength = strength
        result.images["emissive"] = _write_final_image(registry, f"{stem}_Emissive", resolution, "sRGB", pixels)
        _release_raw(registry, result, "Emissive")

    if "transmission" not in result.images and "Transmission" in result.raw:
        values, constant = _raw_values(result.raw["Transmission"])
        pixels = array("f", [0.0]) * (count * 4)
        maximum = 0.0
        for pixel in range(count):
            offset = pixel * 4
            value = max(_sample(values, constant, pixel, component) for component in range(3))
            value = _clamp01(value)
            maximum = max(maximum, value)
            pixels[offset] = pixels[offset + 1] = pixels[offset + 2] = value
            pixels[offset + 3] = 1.0
        result.images["transmission"] = _write_final_image(registry, f"{stem}_Transmission", resolution, "Non-Color", pixels)
        result.detected_transmission = maximum > 1.0e-5
        _release_raw(registry, result, "Transmission")

    for raw_name, image_name in (
        ("Specular", "specular"),
        ("Coat", "coat"),
        ("Coat Roughness", "coat_roughness"),
        ("Sheen Roughness", "sheen_roughness"),
        ("Anisotropic", "anisotropic"),
        ("Anisotropic Rotation", "anisotropic_rotation"),
        ("Thickness", "thickness"),
    ):
        _combine_scalar_image(slot, registry, resolution, raw_name, image_name, stem)

    for raw_name, image_name, colorspace in (
        ("Specular Tint", "specular_tint", "sRGB"),
        ("Coat Normal", "coat_normal", "Non-Color"),
    ):
        if image_name in result.images or raw_name not in result.raw:
            continue
        values, constant = _raw_values(result.raw[raw_name])
        pixels = array("f", [0.0]) * (count * 4)
        for pixel in range(count):
            offset = pixel * 4
            for component in range(3):
                pixels[offset + component] = _clamp01(_sample(values, constant, pixel, component))
            pixels[offset + 3] = 1.0
        result.images[image_name] = _write_final_image(registry, f"{stem}_{image_name}", resolution, colorspace, pixels)
        _release_raw(registry, result, raw_name)

    if "sheen_tint" not in result.images and {"Sheen Weight", "Sheen Tint"}.issubset(result.raw):
        weight_values, weight_constant = _raw_values(result.raw["Sheen Weight"])
        tint_values, tint_constant = _raw_values(result.raw["Sheen Tint"])
        pixels = array("f", [0.0]) * (count * 4)
        for pixel in range(count):
            offset = pixel * 4
            weight = _clamp01(_sample(weight_values, weight_constant, pixel, 0))
            for component in range(3):
                pixels[offset + component] = _clamp01(_sample(tint_values, tint_constant, pixel, component) * weight)
            pixels[offset + 3] = 1.0
        result.images["sheen_tint"] = _write_final_image(registry, f"{stem}_SheenTint", resolution, "sRGB", pixels)
        _release_raw(registry, result, "Sheen Weight", "Sheen Tint")


def _image_node(tree: bpy.types.NodeTree, image: bpy.types.Image, name: str) -> bpy.types.Node:
    node = tree.nodes.new("ShaderNodeTexImage")
    node.name = name
    node.label = name
    node.image = image
    node.interpolation = "Linear"
    return node


def _gltf_settings_node(tree: bpy.types.NodeTree, registry: TempDataRegistry) -> bpy.types.Node:
    """Blender標準exporterが認識する補助入力をJob所有Node Groupで作る。"""

    group = registry.track(bpy.data.node_groups.new(f"glTF Material Output __SHADER_BAKE_GLB_{uuid.uuid4().hex[:8]}", "ShaderNodeTree"))
    group.interface.new_socket(name="Occlusion", in_out="INPUT", socket_type="NodeSocketFloat")
    group.interface.new_socket(name="Thickness", in_out="INPUT", socket_type="NodeSocketFloat")
    group.nodes.new("NodeGroupOutput")
    group.nodes.new("NodeGroupInput")
    node = tree.nodes.new("ShaderNodeGroup")
    node.node_tree = group
    node.name = "glTF Material Output"
    return node


def rebuild_material(slot: MaterialSlotWork, registry: TempDataRegistry, object_name: str) -> bpy.types.Material:
    """ベイク済み画像だけを参照するglTF exporter互換Materialを構築する。"""

    images = slot.result.images
    required = {"base_alpha"} if slot.source_analysis.strategy == "UNLIT" else {"base_alpha", "orm", "normal", "emissive"}
    extension_images = {
        "transmission": {"transmission"},
        "specular": {"specular", "specular_tint"},
        "clearcoat": {"coat", "coat_roughness", "coat_normal"},
        "sheen": {"sheen_tint", "sheen_roughness"},
        "anisotropy": {"anisotropic", "anisotropic_rotation"},
        "volume": {"thickness"},
    }
    extensions = set(slot.source_analysis.active_extensions) if slot.source_analysis.strategy == "PBR" else set()
    if slot.source_analysis.strategy == "FALLBACK" and slot.result.detected_transmission:
        extensions.add("transmission")
    for extension in extensions:
        required.update(extension_images.get(extension, set()))
    missing = required.difference(images)
    if missing:
        raise BakeFailure(f"{object_name}: 最終画像が不足しています: {', '.join(sorted(missing))}")

    material = registry.track(bpy.data.materials.new(f"{object_name}__slot_{slot.slot_index}__baked"))
    material.use_nodes = True
    material.use_backface_culling = slot.source_material.use_backface_culling
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    if slot.source_analysis.strategy == "UNLIT":
        base = _image_node(tree, images["base_alpha"], "Base Color + Alpha")
        alpha_mode = slot.result.alpha_mode or slot.source_analysis.alpha.mode
        if alpha_mode == "OPAQUE":
            tree.links.new(base.outputs["Color"], output.inputs["Surface"])
        else:
            transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
            mix = tree.nodes.new("ShaderNodeMixShader")
            factor = base.outputs["Alpha"]
            if alpha_mode in {"CLIP", "MASK"}:
                clip = tree.nodes.new("ShaderNodeMath")
                clip.operation = "GREATER_THAN"
                clip.inputs[1].default_value = slot.result.alpha_cutoff
                tree.links.new(factor, clip.inputs[0])
                factor = clip.outputs[0]
                material.surface_render_method = "DITHERED"
            else:
                material.surface_render_method = "BLENDED"
            tree.links.new(factor, mix.inputs[0])
            tree.links.new(transparent.outputs[0], mix.inputs[1])
            tree.links.new(base.outputs["Color"], mix.inputs[2])
            tree.links.new(mix.outputs[0], output.inputs["Surface"])
        slot.final_material = material
        return material
    principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
    tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    base = _image_node(tree, images["base_alpha"], "Base Color + Alpha")
    base_factor = slot.source_analysis.base_color_factor if slot.source_analysis.strategy == "PBR" else (1.0, 1.0, 1.0, 1.0)
    base_color_output = base.outputs["Color"]
    if any(abs(base_factor[index] - 1.0) > 1.0e-6 for index in range(3)):
        multiply_color = tree.nodes.new("ShaderNodeMix")
        multiply_color.data_type = "RGBA"
        multiply_color.blend_type = "MULTIPLY"
        next(socket for socket in multiply_color.inputs if socket.identifier == "Factor_Float").default_value = 1.0
        color_a = next(socket for socket in multiply_color.inputs if socket.identifier == "A_Color")
        color_b = next(socket for socket in multiply_color.inputs if socket.identifier == "B_Color")
        color_b.default_value = (*base_factor[:3], 1.0)
        tree.links.new(base_color_output, color_a)
        base_color_output = next(socket for socket in multiply_color.outputs if socket.identifier == "Result_Color")
    tree.links.new(base_color_output, principled.inputs["Base Color"])
    alpha_mode = slot.result.alpha_mode or slot.source_analysis.alpha.mode
    alpha_cutoff = slot.result.alpha_cutoff
    alpha_output = base.outputs["Alpha"]
    if abs(base_factor[3] - 1.0) > 1.0e-6:
        multiply_alpha = tree.nodes.new("ShaderNodeMath")
        multiply_alpha.operation = "MULTIPLY"
        multiply_alpha.inputs[1].default_value = base_factor[3]
        tree.links.new(alpha_output, multiply_alpha.inputs[0])
        alpha_output = multiply_alpha.outputs[0]
    if alpha_mode == "CLIP" or alpha_mode == "MASK":
        clip = tree.nodes.new("ShaderNodeMath")
        clip.operation = "GREATER_THAN"
        clip.inputs[1].default_value = alpha_cutoff
        tree.links.new(alpha_output, clip.inputs[0])
        tree.links.new(clip.outputs[0], principled.inputs["Alpha"])
        material.surface_render_method = "DITHERED"
    elif alpha_mode == "BLEND":
        tree.links.new(alpha_output, principled.inputs["Alpha"])
        material.surface_render_method = "BLENDED"
    else:
        principled.inputs["Alpha"].default_value = 1.0

    orm = _image_node(tree, images["orm"], "ORM")
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(orm.outputs["Color"], separate.inputs["Color"])
    roughness_output = separate.outputs["Green"]
    roughness_factor = slot.source_analysis.roughness_factor if slot.source_analysis.strategy == "PBR" else 1.0
    if abs(roughness_factor - 1.0) > 1.0e-6:
        multiply_roughness = tree.nodes.new("ShaderNodeMath")
        multiply_roughness.operation = "MULTIPLY"
        multiply_roughness.inputs[1].default_value = roughness_factor
        tree.links.new(roughness_output, multiply_roughness.inputs[0])
        roughness_output = multiply_roughness.outputs[0]
    metallic_output = separate.outputs["Blue"]
    metallic_factor = slot.source_analysis.metallic_factor if slot.source_analysis.strategy == "PBR" else 1.0
    if abs(metallic_factor - 1.0) > 1.0e-6:
        multiply_metallic = tree.nodes.new("ShaderNodeMath")
        multiply_metallic.operation = "MULTIPLY"
        multiply_metallic.inputs[1].default_value = metallic_factor
        tree.links.new(metallic_output, multiply_metallic.inputs[0])
        metallic_output = multiply_metallic.outputs[0]
    tree.links.new(roughness_output, principled.inputs["Roughness"])
    tree.links.new(metallic_output, principled.inputs["Metallic"])

    settings = None
    if "occlusion" in extensions or "volume" in extensions:
        settings = _gltf_settings_node(tree, registry)
    if "occlusion" in extensions and settings is not None:
        tree.links.new(separate.outputs["Red"], settings.inputs["Occlusion"])

    normal = _image_node(tree, images["normal"], "Normal")
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    normal_map.space = "TANGENT"
    normal_map.uv_map = BAKE_UV_NAME
    tree.links.new(normal.outputs["Color"], normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])

    emissive = _image_node(tree, images["emissive"], "Emissive")
    emissive_output = emissive.outputs["Color"]
    emission_factor = slot.source_analysis.emission_color_factor if slot.source_analysis.strategy == "PBR" else (1.0, 1.0, 1.0)
    if any(abs(value - 1.0) > 1.0e-6 for value in emission_factor):
        multiply_emission = tree.nodes.new("ShaderNodeMix")
        multiply_emission.data_type = "RGBA"
        multiply_emission.blend_type = "MULTIPLY"
        next(socket for socket in multiply_emission.inputs if socket.identifier == "Factor_Float").default_value = 1.0
        emission_a = next(socket for socket in multiply_emission.inputs if socket.identifier == "A_Color")
        emission_b = next(socket for socket in multiply_emission.inputs if socket.identifier == "B_Color")
        emission_b.default_value = (*emission_factor, 1.0)
        tree.links.new(emissive_output, emission_a)
        emissive_output = next(socket for socket in multiply_emission.outputs if socket.identifier == "Result_Color")
    tree.links.new(emissive_output, principled.inputs["Emission Color"])
    principled.inputs["Emission Strength"].default_value = slot.result.emission_strength

    if "transmission" in extensions:
        transmission = _image_node(tree, images["transmission"], "Transmission")
        tree.links.new(transmission.outputs["Color"], principled.inputs["Transmission Weight"])
    principled.inputs["IOR"].default_value = slot.source_analysis.ior if slot.source_analysis.strategy == "PBR" else 1.5

    if "specular" in extensions:
        specular = _image_node(tree, images["specular"], "Specular")
        specular_tint = _image_node(tree, images["specular_tint"], "Specular Tint")
        tree.links.new(specular.outputs["Color"], principled.inputs["Specular IOR Level"])
        tree.links.new(specular_tint.outputs["Color"], principled.inputs["Specular Tint"])

    if "clearcoat" in extensions:
        coat = _image_node(tree, images["coat"], "Coat")
        coat_roughness = _image_node(tree, images["coat_roughness"], "Coat Roughness")
        coat_normal = _image_node(tree, images["coat_normal"], "Coat Normal")
        coat_normal_map = tree.nodes.new("ShaderNodeNormalMap")
        coat_normal_map.space = "TANGENT"
        coat_normal_map.uv_map = BAKE_UV_NAME
        tree.links.new(coat.outputs["Color"], principled.inputs["Coat Weight"])
        tree.links.new(coat_roughness.outputs["Color"], principled.inputs["Coat Roughness"])
        tree.links.new(coat_normal.outputs["Color"], coat_normal_map.inputs["Color"])
        tree.links.new(coat_normal_map.outputs["Normal"], principled.inputs["Coat Normal"])

    if "sheen" in extensions:
        sheen_tint = _image_node(tree, images["sheen_tint"], "Sheen Tint")
        sheen_roughness = _image_node(tree, images["sheen_roughness"], "Sheen Roughness")
        principled.inputs["Sheen Weight"].default_value = 1.0
        tree.links.new(sheen_tint.outputs["Color"], principled.inputs["Sheen Tint"])
        tree.links.new(sheen_roughness.outputs["Color"], principled.inputs["Sheen Roughness"])

    if "anisotropy" in extensions:
        anisotropic = _image_node(tree, images["anisotropic"], "Anisotropic")
        rotation = _image_node(tree, images["anisotropic_rotation"], "Anisotropic Rotation")
        tangent = tree.nodes.new("ShaderNodeTangent")
        tangent.direction_type = "UV_MAP"
        tangent.uv_map = BAKE_UV_NAME
        tree.links.new(anisotropic.outputs["Color"], principled.inputs["Anisotropic"])
        tree.links.new(rotation.outputs["Color"], principled.inputs["Anisotropic Rotation"])
        tree.links.new(tangent.outputs["Tangent"], principled.inputs["Tangent"])

    if "volume" in extensions and settings is not None:
        thickness = _image_node(tree, images["thickness"], "Thickness")
        tree.links.new(thickness.outputs["Color"], settings.inputs["Thickness"])
        source_volume = slot.source_analysis.volume_node
        if source_volume is not None:
            volume = tree.nodes.new("ShaderNodeVolumePrincipled")
            for name in ("Color", "Density"):
                source = source_volume.inputs.get(name)
                destination = volume.inputs.get(name)
                if source is not None and destination is not None and not source.is_linked:
                    destination.default_value = source.default_value
            tree.links.new(volume.outputs["Volume"], output.inputs["Volume"])
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
