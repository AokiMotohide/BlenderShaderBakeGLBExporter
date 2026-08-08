"""Principled材質を標準Metallic-Roughnessへ変換できるか検証する。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import bpy


EPSILON = 1.0e-6


@dataclass(frozen=True)
class AlphaContract:
    """Alphaの公開契約。source_socketはClip比較前の値を指す。"""

    mode: str
    cutoff: float
    source_socket: bpy.types.NodeSocket | None


@dataclass(frozen=True)
class MaterialAnalysis:
    """Bakeが利用する検証済みMaterial情報。"""

    material: bpy.types.Material
    output_node: bpy.types.Node
    principled_node: bpy.types.Node
    alpha: AlphaContract
    ior: float


@dataclass(frozen=True)
class MaterialValidationError(Exception):
    """Object名、Material名、拒否理由をUIへ渡す構造化エラー。"""

    object_name: str
    material_name: str
    reason: str

    def __str__(self) -> str:
        return f"{self.object_name} / {self.material_name}: {self.reason}"


def _socket(node: bpy.types.Node, name: str) -> bpy.types.NodeSocket:
    socket = node.inputs.get(name)
    if socket is None:
        raise ValueError(f"Principled BSDFに{name}入力がありません")
    return socket


def _is_finite_value(value: object) -> bool:
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    try:
        return all(math.isfinite(float(component)) for component in value)  # type: ignore[arg-type]
    except TypeError:
        return True


def _constant(socket: bpy.types.NodeSocket) -> float:
    return float(socket.default_value)


def _linked_source(socket: bpy.types.NodeSocket) -> bpy.types.NodeSocket | None:
    return socket.links[0].from_socket if socket.is_linked and socket.links else None


def _all_nested_nodes(node_tree: bpy.types.NodeTree) -> Iterable[bpy.types.Node]:
    """Node Group内も含める。Group内の未使用ノードも安全側で検査する。"""

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
    """Principled入力から到達できる上流Nodeと利用Output名を返す。"""

    found: list[tuple[bpy.types.Node, str]] = []
    visited: set[tuple[int, str, tuple[int, ...]]] = set()

    def matching_socket(sockets, template: bpy.types.NodeSocket, fallback_index: int):
        for socket in sockets:
            if socket.identifier == template.identifier or socket.name == template.name:
                return socket
        return sockets[fallback_index] if fallback_index < len(sockets) else None

    def walk_input(input_socket: bpy.types.NodeSocket, group_stack: tuple[bpy.types.Node, ...]) -> None:
        if not input_socket.is_linked:
            return
        for link in input_socket.links:
            node = link.from_node
            output_socket = link.from_socket
            key = (
                node.as_pointer(),
                output_socket.identifier or output_socket.name,
                tuple(group.as_pointer() for group in group_stack),
            )
            if key in visited:
                continue
            visited.add(key)
            found.append((node, output_socket.name))

            nested = getattr(node, "node_tree", None)
            if node.bl_idname == "ShaderNodeGroup" and nested is not None:
                group_outputs = [item for item in nested.nodes if item.bl_idname == "NodeGroupOutput"]
                active_output = next((item for item in group_outputs if item.is_active_output), None)
                group_output = active_output or (group_outputs[0] if group_outputs else None)
                if group_output is not None:
                    index = list(node.outputs).index(output_socket)
                    internal_input = matching_socket(group_output.inputs, output_socket, index)
                    if internal_input is not None:
                        walk_input(internal_input, group_stack + (node,))
                continue

            if node.bl_idname == "NodeGroupInput" and group_stack:
                outer_group = group_stack[-1]
                index = list(node.outputs).index(output_socket)
                external_input = matching_socket(outer_group.inputs, output_socket, index)
                if external_input is not None:
                    walk_input(external_input, group_stack[:-1])
                continue

            for upstream_input in node.inputs:
                walk_input(upstream_input, group_stack)

    for input_socket in principled.inputs:
        walk_input(input_socket, ())
    return found


def _alpha_contract(material: bpy.types.Material, principled: bpy.types.Node) -> AlphaContract:
    alpha = _socket(principled, "Alpha")
    if not alpha.is_linked:
        value = _constant(alpha)
        if not math.isfinite(value):
            raise ValueError("Alpha定数が有限値ではありません")
        if abs(value - 1.0) <= EPSILON:
            return AlphaContract("OPAQUE", 0.5, None)
        raise ValueError("Alpha BlendまたはAlpha Hashed相当の定数Alphaは未対応です")

    source = alpha.links[0].from_node
    fallback = float(getattr(material, "alpha_threshold", 0.5))
    if not math.isfinite(fallback):
        fallback = 0.5

    if source.bl_idname == "ShaderNodeMath" and source.operation == "ROUND":
        return AlphaContract("CLIP", 0.5, _linked_source(source.inputs[0]) or source.inputs[0])

    if source.bl_idname == "ShaderNodeMath" and source.operation == "GREATER_THAN":
        threshold = source.inputs[1]
        if threshold.is_linked:
            raise ValueError("Alpha Clipのしきい値がProceduralです")
        cutoff = _constant(threshold)
        if not math.isfinite(cutoff):
            cutoff = fallback
        return AlphaContract("CLIP", cutoff, _linked_source(source.inputs[0]) or source.inputs[0])

    if source.bl_idname == "ShaderNodeMath" and source.operation == "LESS_THAN":
        left, right = source.inputs[0], source.inputs[1]
        if not left.is_linked and right.is_linked:
            cutoff = _constant(left)
            return AlphaContract("CLIP", cutoff if math.isfinite(cutoff) else fallback, _linked_source(right) or right)

    if source.bl_idname == "ShaderNodeMath" and source.operation == "SUBTRACT":
        left, right = source.inputs[0], source.inputs[1]
        compare = right.links[0].from_node if right.is_linked else None
        if abs(_constant(left) - 1.0) <= EPSILON and compare and compare.bl_idname == "ShaderNodeMath":
            if compare.operation == "LESS_THAN" and compare.inputs[1].is_linked is False:
                cutoff = _constant(compare.inputs[1])
                return AlphaContract("CLIP", cutoff if math.isfinite(cutoff) else fallback, _linked_source(compare.inputs[0]) or compare.inputs[0])

    raise ValueError("Alpha BlendまたはAlpha Hashed相当の連続Alphaは未対応です")


def _validate_principled_contract(principled: bpy.types.Node) -> None:
    zero_only = (
        "Subsurface Weight",
        "Coat Weight",
        "Sheen Weight",
        "Anisotropic",
        "Diffuse Roughness",
        "Thin Film Thickness",
    )
    for name in zero_only:
        socket = _socket(principled, name)
        if socket.is_linked or abs(_constant(socket)) > EPSILON:
            raise ValueError(f"{name}が有効な材質は未対応です")

    specular_level = _socket(principled, "Specular IOR Level")
    if specular_level.is_linked or abs(_constant(specular_level) - 0.5) > EPSILON:
        raise ValueError("非標準のSpecular IOR Levelは未対応です")

    specular_tint = _socket(principled, "Specular Tint")
    tint = tuple(float(value) for value in specular_tint.default_value)
    if specular_tint.is_linked or any(abs(value - 1.0) > EPSILON for value in tint[:3]):
        raise ValueError("Specular Tintは未対応です")

    ior = _socket(principled, "IOR")
    if ior.is_linked:
        raise ValueError("Procedural IORは未対応です")
    if not math.isfinite(_constant(ior)) or _constant(ior) < 1.0:
        raise ValueError("IOR定数は1以上の有限値が必要です")

    for name in (
        "Base Color",
        "Metallic",
        "Roughness",
        "IOR",
        "Alpha",
        "Emission Color",
        "Emission Strength",
        "Transmission Weight",
    ):
        socket = _socket(principled, name)
        if not socket.is_linked and not _is_finite_value(socket.default_value):
            raise ValueError(f"{name}定数が有限値ではありません")


def _validate_upstream_nodes(material: bpy.types.Material, principled: bpy.types.Node) -> None:
    forbidden_ids = {
        "ShaderNodeMixShader": "Mix Shader",
        "ShaderNodeAddShader": "Add Shader",
        "ShaderNodeBsdfToon": "Toon BSDF",
        "ShaderNodeLayerWeight": "Layer Weight",
        "ShaderNodeCameraData": "Camera Data",
        "ShaderNodeLightPath": "Light Path",
        "ShaderNodeFresnel": "Fresnel",
        "ShaderNodeScript": "OSL Script",
    }
    for node, output_name in _reachable_nodes(principled):
        label = forbidden_ids.get(node.bl_idname)
        if label:
            raise ValueError(f"{label}は未対応です")
        if node.bl_idname == "ShaderNodeTexCoord" and output_name in {"Camera", "Window", "Reflection", "*"}:
            raise ValueError("視線依存のTexture Coordinateは未対応です")
        if node.bl_idname == "ShaderNodeVectorTransform":
            if getattr(node, "convert_from", "") == "CAMERA" or getattr(node, "convert_to", "") == "CAMERA":
                raise ValueError("Camera空間のVector Transformは未対応です")

    # Group内は共有NodeTreeなので変更しない。危険Nodeが存在するGroupは保守的に拒否する。
    for node in _all_nested_nodes(material.node_tree):
        if node.bl_idname == "ShaderNodeScript":
            raise ValueError("OSL Scriptは未対応です")


def analyze_material(material: bpy.types.Material, object_name: str = "") -> MaterialAnalysis:
    """材質を検証し、Bakeに必要な安定した入口を返す。"""

    material_name = material.name if material else "<なし>"
    try:
        if material is None:
            raise ValueError("Materialが割り当てられていません")
        if not material.use_nodes or material.node_tree is None:
            raise ValueError("Node Materialではありません")
        if getattr(material, "surface_render_method", "DITHERED") == "BLENDED":
            raise ValueError("Alpha Blendは未対応です")

        outputs = [
            node
            for node in material.node_tree.nodes
            if node.bl_idname == "ShaderNodeOutputMaterial" and node.is_active_output
        ]
        if len(outputs) != 1:
            raise ValueError("Active Material Outputが1つ必要です")
        output = outputs[0]
        surface = output.inputs.get("Surface")
        if surface is None or len(surface.links) != 1:
            raise ValueError("Material OutputのSurfaceへ単一Shaderを接続してください")
        principled = surface.links[0].from_node
        if principled.bl_idname != "ShaderNodeBsdfPrincipled":
            raise ValueError("SurfaceへPrincipled BSDFを直接接続してください")
        if output.inputs.get("Volume") and output.inputs["Volume"].is_linked:
            raise ValueError("Volume接続は未対応です")
        if output.inputs.get("Displacement") and output.inputs["Displacement"].is_linked:
            raise ValueError("Shader Displacementは未対応です")

        _validate_principled_contract(principled)
        _validate_upstream_nodes(material, principled)
        alpha = _alpha_contract(material, principled)
        return MaterialAnalysis(material, output, principled, alpha, _constant(_socket(principled, "IOR")))
    except ValueError as exc:
        raise MaterialValidationError(object_name or "<Object>", material_name, str(exc)) from exc


def find_principled(material: bpy.types.Material) -> tuple[bpy.types.Node, bpy.types.Node]:
    """検証済みMaterialコピーからActive OutputとPrincipledを再取得する。"""

    analysis = analyze_material(material, "<作業用>")
    return analysis.output_node, analysis.principled_node
