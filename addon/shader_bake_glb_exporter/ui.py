"""3D View SidebarとModal Operator。"""

from __future__ import annotations

from pathlib import Path

import bpy

from .job import BakeJob, BakeJobConfig, JobStatus, detected_material_count, selected_mesh_objects


ACTIVE_JOB: BakeJob | None = None


def _settings(context: bpy.types.Context):
    return context.window_manager.shader_bake_glb


def _copy_job_state(context: bpy.types.Context, job: BakeJob) -> None:
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


class SHADERBAKEGLB_OT_ExportSelected(bpy.types.Operator):
    """選択Meshを作業コピーでベイクし、検証済みGLBへ書き出す公開Operator。"""

    bl_idname = "shader_bake_glb.export_selected"
    bl_label = "選択オブジェクトをGLB書き出し"
    bl_options = {"REGISTER"}

    _timer = None

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.window_manager is not None and not _settings(context).is_running

    def execute(self, context: bpy.types.Context):
        global ACTIVE_JOB
        settings = _settings(context)
        settings.errors.clear()
        settings.completed_path = ""
        settings.progress = 0.0
        settings.cancel_requested = False
        output_text = bpy.path.abspath(settings.output_path) if settings.output_path else ""
        config = BakeJobConfig(Path(output_text), int(settings.resolution))
        job = BakeJob(context, config)
        ACTIVE_JOB = job
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
        if event.type == "ESC":
            job.request_cancel()
            _settings(context).cancel_requested = True
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

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
            self.report({"INFO"}, "GLB書き出しが完了しました")
            return {"FINISHED"}
        if status == JobStatus.CANCELLED:
            self.report({"WARNING"}, "GLB書き出しをキャンセルしました")
        else:
            self.report({"ERROR"}, job.errors[-1].reason if job.errors else "GLB書き出しに失敗しました")
        return {"CANCELLED"}

    def _finish_timer(self, context: bpy.types.Context) -> None:
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

    def cancel(self, context: bpy.types.Context) -> None:
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
        layout.prop(settings, "output_path")
        layout.prop(settings, "resolution")
        stats = layout.column(align=True)
        stats.label(text=f"選択Mesh数: {len(meshes)}")
        stats.label(text=f"検出Material数: {detected_material_count(meshes)}")

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
            layout.operator(SHADERBAKEGLB_OT_ExportSelected.bl_idname, icon="EXPORT")

        if settings.errors:
            box = layout.box()
            box.label(text="エラー一覧", icon="ERROR")
            for item in settings.errors:
                box.label(text=item.message)
        if settings.completed_path:
            box = layout.box()
            box.label(text="完了したGLBパス", icon="CHECKMARK")
            box.label(text=settings.completed_path)


CLASSES = (SHADERBAKEGLB_OT_ExportSelected, SHADERBAKEGLB_OT_Cancel, SHADERBAKEGLB_PT_Panel)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    global ACTIVE_JOB
    if ACTIVE_JOB is not None and ACTIVE_JOB.status == JobStatus.RUNNING:
        ACTIVE_JOB.request_cancel()
        ACTIVE_JOB.advance()
    ACTIVE_JOB = None
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
