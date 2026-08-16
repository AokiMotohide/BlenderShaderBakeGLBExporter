"""SidebarとModal Jobが共有する一時Property。"""

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty


class SHADERBAKEGLB_PG_ErrorItem(bpy.types.PropertyGroup):
    # Jobの診断をUI CollectionPropertyへ転記するための最小単位。
    message: StringProperty(name="Error")


class SHADERBAKEGLB_PG_Settings(bpy.types.PropertyGroup):
    """WindowManagerに置き、元Sceneを保存時に汚さない設定と状態。"""

    # WindowManager所有のため、BlendファイルのScene設定や元Objectを変更しない。
    output_path: StringProperty(name="出力GLBパス", subtype="FILE_PATH")
    resolution: EnumProperty(
        name="Texture Resolution",
        items=(("512", "512", "512 × 512"), ("1024", "1024", "1024 × 1024"), ("2048", "2048", "2048 × 2048")),
        default="1024",
    )
    # 以下の非表示Propertyは、Modal Jobの状態をSidebarに表示するためだけに使う。
    # 実行中の真の状態はBakeJobが所有し、UIはそのスナップショットを受け取る。
    is_running: BoolProperty(default=False, options={"HIDDEN"})
    cancel_requested: BoolProperty(default=False, options={"HIDDEN"})
    progress: FloatProperty(default=0.0, min=0.0, max=1.0, subtype="FACTOR", options={"HIDDEN"})
    completed_units: IntProperty(default=0, min=0, options={"HIDDEN"})
    total_units: IntProperty(default=0, min=0, options={"HIDDEN"})
    current_object: StringProperty(default="", options={"HIDDEN"})
    current_material: StringProperty(default="", options={"HIDDEN"})
    current_phase: StringProperty(default="", options={"HIDDEN"})
    completed_path: StringProperty(default="", options={"HIDDEN"})
    errors: CollectionProperty(type=SHADERBAKEGLB_PG_ErrorItem)
    warnings: CollectionProperty(type=SHADERBAKEGLB_PG_ErrorItem)


CLASSES = (SHADERBAKEGLB_PG_ErrorItem, SHADERBAKEGLB_PG_Settings)


# BlenderのRNAへ登録してから、WindowManagerの専用領域を追加する。
def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.shader_bake_glb = PointerProperty(type=SHADERBAKEGLB_PG_Settings)


# Propertyを先に外して参照不能にし、登録順の逆順で型を解除する。
def unregister() -> None:
    if hasattr(bpy.types.WindowManager, "shader_bake_glb"):
        del bpy.types.WindowManager.shader_bake_glb
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
