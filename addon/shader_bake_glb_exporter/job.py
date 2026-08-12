"""Modal UIとbackgroundテストが共有する段階実行Job。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
from typing import Callable, Iterable

import bpy
from mathutils import Matrix

from .bake import (
    BakeFailure,
    TempDataRegistry,
    WorkObject,
    bake_channel,
    combine_ready_images,
    channels_for_analysis,
    create_job_scene,
    create_work_object,
    finalize_work_object,
    rebuild_material,
    unwrap_work_object,
    used_material_indices,
)
from .glb_export import PendingGlb, export_to_temporary_glb, validate_and_commit
from .material_validation import MaterialValidationError


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


@dataclass(frozen=True)
class StaticObjectInstance:
    """選択Meshがdepsgraph上に生成した静的Mesh instanceの出力情報。"""

    source: bpy.types.Object
    parent: bpy.types.Object
    matrix_world: Matrix
    persistent_id: tuple[int, ...]


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
    candidates = list(objects) if objects is not None else selected_mesh_objects(context)
    if not candidates:
        errors.append(MaterialValidationError("<選択>", "<なし>", "選択Meshが0件です"))
    selected = [obj for obj in candidates if obj.data.polygons]
    if candidates and not selected:
        errors.append(MaterialValidationError("<選択>", "<なし>", "書き出せるFaceを持つMeshがありません"))
    if not str(config.output_path):
        errors.append(MaterialValidationError("<出力>", "<なし>", "出力先が未指定です"))
    elif config.output_path.suffix.lower() != ".glb":
        errors.append(MaterialValidationError("<出力>", "<なし>", "出力拡張子は.glbが必要です"))
    if config.resolution not in {512, 1024, 2048}:
        errors.append(MaterialValidationError("<設定>", "<なし>", "解像度は512、1024、2048だけを指定できます"))

    material_usages = 0
    for obj in selected:
        for slot_index in used_material_indices(obj.data):
            material_usages += 1
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
        candidates = list(objects) if objects is not None else selected_mesh_objects(context)
        self.objects, self.errors, self.material_usages = preflight(context, config, objects)
        self.warnings: list[MaterialValidationError] = []
        for obj in candidates:
            if not obj.data.polygons:
                self._warn(obj.name, "<なし>", "Faceがないため書き出し対象から除外しました")
        self.status = JobStatus.READY
        self.cancel_requested = False
        self.completed_units = 0
        self.total_units = 0
        self.current_phase = "検証"
        self.current_object = ""
        self.current_material = ""
        self.registry = TempDataRegistry()
        self.scene: bpy.types.Scene | None = None
        self.collection: bpy.types.Collection | None = None
        self.work_objects: list[WorkObject] = []
        self.static_instances: list[StaticObjectInstance] = []
        self._instance_only_source_pointers: set[int] = set()
        self.pending_glb: PendingGlb | None = None
        self._steps: list[JobStep] = []
        self._step_index = 0
        self._snapshot: ContextSnapshot | None = None

    def _warn(self, object_name: str, material_name: str, reason: str) -> None:
        warning = MaterialValidationError(object_name, material_name, reason)
        if warning not in self.warnings:
            self.warnings.append(warning)

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

    def _capture_static_instances(self) -> list[bpy.types.Object]:
        """選択Meshが生成したinstanceを元SceneのdepsgraphからJob所有情報へ固定する。"""

        selected_pointers = {obj.as_pointer() for obj in self.objects}
        source_objects: dict[int, bpy.types.Object] = {}
        depsgraph = self.context.evaluated_depsgraph_get()
        for instance in depsgraph.object_instances:
            if not instance.is_instance or instance.parent is None:
                continue
            parent = getattr(instance.parent, "original", instance.parent)
            if parent.as_pointer() not in selected_pointers:
                continue
            evaluated_source = instance.instance_object or instance.object
            source = getattr(evaluated_source, "original", evaluated_source)
            if source.type != "MESH" or source.data is None or not source.data.polygons:
                self._warn(parent.name, source.name, "Mesh以外またはFaceなしの静的instanceを除外しました")
                continue
            matrix_world = instance.matrix_world.copy()
            if not all(
                math.isfinite(float(matrix_world[row][column]))
                for row in range(4)
                for column in range(4)
            ):
                self._warn(parent.name, source.name, "非有限transformを持つ静的instanceを除外しました")
                continue
            persistent_id = tuple(
                int(value)
                for value in instance.persistent_id
                if int(value) != 2147483647
            )
            self.static_instances.append(
                StaticObjectInstance(source, parent, matrix_world, persistent_id)
            )
            source_objects[source.as_pointer()] = source
        return [
            source
            for pointer, source in source_objects.items()
            if pointer not in selected_pointers
        ]

    def start(self) -> None:
        if self.status != JobStatus.READY:
            raise RuntimeError("Jobは開始済みです")
        if self.errors:
            self.status = JobStatus.FAILED
            return
        self._snapshot = self._capture_context()
        try:
            self._ensure_original_object_mode()
            instance_only_sources = self._capture_static_instances()
            self._instance_only_source_pointers = {
                source.as_pointer() for source in instance_only_sources
            }
            self.scene, self.collection = create_job_scene(self.context.scene, self.registry)
            for original in [*self.objects, *instance_only_sources]:
                try:
                    self.work_objects.append(
                        create_work_object(self.context, original, self.scene, self.collection, self.registry, self._warn)
                    )
                except Exception as exc:
                    self._warn(original.name, "<なし>", f"Mesh準備に失敗したため除外しました: {exc}")
            if not self.work_objects:
                raise BakeFailure("書き出せるMeshがありません")
            self.material_usages = sum(len(work.slots) for work in self.work_objects)
            self._build_steps()
            self.total_units = len(self._steps)
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
                for channel in channels_for_analysis(slot.source_analysis):
                    stem = f"{work.original.name}_slot_{slot.slot_index}"

                    def channel_action(work=work, slot=slot, channel=channel, stem=stem):
                        bake_channel(self.context, self.scene, work, slot, channel, self.config.resolution, self.registry, self._warn)
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
            export_nodes: dict[int, bpy.types.Object] = {}
            export_sources: dict[int, WorkObject] = {}
            for work in self.work_objects:
                export_mesh = self.registry.track(work.object.data.copy())
                export_object = self.registry.track(
                    bpy.data.objects.new(f"{work.original.name}__BAKED", export_mesh)
                )
                export_object.matrix_world = work.object.matrix_world.copy()
                export_collection.objects.link(export_object)
                export_work = WorkObject(work.original, export_object, work.bake_uv_name, work.slots)
                export_work_objects.append(export_work)
                source_pointer = work.original.as_pointer()
                export_sources[source_pointer] = export_work
                if source_pointer not in self._instance_only_source_pointers:
                    export_nodes[source_pointer] = export_object

            # 選択Meshの祖先は形状を持たない構造Nodeとして複製する。
            # world matrixを固定した後に親を設定し、元階層のlocal transformを再現する。
            ancestors: list[bpy.types.Object] = []
            seen_ancestors: set[int] = set()
            for work in self.work_objects:
                if work.original.as_pointer() in self._instance_only_source_pointers:
                    continue
                parent = work.original.parent
                while parent is not None:
                    pointer = parent.as_pointer()
                    if pointer not in export_nodes and pointer not in seen_ancestors:
                        ancestors.append(parent)
                        seen_ancestors.add(pointer)
                    parent = parent.parent
            def hierarchy_depth(obj: bpy.types.Object) -> int:
                depth = 0
                parent = obj.parent
                while parent is not None:
                    depth += 1
                    parent = parent.parent
                return depth

            ancestors.sort(key=hierarchy_depth)
            depsgraph = self.context.evaluated_depsgraph_get()
            for original in ancestors:
                proxy = self.registry.track(
                    bpy.data.objects.new(f"{original.name}__BAKED_HIERARCHY", None)
                )
                proxy.empty_display_type = "PLAIN_AXES"
                proxy.matrix_world = original.evaluated_get(depsgraph).matrix_world.copy()
                export_collection.objects.link(proxy)
                export_nodes[original.as_pointer()] = proxy

            for work in export_work_objects:
                original = work.original
                if original.as_pointer() in self._instance_only_source_pointers:
                    continue
                parent = original.parent
                if parent is None:
                    continue
                export_parent = export_nodes.get(parent.as_pointer())
                if export_parent is None:
                    continue
                world = work.object.matrix_world.copy()
                work.object.parent = export_parent
                work.object.matrix_world = world
            for original in ancestors:
                parent = original.parent
                if parent is None:
                    continue
                proxy = export_nodes[original.as_pointer()]
                export_parent = export_nodes.get(parent.as_pointer())
                if export_parent is None:
                    continue
                world = proxy.matrix_world.copy()
                proxy.parent = export_parent
                proxy.matrix_world = world

            hierarchy_objects = list(export_nodes.values())
            for index, instance in enumerate(self.static_instances):
                source_work = export_sources.get(instance.source.as_pointer())
                export_parent = export_nodes.get(instance.parent.as_pointer())
                if source_work is None:
                    self._warn(instance.parent.name, instance.source.name, "静的instanceのベイク済みMeshを取得できないため除外しました")
                    continue
                instance_object = self.registry.track(
                    bpy.data.objects.new(
                        f"{instance.source.name}__BAKED_INSTANCE_{index:04d}",
                        source_work.object.data,
                    )
                )
                instance_object.matrix_world = instance.matrix_world.copy()
                export_collection.objects.link(instance_object)
                if export_parent is not None:
                    world = instance_object.matrix_world.copy()
                    instance_object.parent = export_parent
                    instance_object.matrix_world = world
                else:
                    self._warn(instance.parent.name, instance.source.name, "静的instanceの親Nodeを取得できないためrootへ配置しました")
                hierarchy_objects.append(instance_object)
            if self.context.window:
                self.context.window.scene = export_scene
            self.pending_glb = export_to_temporary_glb(
                self.context,
                export_scene,
                export_work_objects,
                hierarchy_objects,
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
