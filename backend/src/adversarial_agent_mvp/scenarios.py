"""Operator-owned bundles and an explicitly public scenario catalog.

Registration is a local operator operation, not an unauthenticated upload API.
The private document is only loaded for trusted episode bootstrap.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import select

from aml_target_protocol import (
    InterventionSurface,
    ToolResultSlot,
    validate_payload,
    validate_surface,
)

from .contracts import (
    AttackChannel,
    AttackTask,
    BenignExpectation,
    ForbiddenStateSpec,
    RedAction,
    StrictModel,
    TargetManifest,
    new_id,
)
from .storage import (
    AttackTaskRow,
    Repository,
    ScenarioVersionRow,
    Target,
    TargetBundleRow,
    TargetManifestRow,
    TargetVersion,
    normalized_image_digest,
)

VERIFIER_VERSION = "deterministic-v1"


def content_hash(document: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class ScenarioSurface(InterventionSurface):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=False)


class PublicScenario(StrictModel):
    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,99}$")
    version: str = Field(min_length=1, max_length=80)
    family: str = Field(min_length=1, max_length=100)
    split: Literal["train", "validation", "test", "development"]
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    legitimate_task: str = Field(min_length=1)
    attack_objective: str = Field(min_length=1)
    surfaces: list[ScenarioSurface] = Field(min_length=1)
    allowed_operations: dict[str, list[str]]

    @model_validator(mode="after")
    def unique_surfaces(self) -> PublicScenario:
        if len({surface.channel for surface in self.surfaces}) != len(self.surfaces):
            raise ValueError("scenario surface channels must be unique")
        if not self.allowed_operations or any(not ops for ops in self.allowed_operations.values()):
            raise ValueError("scenario must declare allowed tool operations")
        return self


class ScenarioGroundTruth(StrictModel):
    initial_world_state: dict[str, dict[str, Any]]
    target_configuration: dict[str, Any]
    verifier_version: Literal["deterministic-v1"] = VERIFIER_VERSION
    forbidden_states: list[ForbiddenStateSpec] = Field(min_length=1)
    benign_actions: list[RedAction] = Field(min_length=1)
    benign_expectations: list[BenignExpectation] = Field(min_length=1)
    known_attack: list[RedAction] = Field(min_length=1)
    expected_verifier_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_expectations(self) -> ScenarioGroundTruth:
        ids = {item.verifier_id for item in self.forbidden_states}
        if len(ids) != len(self.forbidden_states):
            raise ValueError("verifier IDs must be unique")
        if not set(self.expected_verifier_ids) <= ids:
            raise ValueError("fixture refers to an undeclared verifier")
        if any(e.action_index > len(self.benign_actions) for e in self.benign_expectations):
            raise ValueError("benign expectation refers to an absent action")
        if set(self.initial_world_state) - {"payments", "mail", "customers", "memory", "files"}:
            raise ValueError("unknown initial-state service")
        # Validate baseline shapes before a runtime is provisioned. Service-specific
        # record semantics belong to the operator's scenario and acceptance checks.
        required_maps = {
            "payments": ("transactions", "approvals"),
            "mail": ("messages",),
            "customers": ("records",),
            "memory": ("session", "persistent"),
            "files": ("files",),
        }
        for service, state in self.initial_world_state.items():
            for key, value in state.items():
                if key not in required_maps[service] or not isinstance(value, dict):
                    raise ValueError(f"invalid baseline field {service}.{key}")
        return self


class TargetBundle(StrictModel):
    schema_version: Literal["aml.target-bundle.v1"] = "aml.target-bundle.v1"
    manifest: TargetManifest
    execution_mode: Literal["fixture", "model"]
    scenario: PublicScenario
    ground_truth: ScenarioGroundTruth

    @model_validator(mode="after")
    def validate_wiring(self) -> TargetBundle:
        normalized_image_digest(self.manifest.image)
        if (self.execution_mode == "model") != (self.manifest.inference_profile is not None):
            raise ValueError(
                "model bundles require an inference profile; fixture bundles cannot use one"
            )
        if self.manifest.reset_url is None:
            raise ValueError("runnable target bundles require a reset endpoint")
        from .target_adapter import _capsule_target_endpoints

        _capsule_target_endpoints(self.manifest)
        declared = {
            alias: route.operations for alias, route in self.manifest.destination_routes.items()
        }
        if declared != self.scenario.allowed_operations:
            raise ValueError("scenario operations must match the target destination routes")
        if self.manifest.intervention_protocol:
            for surface in self.scenario.surfaces:
                validate_surface(surface)
                for slot in surface.slots:
                    if isinstance(slot, ToolResultSlot) and slot.operation not in declared.get(
                        slot.destination_alias, []
                    ):
                        raise ValueError(
                            "tool-response slot is outside the declared destination routes"
                        )
        if any(
            route.identity_alias not in self.manifest.identity_context
            for route in self.manifest.destination_routes.values()
        ):
            raise ValueError("every route requires an explicit trusted identity")
        channels = {surface.channel for surface in self.scenario.surfaces}
        if any(
            action.channel not in channels
            for action in self.ground_truth.benign_actions + self.ground_truth.known_attack
        ):
            raise ValueError("fixture uses an undeclared surface")
        if self.manifest.intervention_protocol:
            for action in self.ground_truth.benign_actions + self.ground_truth.known_attack:
                surface = next(s for s in self.scenario.surfaces if s.channel == action.channel)
                validate_payload(surface, action.payload)
        return self

    def scenario_document(self) -> dict[str, Any]:
        return {
            "public": self.scenario.model_dump(mode="json"),
            "private": self.ground_truth.model_dump(mode="json"),
        }


class BundleRegistration(StrictModel):
    bundle_id: str
    scenario_version_id: str
    target_version_id: str
    attack_task_id: str


class ScenarioRuntime(StrictModel):
    scenario_version_id: str
    initial_world_state: dict[str, dict[str, Any]]
    target_configuration: dict[str, Any]
    intervention_surfaces: list[InterventionSurface] = Field(default_factory=list)


class ScenarioCatalog:
    def __init__(self, repository: Repository):
        self.repository = repository

    @staticmethod
    def _registration(row: TargetBundleRow) -> BundleRegistration:
        return BundleRegistration(
            bundle_id=row.id,
            scenario_version_id=row.scenario_version_id,
            target_version_id=row.target_version_id,
            attack_task_id=row.attack_task_id,
        )

    def register(self, bundle: TargetBundle) -> BundleRegistration:
        # Revalidate mutable nested Pydantic values at the persistence boundary.
        bundle = TargetBundle.model_validate(bundle.model_dump(mode="json"))
        digest = content_hash(bundle.model_dump(mode="json"))
        scenario_hash = content_hash(bundle.scenario_document())
        scenario_key = f"scenario_{scenario_hash}"
        with self.repository.db.session() as session:
            existing = session.scalar(
                select(TargetBundleRow).where(TargetBundleRow.content_hash == digest)
            )
            if existing:
                return self._registration(existing)
            scenario = session.scalar(
                select(ScenarioVersionRow).where(
                    ScenarioVersionRow.scenario_id == bundle.scenario.scenario_id,
                    ScenarioVersionRow.version == bundle.scenario.version,
                )
            )
            if scenario and scenario.content_hash != scenario_hash:
                raise ValueError("scenario content changed; publish a new scenario version")
            previous = session.scalar(
                select(ScenarioVersionRow).where(
                    ScenarioVersionRow.scenario_id == bundle.scenario.scenario_id
                )
            )
            if previous and (previous.family, previous.split) != (
                bundle.scenario.family,
                bundle.scenario.split,
            ):
                raise ValueError("scenario family and split membership are frozen")
            if scenario is None:
                session.add(
                    ScenarioVersionRow(
                        id=scenario_key,
                        scenario_id=bundle.scenario.scenario_id,
                        version=bundle.scenario.version,
                        family=bundle.scenario.family,
                        split=bundle.scenario.split,
                        public_document=bundle.scenario.model_dump(mode="json"),
                        private_document=bundle.ground_truth.model_dump(mode="json"),
                        content_hash=scenario_hash,
                    )
                )
                session.flush()
            target = session.scalar(
                select(Target).where(Target.name == bundle.manifest.target_name)
            )
            if target is None:
                target = Target(id=new_id("target"), name=bundle.manifest.target_name)
                session.add(target)
                session.flush()
            version = TargetVersion(
                id=new_id("targetv"),
                target_id=target.id,
                image=bundle.manifest.image,
                image_digest=normalized_image_digest(bundle.manifest.image),
            )
            session.add(version)
            session.flush()
            session.add(
                TargetManifestRow(
                    id=new_id("manifest"),
                    target_version_id=version.id,
                    document=bundle.manifest.model_dump(mode="json"),
                )
            )
            task = AttackTask(
                target_version_id=version.id,
                scenario_version_id=scenario_key,
                objective=bundle.scenario.attack_objective,
                forbidden_states=bundle.ground_truth.forbidden_states,
                available_channels=[
                    AttackChannel(surface.channel) for surface in bundle.scenario.surfaces
                ],
            )
            session.add(
                AttackTaskRow(
                    id=task.task_id,
                    target_version_id=version.id,
                    document=task.model_dump(mode="json"),
                )
            )
            session.flush()
            row = TargetBundleRow(
                id=new_id("bundle"),
                content_hash=digest,
                execution_mode=bundle.execution_mode,
                scenario_version_id=scenario_key,
                target_version_id=version.id,
                attack_task_id=task.task_id,
            )
            session.add(row)
            self.repository._append_event(
                session,
                "target_bundle",
                row.id,
                "TARGET_BUNDLE_REGISTERED",
                {
                    "scenario_version_id": scenario_key,
                    "target_version_id": version.id,
                    "attack_task_id": task.task_id,
                    "execution_mode": bundle.execution_mode,
                    "bundle_sha256": digest,
                },
            )
        return self._registration(row)

    def list_public(
        self,
        *,
        family: str | None = None,
        split: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = select(ScenarioVersionRow).order_by(
            ScenarioVersionRow.scenario_id, ScenarioVersionRow.version
        )
        if family is not None:
            query = query.where(ScenarioVersionRow.family == family)
        if split is not None:
            query = query.where(ScenarioVersionRow.split == split)
        with self.repository.db.session() as session:
            rows = list(session.scalars(query.limit(limit).offset(offset)))
            bindings = list(
                session.scalars(
                    select(TargetBundleRow).where(
                        TargetBundleRow.scenario_version_id.in_([row.id for row in rows])
                    )
                )
            )
            return [
                self._public(
                    row, [binding for binding in bindings if binding.scenario_version_id == row.id]
                )
                for row in rows
            ]

    @staticmethod
    def _public(row: ScenarioVersionRow, bindings: list[TargetBundleRow]) -> dict[str, Any]:
        # Explicit whitelist; never serialize a database row or ground-truth model.
        return {
            "scenario_version_id": row.id,
            **PublicScenario.model_validate(row.public_document).model_dump(mode="json"),
            "targets": [
                {
                    "target_version_id": binding.target_version_id,
                    "attack_task_id": binding.attack_task_id,
                    "bundle_id": binding.id,
                    "execution_mode": binding.execution_mode,
                }
                for binding in sorted(bindings, key=lambda item: item.id)
            ],
        }

    def get_public(self, scenario_version_id: str) -> dict[str, Any]:
        with self.repository.db.session() as session:
            row = session.get(ScenarioVersionRow, scenario_version_id)
            if row is None:
                raise KeyError("scenario version not found")
            bindings = list(
                session.scalars(
                    select(TargetBundleRow).where(TargetBundleRow.scenario_version_id == row.id)
                )
            )
            return self._public(row, bindings)

    def lineage(self, scenario_version_id: str) -> dict[str, Any]:
        with self.repository.db.session() as session:
            row = session.get(ScenarioVersionRow, scenario_version_id)
            if row is None:
                raise KeyError("scenario version not found")
            return {
                "scenario_version_id": row.id,
                "scenario_id": row.scenario_id,
                "version": row.version,
                "family": row.family,
                "split": row.split,
                "content_sha256": row.content_hash,
                "verifier_version": row.private_document["verifier_version"],
            }

    def runtime(self, task: AttackTask) -> ScenarioRuntime:
        with self.repository.db.session() as session:
            row = session.get(ScenarioVersionRow, task.scenario_version_id)
            if row is None:
                raise KeyError("scenario version not found")
            binding = session.scalar(
                select(TargetBundleRow).where(
                    TargetBundleRow.scenario_version_id == row.id,
                    TargetBundleRow.target_version_id == task.target_version_id,
                )
            )
            if binding is None:
                raise ValueError("scenario is not registered for this target version")
            ground = ScenarioGroundTruth.model_validate(row.private_document)
            public = PublicScenario.model_validate(row.public_document)
            if (
                content_hash(
                    {
                        "public": public.model_dump(mode="json"),
                        "private": ground.model_dump(mode="json"),
                    }
                )
                != row.content_hash
            ):
                raise ValueError("scenario content hash mismatch")
            if task.forbidden_states != ground.forbidden_states:
                raise ValueError("scenario verifier rules cannot be overridden")
            if task.available_channels != [s.channel for s in public.surfaces]:
                raise ValueError("scenario attack surfaces cannot be overridden")
            return ScenarioRuntime(
                scenario_version_id=row.id,
                initial_world_state=ground.initial_world_state,
                target_configuration=ground.target_configuration,
                intervention_surfaces=[
                    InterventionSurface.model_validate(s.model_dump(mode="json"))
                    for s in public.surfaces
                ],
            )
