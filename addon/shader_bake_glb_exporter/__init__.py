"""Shader Bake GLB ExporterのBlender Extension entrypoint。"""

bl_info = {
    "name": "Shader Bake GLB Exporter",
    "author": "AokiMotohide",
    "version": (1, 0, 0),
    "blender": (5, 1, 1),
    "location": "3D View > Sidebar > GLB Bake Export",
    "description": "接続済みシェーダーをPBRテクスチャへベイクして選択MeshをGLBへ書き出します",
    "category": "Import-Export",
    "license": "GPL-3.0-or-later",
}

from . import properties, ui


def register() -> None:
    """Propertyを先に登録し、UIから参照できる状態にする。"""

    properties.register()
    ui.register()


def unregister() -> None:
    """実行中Jobを停止してからPropertyを解除する。"""

    ui.unregister()
    properties.unregister()
