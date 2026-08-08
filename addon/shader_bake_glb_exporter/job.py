"""Modal UIとbackgroundテストが共有する段階実行Job。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

import bpy

from .bake import (
    BakeFailure,
    CHANNELS,
    TempDataRegistry,
    WorkObject,
    bake_channel,
    combine_ready_images,
    create_job_scene,
    create_work_object,
    finalize_work_object,
    rebuild_material,
    unwrap_work_object,
    used_material_indices,
)
from .glb_export import PendingGlb, export_to_temporary_glb, validate_and_commit
from .material_validation import MaterialValidationError, analyze_material


class JobStatus(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class BakeJobConfig:
    """外部から指定するJob契約。解像度はUIの3値だけを許可する。"""

    output_path: Path
    resolution: int = 1024
    fail_after_phase: str | None = None


@dataclass(frozen=True)
class JobStep:
    phase: str
    object_name: str
    material_name: str
    action: Callable[[], None]


@dataclass
class ContextSnapshot:
    scene: bpy.types.Scene
    window_scene: bpy.types.Scene | None
    view_layer: bpy.types.ViewLayer
    selected_objects: tuple[bpy.types.Object, ...]
    active_object: bpy.types.Object | None
    mode: str
    render_engine: str
    image_file_format: str
    image_color_depth: str
    image_color_mode: str
    cycles_samples: int
    cycles_use_denoising: bool


def selected_mesh_objects(context: bpy.types.Context) -> list[bpy.types.Object]:
    return [obj for obj in context.selected_objects if obj.type == "MESH"]


def detected_material_count(objects: Iterable[bpy.types.Object]) -> int:
    materials = set()
    for obj in objects:
        for index in used_material_indices(obj.data):
            if index < len(obj.material_slots) and obj.material_slots[index].material:
                materials.add(obj.material_slots[index].material.as_pointer())
    return len(materials)


def preflight(
    context: bpy.types.Context,
    config: BakeJobConfig,
    objects: list[bpy.types.Object] | None = None,
) -> tuple[list[bpy.types.Object], list[MaterialValidationError], int]:
    """一時DataBlockを作る前に全対象を検証する。"""

    errors: list[MaterialValidationError] = []
    selected = list(objects) if objects is not None else selected_mesh_objects(context)
    if not selected:
        errors.append(MaterialValidationError("<選択>", "<なし>", "選択Meshが0件です"))
    if not str(config.output_path):
        errors.append(MaterialValidationError("<出力>", "<なし>", "出力先が未指定です"))
    elif config.output_path.suffix.lower() != ".glb":
        errors.append(MaterialValidationError("<出力>", "<なし>", "出力拡張子は.glbが必要です"))
    if config.resolution not in {512, 1024, 2048}:
        errors.append(MaterialValidationError("<設定>", "<なし>", "解像度は512、1024、2048だけを指定できます"))

    material_usages = 0
    for obj in selected:
        if not obj.data.polygons:
            errors.append(MaterialValidationError(obj.name, "<なし>", "FaceがないMeshはベイクできません"))
            continue
        for slot_index in used_material_indices(obj.data):
            material_usages += 1
            if slot_index >= len(obj.material_slots) or obj.material_slots[slot_index].material is None:
                errors.append(MaterialValidationError(obj.name, f"Slot {slot_index}", "使用中SlotにMaterialがありません"))
                continue
            material = obj.material_slots[slot_index].material
            try:
                analyze_material(material, obj.name)
            except MaterialValidationError as exc:
                errors.append(exc)
    return selected, errors, material_usages


class BakeJob:
    """1回のGLB生成を所有し、各advanceで最大1処理単位だけ実行する。"""

    def __init__(
        self,
        context: bpy.types.Context,
        config: BakeJobConfig,
        objects: list[bpy.types.Object] | None = None,
    ) -> None:
        self.context = context
        self.config = config
        self.objects, self.errors, self.material_usages = preflight(context, config, objects)
        self.status = JobStatus.READY
        self.cancel_requested = False
        self.completed_units = 0
        self.total_units = 9 * self.material_usages + len(self.objects) + 2
        self.current_phase = "検証"
        self.current_object = ""
        self.current_material = ""
        self.registry = TempDataRegistry()
        self.scene: bpy.types.Scene | None = None
        self.collection: bpy.types.Collection | None = None
        self.work_objects: list[WorkObject] = []
        self.pending_glb: PendingGlb | None = None
        self._steps: list[JobStep] = []
        self._step_index = 0
        self._snapshot: ContextSnapshot | None = None

    @property
    def progress(self) -> float:
        if self.total_units <= 0:
            return 0.0
        return min(1.0, self.completed_units / self.total_units)

    def request_cancel(self) -> None:
        self.cancel_requested = True

    def _capture_context(self) -> ContextSnapshot:
        return ContextSnapshot(
            scene=self.context.scene,
            window_scene=self.context.window.scene if self.context.window else None,
            view_layer=self.context.view_layer,
            selected_objects=tuple(self.context.selected_objects),
            active_object=self.context.view_layer.objects.active,
            mode=self.context.mode,
            render_engine=self.context.scene.render.engine,
            image_file_format=self.context.scene.render.image_settings.file_format,
            image_color_depth=self.context.scene.render.image_settings.color_depth,
            image_color_mode=self.context.scene.render.image_settings.color_mode,
            cycles_samples=self.context.scene.cycles.samples,
            cycles_use_denoising=self.context.scene.cycles.use_denoising,
        )

    def _ensure_original_object_mode(self) -> None:
        active = self.context.view_layer.objects.active
        if active is not None and active.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError as exc:
                raise BakeFailure(f"Object Modeへ移行できません: {exc}") from exc

    def start(self) -> None:
        if self.status != JobStatus.READY:
            raise RuntimeError("Jobは開始済みです")
        if self.errors:
            self.status = JobStatus.FAILED
            return
        self._snapshot = self._capture_context()
        try:
            self._ensure_original_object_mode()
            self.scene, self.collection = create_job_scene(self.context.scene, self.registry)
            for original in self.objects:
                self.work_objects.append(
                    create_work_object(self.context, original, self.scene, self.collection, self.registry)
                )
            actual_usages = sum(len(work.slots) for work in self.work_objects)
            if actual_usages != self.material_usages:
                raise BakeFailure("Modifier評価後にMaterial Slot構成が変化したため安全に進捗を確定できません")
            self.completed_units = self.material_usages
            self._build_steps()
            self.status = JobStatus.RUNNING
        except Exception as exc:
            self._fail(exc)

    def _build_steps(self) -> None:
        assert self.scene is not None
        for work in self.work_objects:
            self._steps.append(
                JobStep("UV生成", work.original.name, "", lambda work=work: unwrap_work_object(self.context, self.scene, work))
            )
        for work in self.work_objects:
            for slot in work.slots:
                for channel in CHANNELS:
                    stem = f"{work.original.name}_slot_{slot.slot_index}"

                    def channel_action(work=work, slot=slot, channel=channel, stem=stem):
                        bake_channel(self.context, self.scene, work, slot, channel, self.config.resolution, self.registry)
                        combine_ready_images(slot, self.config.resolution, self.registry, stem)

                    self._steps.append(JobStep(channel, work.original.name, slot.source_material.name, channel_action))
                self._steps.append(
                    JobStep(
                        "Material再構築",
                        work.original.name,
                        slot.source_material.name,
                        lambda work=work, slot=slot: rebuild_material(slot, self.registry, work.original.name),
                    )
                )

        def export_action() -> None:
            for work in self.work_objects:
                finalize_work_object(work)
            assert self._snapshot is not None
            # Blender 5.1.1ではWindowと異なるSceneのViewLayerをoverrideして
            # glTF exporterへ渡すとDepsgraphが破綻する場合がある。元Scene内の
            # 一時Collectionへ完成コピーだけを接続し、選択状態はJob終了時に戻す。
            export_scene = self._snapshot.scene
            export_collection = self.registry.track(
                bpy.data.collections.new("__SHADER_BAKE_GLB_EXPORT_COLLECTION")
            )
            export_scene.collection.children.link(export_collection)
            export_work_objects: list[WorkObject] = []
            for work in self.work_objects:
                export_mesh = self.registry.track(work.object.data.copy())
                export_object = self.registry.track(
                    bpy.data.objects.new(f"{work.original.name}__BAKED", export_mesh)
                )
                export_object.matrix_world = work.object.matrix_world.copy()
                export_collection.objects.link(export_object)
                export_work_objects.append(WorkObject(work.original, export_object, work.bake_uv_name, work.slots))
            if self.context.window:
                self.context.window.scene = export_scene
            self.pending_glb = export_to_temporary_glb(
                self.context,
                export_scene,
                export_work_objects,
                self.config.output_path,
            )

        def validate_action() -> None:
            if self.pending_glb is None:
                raise BakeFailure("検証対象の一時GLBがありません")
            validate_and_commit(self.pending_glb)
            self.pending_glb = None

        self._steps.append(JobStep("GLB出力", "", "", export_action))
        self._steps.append(JobStep("検証", "", "", validate_action))

    def advance(self) -> JobStatus:
        if self.status == JobStatus.READY:
            self.start()
        if self.status != JobStatus.RUNNING:
            return self.status
        if self.cancel_requested:
            self._cancel()
            return self.status
        if self._step_index >= len(self._steps):
            self._succeed()
            return self.status

        step = self._steps[self._step_index]
        self.current_phase = step.phase
        self.current_object = step.object_name
        self.current_material = step.material_name
        try:
            step.action()
            self.completed_units += 1
            self._step_index += 1
            if self.config.fail_after_phase == step.phase:
                raise BakeFailure(f"テスト用故障注入: {step.phase}")
            if self._step_index >= len(self._steps):
                self._succeed()
        except Exception as exc:
            self._fail(exc)
        return self.status

    def run_to_completion(self) -> JobStatus:
        while self.status in {JobStatus.READY, JobStatus.RUNNING}:
            self.advance()
        return self.status

    def _restore_context(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        if self.context.window and snapshot.window_scene is not None:
            self.context.window.scene = snapshot.window_scene
        try:
            snapshot.scene.render.engine = snapshot.render_engine
            snapshot.scene.render.image_settings.file_format = snapshot.image_file_format
            snapshot.scene.render.image_settings.color_depth = snapshot.image_color_depth
            snapshot.scene.render.image_settings.color_mode = snapshot.image_color_mode
            snapshot.scene.cycles.samples = snapshot.cycles_samples
            snapshot.scene.cycles.use_denoising = snapshot.cycles_use_denoising
            for obj in snapshot.scene.objects:
                obj.select_set(obj in snapshot.selected_objects)
            snapshot.view_layer.objects.active = snapshot.active_object
            active = snapshot.active_object
            if active is not None and snapshot.mode != "OBJECT":
                mode_map = {
                    "EDIT_MESH": "EDIT",
                    "EDIT_CURVE": "EDIT",
                    "SCULPT": "SCULPT",
                    "PAINT_VERTEX": "VERTEX_PAINT",
                    "PAINT_WEIGHT": "WEIGHT_PAINT",
                    "PAINT_TEXTURE": "TEXTURE_PAINT",
                }
                target_mode = mode_map.get(snapshot.mode)
                if target_mode:
                    override = dict(
                        scene=snapshot.scene,
                        view_layer=snapshot.view_layer,
                        active_object=active,
                        object=active,
                        selected_objects=list(snapshot.selected_objects),
                        selected_editable_objects=list(snapshot.selected_objects),
                    )
                    with self.context.temp_override(**override):
                        bpy.ops.object.mode_set(mode=target_mode)
        except (ReferenceError, RuntimeError):
            pass

    def _cleanup(self) -> None:
        if self.pending_glb is not None:
            self.pending_glb.temporary_path.unlink(missing_ok=True)
            self.pending_glb = None
        self._restore_context()
        self.registry.cleanup()

    def _succeed(self) -> None:
        self.completed_units = self.total_units
        self.status = JobStatus.SUCCEEDED
        self.current_phase = "完了"
        self._cleanup()

    def _cancel(self) -> None:
        self.status = JobStatus.CANCELLED
        self.current_phase = "キャンセル"
        self._cleanup()

    def _fail(self, exc: Exception) -> None:
        self.status = JobStatus.FAILED
        self.current_phase = "失敗"
        if isinstance(exc, MaterialValidationError):
            self.errors.append(exc)
        else:
            self.errors.append(MaterialValidationError(self.current_object or "<Job>", self.current_material or "<なし>", str(exc)))
        self._cleanup()
