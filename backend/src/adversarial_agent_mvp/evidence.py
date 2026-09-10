from __future__ import annotations

import hashlib
import json
from typing import Any

from .artifacts import ArtifactStore, StoredArtifact
from .contracts import EpisodeStatus
from .storage import Repository


class EvidenceBuilder:
    def __init__(self, repository: Repository, store: ArtifactStore):
        self.repository, self.store = repository, store

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        return value.isoformat() if value else None

    @staticmethod
    def _content_hash(document: dict[str, Any]) -> str:
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()

    def build_episode_bundle(
        self,
        campaign_id: str,
        episode_id: str,
        *,
        require_terminal: bool = True,
        link_findings: bool = True,
    ) -> StoredArtifact:
        loaded = self.repository.get_episode(episode_id)
        if loaded is None:
            raise KeyError("episode not found")
        episode, steps = loaded
        if episode.campaign_id != campaign_id:
            raise ValueError("episode does not belong to campaign")
        terminal_statuses = {
            EpisodeStatus.SUCCEEDED,
            EpisodeStatus.EXHAUSTED,
            EpisodeStatus.FAILED,
            EpisodeStatus.DESTROYED,
        }
        if require_terminal and episode.status not in terminal_statuses:
            raise ValueError("evidence can only be exported for a terminal episode")
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError("campaign not found")
        target_version = self.repository.get_target_version(campaign.target_version_id)
        if target_version is None:
            raise KeyError("target version not found")
        target = self.repository.get_target(target_version.target_id)
        if target is None:
            raise KeyError("target not found")
        manifest_document = self.repository.load_manifest(target_version.id)
        task_document = self.repository.load_task(campaign.attack_task_id)
        scenario_document = None
        if task_document.get("scenario_version_id"):
            from .scenarios import ScenarioCatalog

            scenario_document = ScenarioCatalog(self.repository).lineage(task_document["scenario_version_id"])
        policy = (
            self.repository.get_policy_version(campaign.policy_version_id)
            if campaign.policy_version_id
            else None
        )
        red_config = (
            self.repository.get_red_experiment_config(campaign.red_config_id)
            if campaign.red_config_id
            else None
        )
        effects = self.repository.list_effect_attempts(episode_id)
        decisions = self.repository.list_policy_decisions(episode_id)
        decision_by_effect = {decision.effect_id: decision for decision in decisions}
        verifier_events = self.repository.list_verifier_events(episode_id)
        invocations = self.repository.list_model_invocations(
            campaign_id=campaign_id, episode_id=episode_id
        )
        invocation_links = self.repository.list_model_invocation_links(episode_id=episode_id)
        links_by_invocation: dict[str, list[Any]] = {}
        invocation_ids_by_step: dict[str, list[str]] = {}
        for link in invocation_links:
            links_by_invocation.setdefault(link.model_invocation_id, []).append(link)
            if link.step_id is not None:
                invocation_ids_by_step.setdefault(link.step_id, []).append(link.model_invocation_id)
        trajectories = self.repository.list_trajectory_summaries(episode_id)
        findings = self.repository.list_findings(episode_id=episode_id)
        containment_preflight = self.repository.get_containment_preflight(campaign_id)
        runtime_containment = self.repository.get_runtime_containment(episode_id)
        content: dict[str, Any] = {
            "campaign": {
                "campaign_id": campaign.id,
                "status": campaign.status,
                "search_mode": campaign.search_mode,
                "run_kind": campaign.run_kind,
                "source_finding_id": campaign.source_finding_id,
                "source_episode_id": campaign.source_episode_id,
                "replay_episode_id": campaign.replay_episode_id,
                "configuration": campaign.configuration,
                "containment_status": (
                    containment_preflight["containment_status"]
                    if containment_preflight is not None
                    else "PENDING"
                ),
                "containment_preflight": containment_preflight,
                "created_at": self._timestamp(campaign.created_at),
                "started_at": self._timestamp(campaign.started_at),
                "completed_at": self._timestamp(campaign.completed_at),
            },
            "target": {
                "target_id": target.id,
                "target_name": target.name,
                "target_version_id": target_version.id,
                "image": target_version.image,
                "image_digest": target_version.image_digest,
                "manifest": manifest_document,
                "manifest_sha256": self._content_hash(manifest_document),
            },
            "runtime_containment": runtime_containment,
            "scenario": scenario_document,
            "attack_task": {
                "attack_task_id": campaign.attack_task_id,
                "document": task_document,
                "content_sha256": self._content_hash(task_document),
            },
            "red_experiment_config": (
                {
                    "red_config_id": red_config.id,
                    "name": red_config.name,
                    "document": red_config.document,
                    "content_sha256": red_config.content_hash,
                }
                if red_config
                else None
            ),
            "policy_version": (
                {
                    "policy_version_id": policy.id,
                    "version": policy.version,
                    "policies": policy.policies,
                    "content_sha256": policy.content_hash,
                }
                if policy
                else None
            ),
            "episode": {
                "episode_id": episode.id,
                "status": episode.status,
                "seed": episode.seed,
                "terminal_success": episode.terminal_success,
                "cumulative_reward": episode.cumulative_reward,
                "error": episode.error,
                "created_at": self._timestamp(episode.created_at),
                "started_at": self._timestamp(episode.started_at),
                "completed_at": self._timestamp(episode.completed_at),
            },
            "steps": [
                {
                    "step_id": step.id,
                    "step_index": step.step_index,
                    "red_action": step.red_action,
                    "public_observation": step.public_observation,
                    "reward": step.reward,
                    "terminal_success": step.terminal_success,
                    "strategy_id": step.strategy_id,
                    "model_invocation_id": step.model_invocation_id,
                    "model_invocation_ids": sorted(
                        {
                            *invocation_ids_by_step.get(step.id, []),
                            *(
                                [step.model_invocation_id]
                                if step.model_invocation_id is not None
                                else []
                            ),
                        }
                    ),
                    "created_at": self._timestamp(step.created_at),
                }
                for step in steps
            ],
            "model_invocations": [
                {
                    "model_invocation_id": invocation.id,
                    "episode_id": invocation.episode_id,
                    "step_id": invocation.step_id,
                    "provider": invocation.provider,
                    "model": invocation.model,
                    "tokens": invocation.tokens,
                    "cost": invocation.cost,
                    "latency_ms": invocation.latency_ms,
                    "request_hash": invocation.request_hash,
                    "status": invocation.status,
                    "error": invocation.error,
                    "configuration": invocation.configuration,
                    "attributions": [
                        {
                            "model_invocation_link_id": link.id,
                            "episode_id": link.episode_id,
                            "step_id": link.step_id,
                            "created_at": self._timestamp(link.created_at),
                        }
                        for link in links_by_invocation.get(invocation.id, [])
                    ],
                    "created_at": self._timestamp(invocation.created_at),
                }
                for invocation in invocations
            ],
            "effects": [
                {
                    "effect_id": effect.id,
                    "step_id": effect.step_id,
                    "attempt": effect.document,
                    "decision": (
                        decision_by_effect[effect.id].document
                        if effect.id in decision_by_effect
                        else None
                    ),
                    "virtual_result": (
                        decision_by_effect[effect.id].result
                        if effect.id in decision_by_effect
                        else None
                    ),
                    "created_at": self._timestamp(effect.created_at),
                }
                for effect in effects
            ],
            "verifier_events": [
                {
                    "verifier_event_id": verifier_event.id,
                    "verifier_id": verifier_event.verifier_id,
                    "signal": verifier_event.document,
                    "created_at": self._timestamp(verifier_event.created_at),
                }
                for verifier_event in verifier_events
            ],
            "trajectory_summaries": [
                {
                    "trajectory_id": trajectory.id,
                    "score": trajectory.score,
                    "document": trajectory.document,
                    "created_at": self._timestamp(trajectory.created_at),
                }
                for trajectory in trajectories
            ],
            "findings": [
                {
                    "finding_id": finding.id,
                    "verifier_id": finding.verifier_id,
                    "severity": finding.severity,
                    "status": finding.status,
                    "policy_version_id": finding.policy_version_id,
                }
                for finding in findings
            ],
            "metrics": self.repository.campaign_metrics(campaign_id),
        }
        bundle: dict[str, Any] = {
            "schema_version": "2.0",
            "generated_from_snapshot_at": self._timestamp(
                episode.completed_at or episode.created_at
            ),
            "content": content,
            "content_sha256": self._content_hash(content),
        }
        artifact = self.store.put_json("evidence", bundle)
        self.repository.save_artifact(
            artifact_id=artifact.artifact_id,
            kind="episode_evidence",
            uri=artifact.uri,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            campaign_id=campaign_id,
            episode_id=episode_id,
            metadata={
                "schema_version": "2.0",
                "content_sha256": bundle["content_sha256"],
                "target_version_id": target_version.id,
                "target_image_digest": target_version.image_digest,
                "attack_task_id": campaign.attack_task_id,
                "policy_version_id": campaign.policy_version_id,
                "red_config_id": campaign.red_config_id,
            },
        )
        if link_findings:
            self.repository.link_episode_findings_to_evidence(episode_id, artifact.artifact_id)
        return artifact

    def build_hardening_bundle(self, hardening_run_id: str) -> StoredArtifact:
        run = self.repository.get_hardening_run(hardening_run_id)
        if run is None:
            raise KeyError("hardening run not found")
        finding = self.repository.get_finding(run.finding_id)
        if finding is None:
            raise KeyError("finding not found")
        policy = self.repository.get_policy_version(run.policy_version_id)
        if policy is None:
            raise KeyError("policy version not found")
        campaign_ids = [
            campaign_id
            for campaign_id in (
                run.exact_replay_campaign_id,
                run.bypass_campaign_id,
                run.benign_campaign_id,
            )
            if campaign_id
        ]
        content = {
            "hardening_run": {
                "hardening_run_id": run.id,
                "status": run.status,
                "result": run.result,
                "created_at": self._timestamp(run.created_at),
                "completed_at": self._timestamp(run.completed_at),
            },
            "source_finding": {
                "finding_id": finding.id,
                "campaign_id": finding.campaign_id,
                "episode_id": finding.episode_id,
                "verifier_id": finding.verifier_id,
                "severity": finding.severity,
                "evidence_artifact_id": finding.evidence_artifact_id,
            },
            "policy_version": {
                "policy_version_id": policy.id,
                "version": policy.version,
                "policies": policy.policies,
                "content_sha256": policy.content_hash,
            },
            "campaign_results": [
                self.repository.campaign_metrics(campaign_id) for campaign_id in campaign_ids
            ],
            "proof": run.result.get("proof", {}),
            "lineage": {
                "exact_replay_campaign_id": run.exact_replay_campaign_id,
                "nearby_bypass_campaign_id": run.bypass_campaign_id,
                "benign_regression_campaign_id": run.benign_campaign_id,
            },
        }
        bundle = {
            "schema_version": "1.0",
            "content": content,
            "content_sha256": self._content_hash(content),
        }
        artifact = self.store.put_json("hardening", bundle)
        self.repository.save_artifact(
            artifact_id=artifact.artifact_id,
            kind="hardening_bundle",
            uri=artifact.uri,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            campaign_id=finding.campaign_id,
            episode_id=finding.episode_id,
            metadata={
                "hardening_run_id": run.id,
                "content_sha256": bundle["content_sha256"],
            },
            parent_artifact_id=finding.evidence_artifact_id,
        )
        self.repository.link_hardening_artifact(run.id, artifact.artifact_id)
        return artifact

    @staticmethod
    def replay_manifest(
        bundle: dict[str, Any], policy_version_id: str | None = None
    ) -> dict[str, Any]:
        if bundle.get("schema_version") == "2.0":
            content = bundle["content"]
            episode = content["episode"]
            campaign = content["campaign"]
            target = content["target"]
            return {
                "schema_version": "2.0",
                "source_campaign_id": campaign["campaign_id"],
                "source_episode_id": episode["episode_id"],
                "target_version_id": target["target_version_id"],
                "target_image_digest": target["image_digest"],
                "seed": episode["seed"],
                "actions": [step["red_action"] for step in content["steps"]],
                "attack_task_sha256": content["attack_task"]["content_sha256"],
                "red_config_id": (content["red_experiment_config"] or {}).get("red_config_id"),
                "policy_version_id": policy_version_id,
            }
        return {
            "schema_version": bundle["schema_version"],
            "campaign_id": bundle["campaign_id"],
            "source_episode_id": bundle["episode_id"],
            "seed": bundle["seed"],
            "actions": [step["red_action"] for step in bundle["steps"]],
            "policy_version_id": policy_version_id,
        }
