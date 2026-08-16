"""3D View SidebarとModal Operator。"""

from __future__ import annotations

from pathlib import Path

import bpy
from bpy.props import StringProperty
from bpy_extras.io_utils import ExportHelper

from .job import BakeJob, BakeJobConfig, JobStatus, detected_material_count, selected_mesh_objects


ACTIVE_JOB: BakeJob | None = None


# UI操作中のWindowManager設定へ集約してアクセスする。
def _settings(context: bpy.types.Context):
    return context.window_manager.shader_bake_glb


def _copy_job_state(context: bpy.types.Context, job: BakeJob) -> None:
    # JobはBlender Propertyを直接書き換えない。ここで表示専用状態へ一括転記する。
    settings = _settings(context)
    settings.is_running = job.status == JobStatus.RUNNING
    settings.cancel_requested = job.cancel_requested
    settings.progress = job.progress
    settings.completed_units = job.completed_units
    settings.total_units = job.total_units
    settings.current_object = job.current_object
    settings.current_material = job.current_material
    settings.current_phase = job.current_phase
    settings.errors.clear()
    for error in job.errors:
        item = settings.errors.add()
        item.message = str(error)
    settings.warnings.clear()
    for warning in job.warnings:
        item = settings.warnings.add()
        item.message = str(warning)


def ensure_glb_extension(filepath: str) -> str:
    """保存名に.glbがなければ、Blender標準exporterと同様に追加する。"""

    if not filepath or filepath.lower().endswith(".glb"):
        return filepath
    return filepath + ".glb"


def default_export_filepath(
    blend_filepath: str,
    active_mesh_name: str,
    previous_path: str,
    fallback_directory: str,
) -> str:
    """前回値、Blend名、Active Mesh名の順で保存候補を決める。"""

    if previous_path:
        return ensure_glb_extension(previous_path)
    if blend_filepath:
        source = Path(blend_filepath)
        return str(source.with_suffix(".glb"))
    stem = bpy.path.clean_name(active_mesh_name) if active_mesh_name else "export"
    stem = stem or "export"
    return str(Path(fallback_directory) / f"{stem}.glb")


class SHADERBAKEGLB_OT_ExportSelected(bpy.types.Operator, ExportHelper):
    """選択Meshを作業コピーでベイクし、検証済みGLBへ書き出す公開Operator。"""

    bl_idname = "shader_bake_glb.export_selected"
    bl_label = "GLBを書き出し"
    bl_options = {"REGISTER"}
    filename_ext = ".glb"
    filter_glob: StringProperty(default="*.glb", options={"HIDDEN"})
    filepath: StringProperty(subtype="FILE_PATH")

    _timer = None

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        # 同時に複数のJobを開始すると選択状態と一時DataBlockの所有権が競合する。
        return context.window_manager is not None and not _settings(context).is_running

    def check(self, _context: bpy.types.Context) -> bool:
        # ファイルブラウザでの入力中にも拡張子を正規化し、実行時との結果を揃える。
        normalized = ensure_glb_extension(self.filepath)
        changed = normalized != self.filepath
        self.filepath = normalized
        return changed

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event):
        # 保存候補は前回値を優先し、未指定時はBlend名、最後に選択Mesh名から決める。
        settings = _settings(context)
        active = context.view_layer.objects.active
        active_name = active.name if active is not None and active.type == "MESH" else ""
        fallback_directory = bpy.path.abspath("//")
        self.filepath = default_export_filepath(
            bpy.data.filepath,
            active_name,
            settings.output_path,
            fallback_directory,
        )
        return ExportHelper.invoke(self, context, event)

    def execute(self, context: bpy.types.Context):
        global ACTIVE_JOB
        # 前回の診断を消去してから、新しい1回分のJobを作成する。
        settings = _settings(context)
        settings.errors.clear()
        settings.warnings.clear()
        settings.completed_path = ""
        settings.progress = 0.0
        settings.cancel_requested = False
        requested = self.filepath or settings.output_path
        output_text = bpy.path.abspath(ensure_glb_extension(requested)) if requested else ""
        self.filepath = output_text
        settings.output_path = output_text
        config = BakeJobConfig(Path(output_text), int(settings.resolution))
        job = BakeJob(context, config)
        ACTIVE_JOB = job
        # startは検証と作業コピー作成だけを行う。重い各工程はModal timerで分割実行する。
        job.start()
        _copy_job_state(context, job)
        if job.status == JobStatus.FAILED:
            ACTIVE_JOB = None
            self.report({"ERROR"}, job.errors[0].reason if job.errors else "開始前検証に失敗しました")
            return {"CANCELLED"}
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event):
        global ACTIVE_JOB
        job = ACTIVE_JOB
        if job is None:
            self._finish_timer(context)
            return {"CANCELLED"}
        # ESCは即時にBlender Operatorを中断せず、現在の1工程の完了後に安全に停止する。
        if event.type == "ESC":
            job.request_cancel()
            _settings(context).cancel_requested = True
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        # timer 1回につきJobの1工程だけを進め、UIとキャンセル操作の応答性を保つ。
        status = job.advance()
        _copy_job_state(context, job)
        for area in context.screen.areas if context.screen else ():
            area.tag_redraw()
        if status == JobStatus.RUNNING:
            return {"RUNNING_MODAL"}

        self._finish_timer(context)
        ACTIVE_JOB = None
        if status == JobStatus.SUCCEEDED:
            settings = _settings(context)
            settings.completed_path = str(job.config.output_path.resolve())
            if job.warnings:
                self.report({"WARNING"}, f"GLB書き出しが完了しました（警告{len(job.warnings)}件）")
            else:
                self.report({"INFO"}, "GLB書き出しが完了しました")
            return {"FINISHED"}
        if status == JobStatus.CANCELLED:
            self.report({"WARNING"}, "GLB書き出しをキャンセルしました")
        else:
            self.report({"ERROR"}, job.errors[-1].reason if job.errors else "GLB書き出しに失敗しました")
        return {"CANCELLED"}

    def _finish_timer(self, context: bpy.types.Context) -> None:
        # 完了・失敗・キャンセルの各経路でtimerを1回だけ解放する。
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

    def cancel(self, context: bpy.types.Context) -> None:
        # BlenderがModal Operatorを強制取消した場合も、Jobに後始末を実行させる。
        global ACTIVE_JOB
        if ACTIVE_JOB is not None:
            ACTIVE_JOB.request_cancel()
            ACTIVE_JOB.advance()
            _copy_job_state(context, ACTIVE_JOB)
            ACTIVE_JOB = None
        self._finish_timer(context)


class SHADERBAKEGLB_OT_Cancel(bpy.types.Operator):
    """現在のBake完了後にJobを停止させる公開Operator。"""

    bl_idname = "shader_bake_glb.cancel"
    bl_label = "キャンセル"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return ACTIVE_JOB is not None and ACTIVE_JOB.status == JobStatus.RUNNING

    def execute(self, context: bpy.types.Context):
        # 押下時点では要求だけを記録する。実際の破棄は次のadvanceで行う。
        if ACTIVE_JOB is not None:
            ACTIVE_JOB.request_cancel()
            settings = _settings(context)
            settings.cancel_requested = True
            settings.current_phase = "キャンセル待機"
        return {"FINISHED"}


class SHADERBAKEGLB_PT_Panel(bpy.types.Panel):
    bl_label = "Shader Bake GLB Exporter"
    bl_idname = "SHADERBAKEGLB_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GLB Bake Export"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        settings = _settings(context)
        meshes = selected_mesh_objects(context)
        layout.prop(settings, "resolution")
        stats = layout.column(align=True)
        stats.label(text=f"選択Mesh数: {len(meshes)}")
        stats.label(text=f"検出Material数: {detected_material_count(meshes)}")

        # 実行中は進捗と診断だけを表示し、同じ状態を変更する操作を出さない。
        if settings.is_running:
            text = f"{settings.completed_units} / {settings.total_units}"
            layout.progress(factor=settings.progress, type="BAR", text=text)
            layout.label(text=f"Object: {settings.current_object or '-'}")
            layout.label(text=f"Material: {settings.current_material or '-'}")
            layout.label(text=f"処理: {settings.current_phase or '-'}")
            layout.label(text="ベイク中は一時的に操作できません。")
            layout.label(text="キャンセルは現在のベイク完了後に反映されます。")
            row = layout.row()
            row.enabled = not settings.cancel_requested
            row.operator(SHADERBAKEGLB_OT_Cancel.bl_idname, icon="CANCEL")
        else:
            layout.operator(SHADERBAKEGLB_OT_ExportSelected.bl_idname, text="GLBを書き出し…", icon="EXPORT")

        if settings.errors:
            box = layout.box()
            box.label(text="エラー一覧", icon="ERROR")
            for item in settings.errors:
                box.label(text=item.message)
        if settings.warnings:
            box = layout.box()
            box.label(text="警告一覧", icon="INFO")
            for item in settings.warnings:
                box.label(text=item.message)
        if settings.completed_path:
            box = layout.box()
            box.label(text="完了したGLBパス", icon="CHECKMARK")
            box.label(text=settings.completed_path)


CLASSES = (SHADERBAKEGLB_OT_ExportSelected, SHADERBAKEGLB_OT_Cancel, SHADERBAKEGLB_PT_Panel)


# OperatorとPanelをまとめて登録し、Sidebarから公開する。
def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    global ACTIVE_JOB
    # アドオン解除中でも一時DataBlockを残さないよう、実行中Jobを終了させる。
    if ACTIVE_JOB is not None and ACTIVE_JOB.status == JobStatus.RUNNING:
        ACTIVE_JOB.request_cancel()
        ACTIVE_JOB.advance()
    ACTIVE_JOB = None
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
