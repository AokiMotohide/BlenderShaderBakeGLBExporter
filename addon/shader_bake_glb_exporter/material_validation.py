"""Materialを完全変換または外観近似へ分類する。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import bpy


EPSILON = 1.0e-6


@dataclass(frozen=True)
class AlphaContract:
    """AlphaのGLB契約。source_socketはBakeで評価する値を指す。"""

    mode: str
    cutoff: float
    source_socket: bpy.types.NodeSocket | None


@dataclass(frozen=True)
class MaterialAnalysis:
    """Materialの変換方式と、完全変換で使用する入力。"""

    material: bpy.types.Material
    output_node: bpy.types.Node | None
    principled_node: bpy.types.Node | None
    alpha: AlphaContract
    ior: float
    strategy: str
    fallback_reasons: tuple[str, ...]
    active_extensions: frozenset[str]
    occlusion_socket: bpy.types.NodeSocket | None = None
    thickness_socket: bpy.types.NodeSocket | None = None
    volume_node: bpy.types.Node | None = None
    base_color_factor: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    metallic_factor: float = 1.0
    roughness_factor: float = 1.0
    emission_color_factor: tuple[float, float, float] = (1.0, 1.0, 1.0)
    emission_strength_factor: float = 1.0


@dataclass(frozen=True)
class MaterialValidationError(Exception):
    """Object名、Material名、理由をUIへ渡す診断。"""

    object_name: str
    material_name: str
    reason: str

    def __str__(self) -> str:
        return f"{self.object_name} / {self.material_name}: {self.reason}"


def _socket(node: bpy.types.Node, name: str) -> bpy.types.NodeSocket:
    # Blenderのバージョン差や壊れたNode Treeを、後段の属性エラーではなく診断へ変換する。
    socket = node.inputs.get(name)
    if socket is None:
        raise ValueError(f"Principled BSDFに{name}入力がありません")
    return socket


def _constant(socket: bpy.types.NodeSocket) -> float:
    return float(socket.default_value)


def _linked_source(socket: bpy.types.NodeSocket) -> bpy.types.NodeSocket | None:
    # 複数linkがあり得るRNAでも、材質解析は有効な先頭linkを評価入口として扱う。
    return socket.links[0].from_socket if socket.is_linked and socket.links else None


def _finite_or(value: float, fallback: float) -> float:
    return value if math.isfinite(value) else fallback


def _as_tuple(value: object) -> tuple[float, ...]:
    if isinstance(value, (int, float)):
        return (float(value),)
    try:
        return tuple(float(component) for component in value)  # type: ignore[arg-type]
    except TypeError:
        return ()


def _socket_active(socket: bpy.types.NodeSocket, neutral: float | tuple[float, ...]) -> bool:
    # 接続済みは値を静的に判定できないため常に有効とみなす。
    if socket.is_linked:
        return True
    actual = _as_tuple(socket.default_value)
    expected = (float(neutral),) if isinstance(neutral, (int, float)) else tuple(float(v) for v in neutral)
    return len(actual) < len(expected) or any(abs(actual[i] - expected[i]) > EPSILON for i in range(len(expected)))


def _all_nested_nodes(node_tree: bpy.types.NodeTree) -> Iterable[bpy.types.Node]:
    """Node Group内も含め、循環参照を避けてNodeを列挙する。"""

    visited: set[int] = set()

    def visit(tree: bpy.types.NodeTree) -> Iterable[bpy.types.Node]:
        pointer = tree.as_pointer()
        if pointer in visited:
            return
        visited.add(pointer)
        for node in tree.nodes:
            yield node
            nested = getattr(node, "node_tree", None)
            if nested is not None:
                yield from visit(nested)

    yield from visit(node_tree)


def _reachable_nodes(principled: bpy.types.Node) -> list[tuple[bpy.types.Node, str]]:
    """Principled入力から到達できるNodeと利用Output名を返す。"""

    found: list[tuple[bpy.types.Node, str]] = []
    visited: set[tuple[int, str, tuple[int, ...]]] = set()

    def matching_socket(sockets, template: bpy.types.NodeSocket, fallback_index: int):
        # Group境界ではidentifier優先で対応付け、名称変更時のみ配列順へ後退する。
        for socket in sockets:
            if socket.identifier == template.identifier or socket.name == template.name:
                return socket
        return sockets[fallback_index] if fallback_index < len(sockets) else None

    def walk_input(input_socket: bpy.types.NodeSocket, group_stack: tuple[bpy.types.Node, ...]) -> None:
        # Group Input/Outputをまたいで上流へ戻り、視線依存Nodeの見落としを防ぐ。
        if not input_socket.is_linked:
            return
        for link in input_socket.links:
            node = link.from_node
            output_socket = link.from_socket
            key = (node.as_pointer(), output_socket.identifier or output_socket.name, tuple(n.as_pointer() for n in group_stack))
            if key in visited:
                continue
            visited.add(key)
            found.append((node, output_socket.name))
            nested = getattr(node, "node_tree", None)
            if node.bl_idname == "ShaderNodeGroup" and nested is not None:
                outputs = [item for item in nested.nodes if item.bl_idname == "NodeGroupOutput"]
                group_output = next((item for item in outputs if item.is_active_output), None) or (outputs[0] if outputs else None)
                if group_output is not None:
                    index = list(node.outputs).index(output_socket)
                    internal = matching_socket(group_output.inputs, output_socket, index)
                    if internal is not None:
                        walk_input(internal, group_stack + (node,))
                continue
            if node.bl_idname == "NodeGroupInput" and group_stack:
                outer = group_stack[-1]
                index = list(node.outputs).index(output_socket)
                external = matching_socket(outer.inputs, output_socket, index)
                if external is not None:
                    walk_input(external, group_stack[:-1])
                continue
            for upstream in node.inputs:
                walk_input(upstream, group_stack)

    for socket in principled.inputs:
        walk_input(socket, ())
    return found


def _gltf_socket(material: bpy.types.Material, name: str) -> bpy.types.NodeSocket | None:
    # glTF Material Output Groupは標準exporter向け補助入力で、通常のPrincipled入力とは別に扱う。
    if material.node_tree is None:
        return None
    for node in material.node_tree.nodes:
        nested = getattr(node, "node_tree", None)
        if node.bl_idname == "ShaderNodeGroup" and nested is not None and nested.name.lower().startswith("gltf material output"):
            return node.inputs.get(name)
    return None


def _alpha_contract(principled: bpy.types.Node) -> AlphaContract:
    # Blender Node Treeの代表的な閾値形を、GLBのOPAQUE/MASK/BLEND契約へ落とし込む。
    alpha = _socket(principled, "Alpha")
    if not alpha.is_linked:
        value = _finite_or(_constant(alpha), 1.0)
        if abs(value - 1.0) <= EPSILON:
            return AlphaContract("OPAQUE", 0.5, None)
        return AlphaContract("BLEND", 0.5, alpha)

    # ROUND、比較Node、反転比較はMASKに再現できる。その他の動的値はBLENDとして保持する。
    source = alpha.links[0].from_node
    if source.bl_idname == "ShaderNodeMath" and source.operation == "ROUND":
        return AlphaContract("CLIP", 0.5, _linked_source(source.inputs[0]) or source.inputs[0])
    if source.bl_idname == "ShaderNodeMath" and source.operation == "GREATER_THAN":
        threshold = source.inputs[1]
        if threshold.is_linked:
            return AlphaContract("CLIP", 0.5, source.outputs[0])
        cutoff = min(1.0, max(0.0, _finite_or(_constant(threshold), 0.5)))
        return AlphaContract("CLIP", cutoff, _linked_source(source.inputs[0]) or source.inputs[0])
    if source.bl_idname == "ShaderNodeMath" and source.operation == "LESS_THAN":
        left, right = source.inputs[0], source.inputs[1]
        if not left.is_linked and right.is_linked:
            cutoff = min(1.0, max(0.0, _finite_or(_constant(left), 0.5)))
            return AlphaContract("CLIP", cutoff, _linked_source(right) or right)
    if source.bl_idname == "ShaderNodeMath" and source.operation == "SUBTRACT":
        left, right = source.inputs[0], source.inputs[1]
        compare = right.links[0].from_node if right.is_linked else None
        if abs(_constant(left) - 1.0) <= EPSILON and compare and compare.bl_idname == "ShaderNodeMath":
            if compare.operation == "LESS_THAN" and not compare.inputs[1].is_linked:
                cutoff = min(1.0, max(0.0, _finite_or(_constant(compare.inputs[1]), 0.5)))
                return AlphaContract("CLIP", cutoff, _linked_source(compare.inputs[0]) or compare.inputs[0])
    return AlphaContract("BLEND", 0.5, alpha.links[0].from_socket)


def _fallback_analysis(material: bpy.types.Material, reasons: Iterable[str], output: bpy.types.Node | None = None) -> MaterialAnalysis:
    # 変換を中止せず、viewport色を使う最小限の外観近似へ明示的に切り替える。
    color = tuple(float(v) for v in material.diffuse_color)
    alpha = AlphaContract("OPAQUE" if len(color) < 4 or color[3] >= 1.0 - EPSILON else "BLEND", 0.5, None)
    return MaterialAnalysis(material, output, None, alpha, 1.5, "FALLBACK", tuple(dict.fromkeys(reasons)), frozenset())


def analyze_material(material: bpy.types.Material, object_name: str = "") -> MaterialAnalysis:
    """拒否ではなく、完全変換可能か外観近似が必要かを判定する。"""

    if material is None:
        raise MaterialValidationError(object_name or "<Object>", "<なし>", "Materialがありません")
    if not material.use_nodes or material.node_tree is None:
        return _fallback_analysis(material, ("Node未使用Materialをviewport色で近似します",))

    # 1. 実際に評価されるSurface入口を1個だけ特定する。
    outputs = [node for node in material.node_tree.nodes if node.bl_idname == "ShaderNodeOutputMaterial" and node.is_active_output]
    if len(outputs) != 1:
        return _fallback_analysis(material, ("Active Material Outputを特定できないため近似します",))
    output = outputs[0]
    surface = output.inputs.get("Surface")
    if surface is None or len(surface.links) != 1:
        return _fallback_analysis(material, ("Surface接続がないためviewport色で近似します",), output)
    principled = surface.links[0].from_node
    if principled.bl_idname == "ShaderNodeEmission":
        return MaterialAnalysis(
            material,
            output,
            None,
            AlphaContract("OPAQUE", 0.5, None),
            1.5,
            "UNLIT",
            (),
            frozenset({"unlit"}),
        )
    if principled.bl_idname != "ShaderNodeBsdfPrincipled":
        return _fallback_analysis(material, (f"{principled.bl_label or principled.name}をPBRへ近似します",), output)

    # 2. GLB Core PBRへ完全変換できない入力を列挙し、必要なら外観近似へ切り替える。
    reasons: list[str] = []
    unsupported = (
        ("Weight", 1.0),
        ("Diffuse Roughness", 0.0),
        ("Subsurface Weight", 0.0),
        ("Thin Film Thickness", 0.0),
    )
    for name, neutral in unsupported:
        socket = principled.inputs.get(name)
        if socket is not None and _socket_active(socket, neutral):
            reasons.append(f"{name}をCore PBRへ近似します")
    coat = principled.inputs.get("Coat Weight")
    if coat is not None and _socket_active(coat, 0.0):
        coat_ior = principled.inputs.get("Coat IOR")
        coat_tint = principled.inputs.get("Coat Tint")
        if coat_ior is not None and _socket_active(coat_ior, 1.5):
            reasons.append("Coat IORを近似します")
        if coat_tint is not None and _socket_active(coat_tint, (1.0, 1.0, 1.0)):
            reasons.append("Coat Tintを近似します")
    if output.inputs.get("Displacement") and output.inputs["Displacement"].is_linked:
        reasons.append("Shader DisplacementはGLB材質へ保持できないため省略します")

    # 視点やレンダー条件に依存するNodeは、固定テクスチャへ焼くことを診断として残す。
    risky = {
        "ShaderNodeCameraData": "Camera Data",
        "ShaderNodeLightPath": "Light Path",
        "ShaderNodeLayerWeight": "Layer Weight",
        "ShaderNodeFresnel": "Fresnel",
        "ShaderNodeScript": "OSL Script",
    }
    for node, output_name in _reachable_nodes(principled):
        label = risky.get(node.bl_idname)
        if label:
            reasons.append(f"{label}の視線依存結果を固定テクスチャへ近似します")
        if node.bl_idname == "ShaderNodeTexCoord" and output_name in {"Camera", "Window", "Reflection"}:
            reasons.append("視線依存Texture Coordinateを固定テクスチャへ近似します")
    if any(node.bl_idname == "ShaderNodeScript" for node in _all_nested_nodes(material.node_tree)):
        reasons.append("OSL Scriptは評価失敗時に既定値へ置換します")

    # 3. PBRとして保持可能な係数と拡張機能を抽出する。
    ior_socket = _socket(principled, "IOR")
    if ior_socket.is_linked:
        reasons.append("Procedural IORはテクスチャ化できないため近似します")
        ior = 1.5
    else:
        ior = _finite_or(_constant(ior_socket), 1.5)
        if ior < 1.0:
            ior = 1.5
            reasons.append("IORを1.5へ補正します")

    # 中立値と異なる入力だけを拡張として出力し、不要なGLB拡張を増やさない。
    extensions: set[str] = set()
    if _socket_active(_socket(principled, "Transmission Weight"), 0.0):
        extensions.add("transmission")
    if _socket_active(_socket(principled, "Specular IOR Level"), 0.5) or _socket_active(_socket(principled, "Specular Tint"), (1.0, 1.0, 1.0)):
        extensions.add("specular")
    if coat is not None and _socket_active(coat, 0.0):
        extensions.add("clearcoat")
    if _socket_active(_socket(principled, "Sheen Weight"), 0.0):
        extensions.add("sheen")
    if _socket_active(_socket(principled, "Anisotropic"), 0.0):
        extensions.add("anisotropy")
    occlusion = _gltf_socket(material, "Occlusion")
    if occlusion is not None and _socket_active(occlusion, 1.0):
        extensions.add("occlusion")
    thickness = _gltf_socket(material, "Thickness")
    volume_input = output.inputs.get("Volume")
    volume_node = volume_input.links[0].from_node if volume_input and volume_input.is_linked else None
    if thickness is not None and _socket_active(thickness, 0.0) and volume_node is not None and volume_node.bl_idname == "ShaderNodeVolumePrincipled":
        extensions.add("volume")
    elif volume_input and volume_input.is_linked:
        reasons.append("VolumeをCore PBR外観へ近似し、体積効果は省略します")

    # 4. 定数factorはテクスチャと二重に焼かず、再構築時に係数として掛け戻す。
    strategy = "FALLBACK" if reasons else "PBR"
    base_color_socket = _socket(principled, "Base Color")
    if base_color_socket.is_linked:
        base_color_factor = (1.0, 1.0, 1.0, 1.0)
    else:
        base_default = _as_tuple(base_color_socket.default_value)
        alpha_socket = _socket(principled, "Alpha")
        alpha_factor = 1.0 if alpha_socket.is_linked else min(1.0, max(0.0, _finite_or(_constant(alpha_socket), 1.0)))
        base_color_factor = tuple(
            min(1.0, max(0.0, _finite_or(base_default[index], 1.0)))
            for index in range(3)
        ) + (alpha_factor,)
    metallic_socket = _socket(principled, "Metallic")
    metallic_factor = 1.0 if metallic_socket.is_linked else min(1.0, max(0.0, _finite_or(_constant(metallic_socket), 0.0)))
    roughness_socket = _socket(principled, "Roughness")
    roughness_factor = 1.0 if roughness_socket.is_linked else min(1.0, max(0.0, _finite_or(_constant(roughness_socket), 0.5)))
    emission_color_socket = _socket(principled, "Emission Color")
    emission_strength_socket = _socket(principled, "Emission Strength")
    if emission_color_socket.is_linked or emission_strength_socket.is_linked:
        emission_color_factor = (1.0, 1.0, 1.0)
        emission_strength_factor = 1.0
    else:
        emission_default = tuple(max(0.0, _finite_or(value, 0.0)) for value in _as_tuple(emission_color_socket.default_value)[:3])
        source_strength = max(0.0, _finite_or(_constant(emission_strength_socket), 1.0))
        if max(emission_default, default=0.0) * source_strength <= EPSILON:
            # ゼロ発光は黒Textureとして残し、標準exporterによるslot省略を避ける。
            emission_color_factor = (1.0, 1.0, 1.0)
            emission_strength_factor = 1.0
        else:
            color_scale = max(1.0, *emission_default)
            emission_color_factor = tuple(min(1.0, value / color_scale) for value in emission_default)
            emission_strength_factor = source_strength * color_scale
    return MaterialAnalysis(
        material,
        output,
        principled,
        _alpha_contract(principled),
        ior,
        strategy,
        tuple(dict.fromkeys(reasons)),
        frozenset(extensions),
        occlusion,
        thickness,
        volume_node,
        base_color_factor,
        metallic_factor,
        roughness_factor,
        emission_color_factor,
        emission_strength_factor,
    )


def find_principled(material: bpy.types.Material) -> tuple[bpy.types.Node, bpy.types.Node]:
    """完全変換可能なMaterialからActive OutputとPrincipledを取得する。"""

    analysis = analyze_material(material, "<作業用>")
    if analysis.output_node is None or analysis.principled_node is None:
        raise MaterialValidationError("<作業用>", material.name, "Principled BSDFを取得できません")
    return analysis.output_node, analysis.principled_node
