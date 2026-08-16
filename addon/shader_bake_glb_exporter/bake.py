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
# Channel名はJobの進捗表示とテクスチャ結合の双方で使う。ここでPBR拡張との対応を固定する。
# 従来のimport互換。実際のJobは材質ごとに必要Channelを選ぶ。
CHANNELS = CORE_CHANNELS


def channels_for_analysis(analysis: MaterialAnalysis) -> tuple[str, ...]:
    # UNLITはBase/Alphaだけ、FALLBACKは外観近似に必要な最小組、PBRは有効拡張を追加する。
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
        # 種別ごとに分けておくと、依存するObjectから安全な逆順で削除できる。
        self.scenes: list[bpy.types.Scene] = []
        self.collections: list[bpy.types.Collection] = []
        self.objects: list[bpy.types.Object] = []
        self.meshes: list[bpy.types.Mesh] = []
        self.materials: list[bpy.types.Material] = []
        self.images: list[bpy.types.Image] = []
        self.node_groups: list[bpy.types.NodeTree] = []

    def track(self, block):
        # Jobが自ら生成したDataBlockだけを記録する。元DataBlockを所有してはいけない。
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
        # 途中で個別解放済みでもcleanupを失敗させないよう、RNA無効参照を無視する。
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
    """ベイク直後のChannel。画像または定数のどちらかで保持し、結合後に解放する。"""

    name: str
    resolution: int
    image: bpy.types.Image | None = None
    constant: tuple[float, float, float, float] | None = None


@dataclass
class MaterialBakeResult:
    """Material slotごとの中間Channelと、GLB再構築用の最終画像をまとめる。"""

    raw: dict[str, RawChannel] = field(default_factory=dict)
    images: dict[str, bpy.types.Image] = field(default_factory=dict)
    emission_strength: float = 1.0
    alpha_mode: str | None = None
    alpha_cutoff: float = 0.5
    detected_transmission: bool = False


@dataclass
class MaterialSlotWork:
    """元slot、作業コピーでの解析結果、生成済みMaterialを対応付ける。"""

    slot_index: int
    source_material: bpy.types.Material
    source_analysis: MaterialAnalysis
    result: MaterialBakeResult = field(default_factory=MaterialBakeResult)
    final_material: bpy.types.Material | None = None


@dataclass
class WorkObject:
    """元Objectと、Jobだけが変更してよい作業Objectを1対1で関連付ける。"""

    original: bpy.types.Object
    object: bpy.types.Object
    bake_uv_name: str
    slots: list[MaterialSlotWork]


def create_job_scene(
    scene: bpy.types.Scene,
    registry: TempDataRegistry,
) -> tuple[bpy.types.Scene, bpy.types.Collection]:
    """元Sceneへ一時Collectionだけを接続し、別SceneのDepsgraphを作らない。"""

    # 元Sceneを使うが、生成物は識別可能な一時Collectionへ閉じ込める。
    token = uuid.uuid4().hex[:8]
    collection = registry.track(bpy.data.collections.new(f"__SHADER_BAKE_GLB_COLLECTION_{token}"))
    scene.collection.children.link(collection)
    # Bakeに必要な設定だけを一時変更する。Job側のContextSnapshotが必ず復元する。
    scene.render.engine = "CYCLES"
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_depth = "8"
    scene.render.image_settings.color_mode = "RGBA"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    return scene, collection


def used_material_indices(mesh: bpy.types.Mesh) -> list[int]:
    # 未使用slotをベイク・検証・UI件数から除外するため、Face参照だけを集める。
    return sorted({polygon.material_index for polygon in mesh.polygons})


def _unique_uv_name(mesh: bpy.types.Mesh) -> str:
    # 元Meshの同名UVを壊さず、作業コピー内でのみ衝突しないBake UV名を決める。
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

    # 1. Modifier適用後の評価Meshを新規DataBlockとして取得する。
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

    # 2. 作業Objectへ評価時のworld transformを写し、元Objectのtransformには触れない。
    work_object = registry.track(bpy.data.objects.new(original.name, mesh))
    work_object.matrix_world = evaluated.matrix_world.copy()
    collection.objects.link(work_object)

    # 3. Material slotを作業専用コピーへ差し替え、参照元Node Treeの書換えを防ぐ。
    used = used_material_indices(mesh)
    source_slots: list[MaterialSlotWork] = []
    slot_count = max(len(original.material_slots), max(used, default=-1) + 1)
    # new_from_objectは元Material参照を保持するため、作業専用コピーへ置換する。
    mesh.materials.clear()
    for slot_index in range(slot_count):
        original_material = original.material_slots[slot_index].material if slot_index < len(original.material_slots) else None
        if original_material is None:
            # Faceが参照する空slotは、警告付きの既定PBR材質で出力可能にする。
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
        # Node Treeを含むMaterialコピーへ切り替えた後だけ、解析・評価用に変更できる。
        copied = registry.track(original_material.copy())
        copied.name = f"{original.name}__slot_{slot_index}__source"
        mesh.materials.append(copied)
        if slot_index in used:
            analysis = analyze_material(copied, original.name)
            # Node未使用、またはSurface未接続のMaterialはviewport値から最小PBRを再構築する。
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

    # 4. 元UVは残したまま、Bake用の空UV layerを追加する。展開は次工程で実行する。
    bake_uv_name = _unique_uv_name(mesh)
    source_active_render = next((layer.name for layer in mesh.uv_layers if layer.active_render), None)
    bake_uv = mesh.uv_layers.new(name=bake_uv_name, do_init=False)
    if source_active_render and mesh.uv_layers.get(source_active_render):
        mesh.uv_layers.get(source_active_render).active_render = True
        bake_uv.active_render = False
    return WorkObject(original, work_object, bake_uv_name, source_slots)


def unwrap_work_object(context: bpy.types.Context, scene: bpy.types.Scene, work: WorkObject) -> None:
    # 選択状態を局所化してSmart Projectを実行し、他ObjectのUVや選択を変更しない。
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
        # Edit Modeへの移行からObject Modeへの復帰までを同じoverrideで完結させる。
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
        # smart_projectの例外時も作業ObjectをEdit Modeのまま残さない。
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
    # Disk保存はせずJob所有の内部画像として作り、最終GLBへだけ内包させる。
    image = registry.track(
        bpy.data.images.new(name=name, width=resolution, height=resolution, alpha=alpha, float_buffer=float_buffer)
    )
    image.colorspace_settings.name = colorspace
    image.file_format = "PNG"
    return image


def _socket_default_rgba(socket: bpy.types.NodeSocket) -> tuple[float, float, float, float]:
    # Float/Colorの両方を、画像書込みに使うRGBA4要素へ揃える。
    value = socket.default_value
    if isinstance(value, (int, float)):
        scalar = float(value)
        return scalar, scalar, scalar, 1.0
    values = tuple(float(component) for component in value)
    return (values + (1.0, 1.0, 1.0, 1.0))[:4]


def _connect_or_copy(tree: bpy.types.NodeTree, source: bpy.types.NodeSocket, destination: bpy.types.NodeSocket) -> None:
    # 出力socket、接続済み入力、定数入力を同じ評価用Emission Nodeへ接続できるようにする。
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
    # 未接続の静的入力はbpy.ops.object.bakeを呼ばず、同じ値のChannelとして直接記録する。
    analysis = slot.source_analysis
    principled = analysis.principled_node
    if analysis.strategy == "FALLBACK":
        # 外観近似ではMetallicだけを既定値で確定し、他Channelは実際の評価結果を焼く。
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
    # 元のSurface接続を一時的にEmissionへ差し替え、指定入力を色としてベイク可能にする。
    tree = material.node_tree
    output = analysis.output_node
    principled = analysis.principled_node
    if output is None or principled is None:
        raise BakeFailure(f"{material.name}: PBR評価入口がありません")
    # 作業用Materialコピーだけを変更するため、元Materialの見た目・Node Treeは変わらない。
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
    # 他slotのactive Imageを安全な1px画像へ退避するためだけの、一時評価用Materialを作る。
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
    # foreach_getでPythonオブジェクト生成を避け、結合処理用の連続float配列を取得する。
    values = array("f", [0.0]) * (image.size[0] * image.size[1] * 4)
    image.pixels.foreach_get(values)
    return values


def _validate_raw(raw: RawChannel) -> None:
    # ベイク結果を最終8bit画像に落とす前に、寸法・有限値・Channelごとの値域を検査する。
    values: Iterable[float]
    if raw.constant is not None:
        values = raw.constant
    elif raw.image is not None:
        if tuple(raw.image.size) != (raw.resolution, raw.resolution):
            raise BakeFailure(f"{raw.name}: 画像寸法が{raw.resolution}×{raw.resolution}ではありません")
        values = _read_pixels(raw.image)
    else:
        raise BakeFailure(f"{raw.name}: Bake結果がありません")

    # Emissiveだけは強度を別factorへ分離するため、ここではHDR値を保持する。
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
    # Bake失敗時でもGLB構造を完成させるため、Channelの意味に沿った安全な既定値を返す。
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

    # 1. 定数入力ならGPU Bakeを省略し、同じ値を直接中間結果へ入れる。
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
        # 2. Raw画像、他slot退避画像、評価用NodeをすべてJob所有として作成する。
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
            # PBR ChannelはEmission評価に差し替えて、照明やBSDFの影響を除いた入力値を焼く。
            if analysis.output_node is None or analysis.principled_node is None:
                raise BakeFailure("PBR評価入口がありません")
            original_surface_source = analysis.output_node.inputs["Surface"].links[0].from_socket
            if channel == "Coat Normal":
                # BlenderのNORMAL Bakeは通常Normal入力を見るため、Coat Normalを一時的に転送する。
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
        # 3. 対象slotだけにRaw画像をactive Imageとして割り当てる。
        for node in evaluation.node_tree.nodes:
            node.select = False
        target_node = evaluation.node_tree.nodes.new("ShaderNodeTexImage")
        target_node.name = "__SHADER_BAKE_GLB_TARGET"
        target_node.image = raw_image
        target_node.select = True
        evaluation.node_tree.nodes.active = target_node

        bake_object = work.object
        mesh = bake_object.data
        # 同一Meshの他slotが前回のactive Imageへ書き込まないよう、discard画像へ退避する。
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

        # 4. Bake対象を作業Objectだけに絞り、Scene内の元Objectや他の作業Objectを巻き込まない。
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
        # 5. 近似/UNLITはBlender標準Bake種別、PBRは入力値をEmissionとして評価する。
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
        # 個別Channelの失敗はJob全体を破棄せず、警告付き既定値でGLBを完成させる。
        fallback = _channel_default(slot, channel, resolution)
        slot.result.raw[channel] = fallback
        if warn:
            warn(work.original.name, slot.source_material.name, f"{channel}のBakeに失敗したため既定値へ置換しました: {exc}")
        return fallback
    finally:
        # 6. 一時的に変更した作業Materialのlink、active Image Nodeを必ず元へ戻す。
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
    # 以降の結合ループは画像と定数を同じsample関数で扱える形にそろえる。
    return (_read_pixels(raw.image), None) if raw.image is not None else (None, raw.constant)


def _sample(values: array | None, constant: tuple[float, float, float, float] | None, pixel: int, component: int) -> float:
    # 画像ならRGBA配列、定数なら4要素tupleから同じ座標の値を読む。
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
    # 最終画像はGLBの8bit PNG要件に合わせ、float_bufferではなく通常画像として確定する。
    image = _new_image(registry, name, resolution, float_buffer=False, colorspace=colorspace)
    image.pixels.foreach_set(pixels)
    image.update()
    # Blender 5.1.1のglTF exporterが複数のdirty generated imageを並列変換すると
    # 不正参照するため、完成画像を先にpackして不変のPNG sourceへ確定する。
    image.pack()
    return image


def _release_raw(registry: TempDataRegistry, result: MaterialBakeResult, *names: str) -> None:
    # raw辞書からの解放はメモリ保持を短縮する。DataBlock自体の削除はregistryの依存順cleanupに任せる。
    for name in names:
        raw = result.raw.pop(name, None)
        # 評価MaterialのImage Textureが参照している間は削除しない。
        # Job終了後、ObjectとMaterialを先に外してからregistry.cleanupが削除する。
        _ = raw


def _clamp01(value: float) -> float:
    # GLBの非HDR Texture Channelへ書く前の共通クランプ。
    return min(1.0, max(0.0, float(value)))


def _combine_scalar_image(
    slot: MaterialSlotWork,
    registry: TempDataRegistry,
    resolution: int,
    raw_name: str,
    image_name: str,
    stem: str,
) -> None:
    # 1成分ChannelをRGB同値のPNGへ変換し、拡張Material入力へ接続可能にする。
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

    # 1. Core画像を優先して結合し、完成済み画像は再生成しない。
    result = slot.result
    count = resolution * resolution
    fallback = slot.source_analysis.strategy == "FALLBACK"
    base_factor = slot.source_analysis.base_color_factor if slot.source_analysis.strategy == "PBR" else (1.0, 1.0, 1.0, 1.0)
    metallic_factor = slot.source_analysis.metallic_factor if slot.source_analysis.strategy == "PBR" else 1.0
    roughness_factor = slot.source_analysis.roughness_factor if slot.source_analysis.strategy == "PBR" else 1.0

    def remove_factor(value: float, factor: float) -> float:
        # factor=0ではtexture値が外観へ寄与しないため、白へ正規化する。
        return 1.0 if factor <= 1.0e-6 else _clamp01(value / factor)

    # Base/AlphaはFallbackでの透過推定にも使うため、Transmissionも揃うまで待つ。
    base_ready = {"Base Color", "Alpha"}.issubset(result.raw) and (not fallback or "Transmission" in result.raw)
    if "base_alpha" not in result.images and base_ready:
        base_values, base_constant = _raw_values(result.raw["Base Color"])
        alpha_values, alpha_constant = _raw_values(result.raw["Alpha"])
        transmission_values, transmission_constant = _raw_values(result.raw["Transmission"]) if fallback else (None, None)
        normal_values, normal_constant = _raw_values(result.raw["Normal"]) if "Normal" in result.raw else (None, (0.5, 0.5, 1.0, 1.0))
        # 正規化不能な背景画素を除外し、実際の形状部分のAlphaからmodeを推定する。
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
            # 近似材質はBakeしたAlpha分布からGLBのalphaModeを選択する。
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

    # Occlusion / Roughness / MetallicはglTF標準のR/G/Bパッキングで1枚へまとめる。
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

    # Normalは色変換せずNon-ColorのRGB値をそのまま最終画像へ移す。
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

    # HDR Emissiveは画像を0..1へ正規化し、強度をMaterial factorとして保持する。
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

    # Transmissionは単一強度として保存し、非ゼロかどうかを拡張出力の判断にも使う。
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

    # 残る1成分拡張Channelは同じスカラー変換で処理する。
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

    # 色/法線系の拡張Channelは各Channelに必要な色空間を指定して画像化する。
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

    # Sheen tintはweightを色へ乗算して、標準exporterが読む1枚のTextureにする。
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
    # 再構築Materialで使う画像Nodeは命名を固定し、デバッグ時の追跡を容易にする。
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

    # 1. 変換方式と有効なKHR拡張から、書き出し前に必要な最終画像を確定する。
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

    # 2. 元Materialと一切のNodeを共有しない、exporter専用Materialを新規作成する。
    material = registry.track(bpy.data.materials.new(f"{object_name}__slot_{slot.slot_index}__baked"))
    material.use_nodes = True
    material.use_backface_culling = slot.source_material.use_backface_culling
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    if slot.source_analysis.strategy == "UNLIT":
        # UNLITはEmission相当のSurfaceとAlphaだけで構成し、PBR入力を追加しない。
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

    # 3. Base/AlphaとORMを接続し、元の定数factorは必要な場合だけNodeで掛け戻す。
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
    # Alphaの閾値はGLBのMASK、連続値はBLENDとしてBlender側の表示設定にも反映する。
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

    # OcclusionとVolumeの補助入力は、標準exporterが認識するglTF Material Output Groupを経由する。
    settings = None
    if "occlusion" in extensions or "volume" in extensions:
        settings = _gltf_settings_node(tree, registry)
    if "occlusion" in extensions and settings is not None:
        tree.links.new(separate.outputs["Red"], settings.inputs["Occlusion"])

    # 4. Normal、Emissive、各KHR拡張を、すべてBake UVの画像だけから再接続する。
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

    # KHR拡張は解析で有効と判定されたものだけをMaterialへ追加する。
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
        # Volumeの動的Node網は再現せず、未接続の色・密度だけを安全に転記する。
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
    # 最終GLBではBake UVだけをTEXCOORD_0にし、元UVが余分なTEXCOORDとして出ないようにする。
    for layer in list(mesh.uv_layers):
        if layer != bake_uv:
            mesh.uv_layers.remove(layer)
    bake_uv.name = BAKE_UV_NAME
    bake_uv.active = True
    bake_uv.active_render = True
    # 使用slotだけを生成済みMaterialへ置換する。未使用slotはexporterが参照しない。
    for slot in work.slots:
        if slot.final_material is None:
            raise BakeFailure(f"{work.original.name}: Material再構築が完了していません")
        mesh.materials[slot.slot_index] = slot.final_material
