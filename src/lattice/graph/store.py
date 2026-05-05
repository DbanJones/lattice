"""GraphStore: persistence layer for the author graph and related entities.

See docs/HANDOFF.md step 3 and docs/DATA_MODEL.md for the full layout.

All writes are atomic (temp file + os.replace). The store knows nothing about
LLMs; it is pure persistence. Decisions (flag, edit) are logged append-only.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    AuditFlag,
    AuthorGraph,
    Claim,
    Cluster,
    EditProposal,
    Relationship,
    Section,
    Snapshot,
    SnapshotKind,
    Source,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GraphStore:
    """Persistent store for the author graph and related entities."""

    def __init__(self, project_path: Path) -> None:
        self.project_path = Path(project_path)
        self.lattice_dir = self.project_path / ".lattice"
        self.author_graph_path = self.lattice_dir / "author_graph.json"
        self.source_store_path = self.lattice_dir / "source_store.json"
        self.cluster_plan_path = self.lattice_dir / "cluster_plan.json"
        self.audit_flags_path = self.lattice_dir / "audit_flags.json"
        self.history_dir = self.lattice_dir / "author_graph_history"
        self.edit_proposals_dir = self.lattice_dir / "edit_proposals"
        self.flag_decisions_path = self.lattice_dir / "flag_decisions.json"
        self.edit_decisions_path = self.lattice_dir / "edit_decisions.json"
        self.runs_dir = self.lattice_dir / "runs"
        # Phase 7 — provenance + versioning. Bundled snapshots of the
        # full project state (graph, clusters, sources, audit flags)
        # taken before each major mutation, restorable as a unit.
        self.snapshots_dir = self.lattice_dir / "snapshots"

    @classmethod
    def load(cls, project_path: Path) -> "GraphStore":
        store = cls(project_path)
        store.lattice_dir.mkdir(parents=True, exist_ok=True)
        store.history_dir.mkdir(exist_ok=True)
        store.edit_proposals_dir.mkdir(exist_ok=True)
        store.runs_dir.mkdir(exist_ok=True)
        return store

    # ─── atomic write helper ─────────────────────────────

    def _atomic_write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        # Retry os.replace on Windows: OneDrive/antivirus can transiently
        # hold the target file open, which raises PermissionError.
        last_err: OSError | None = None
        for delay in (0, 0.05, 0.1, 0.2, 0.4):
            if delay:
                time.sleep(delay)
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last_err = exc
        if last_err is not None:
            raise last_err

    def _read_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    # ─── Author Graph ────────────────────────────────────

    def get_graph(self) -> AuthorGraph:
        if not self.author_graph_path.exists():
            now = _utcnow()
            return AuthorGraph(
                project_name=self.project_path.name,
                created_at=now,
                modified_at=now,
            )
        return AuthorGraph.model_validate_json(
            self.author_graph_path.read_text(encoding="utf-8")
        )

    def save_graph(self, graph: AuthorGraph) -> None:
        graph.modified_at = _utcnow()
        self._atomic_write_text(self.author_graph_path, graph.model_dump_json(indent=2))

    def snapshot(self, label: str | None = None) -> Path:
        """Copy current author_graph.json to author_graph_history/<ts>[_<label>].json."""
        if not self.author_graph_path.exists():
            raise FileNotFoundError("No author_graph.json to snapshot")
        self.history_dir.mkdir(exist_ok=True)
        ts = _utcnow().strftime("%Y-%m-%dT%H-%M-%S")
        name = f"{ts}_{label}.json" if label else f"{ts}.json"
        dest = self.history_dir / name
        dest.write_text(
            self.author_graph_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return dest

    # ─── Claims ──────────────────────────────────────────

    def save_claim(self, claim: Claim) -> None:
        graph = self.get_graph()
        claim.modified_at = _utcnow()
        for i, existing in enumerate(graph.claims):
            if existing.claim_id == claim.claim_id:
                graph.claims[i] = claim
                break
        else:
            graph.claims.append(claim)
        self.save_graph(graph)

    def get_claim(self, claim_id: str) -> Claim:
        for claim in self.get_graph().claims:
            if claim.claim_id == claim_id:
                return claim
        raise KeyError(f"Claim not found: {claim_id}")

    def list_claims(self, **filters: Any) -> list[Claim]:
        claims = self.get_graph().claims
        for key, value in filters.items():
            claims = [c for c in claims if getattr(c, key, None) == value]
        return claims

    def delete_claim(self, claim_id: str) -> None:
        graph = self.get_graph()
        graph.claims = [c for c in graph.claims if c.claim_id != claim_id]
        self.save_graph(graph)

    # ─── Sources ─────────────────────────────────────────

    def save_source(self, source: Source) -> None:
        sources = self.list_sources()
        for i, existing in enumerate(sources):
            if existing.source_id == source.source_id:
                sources[i] = source
                break
        else:
            sources.append(source)
        payload = json.dumps(
            {"sources": [s.model_dump(mode="json") for s in sources]},
            indent=2,
        )
        self._atomic_write_text(self.source_store_path, payload)

    def get_source(self, source_id: str) -> Source:
        for source in self.list_sources():
            if source.source_id == source_id:
                return source
        raise KeyError(f"Source not found: {source_id}")

    def list_sources(self) -> list[Source]:
        if not self.source_store_path.exists():
            return []
        data = self._read_json(self.source_store_path)
        return [Source.model_validate(s) for s in data.get("sources", [])]

    # ─── Sections ────────────────────────────────────────

    def save_section(self, section: Section) -> None:
        graph = self.get_graph()
        for i, existing in enumerate(graph.sections):
            if existing.section_id == section.section_id:
                graph.sections[i] = section
                break
        else:
            graph.sections.append(section)
        self.save_graph(graph)

    def list_sections(self) -> list[Section]:
        return self.get_graph().sections

    # ─── Clusters ────────────────────────────────────────

    def _load_clusters(self) -> list[Cluster]:
        if not self.cluster_plan_path.exists():
            return []
        data = self._read_json(self.cluster_plan_path)
        return [Cluster.model_validate(c) for c in data.get("clusters", [])]

    def save_cluster(self, cluster: Cluster) -> None:
        clusters = self._load_clusters()
        for i, existing in enumerate(clusters):
            if existing.cluster_id == cluster.cluster_id:
                clusters[i] = cluster
                break
        else:
            clusters.append(cluster)
        payload = json.dumps(
            {"clusters": [c.model_dump(mode="json") for c in clusters]},
            indent=2,
        )
        self._atomic_write_text(self.cluster_plan_path, payload)

    def get_cluster(self, cluster_id: str) -> Cluster:
        for cluster in self._load_clusters():
            if cluster.cluster_id == cluster_id:
                return cluster
        raise KeyError(f"Cluster not found: {cluster_id}")

    def list_clusters(self, section_id: str | None = None) -> list[Cluster]:
        clusters = self._load_clusters()
        if section_id is not None:
            clusters = [c for c in clusters if c.section_id == section_id]
        return clusters

    # ─── Relationships ───────────────────────────────────

    def save_relationship(self, rel: Relationship) -> None:
        graph = self.get_graph()
        for i, existing in enumerate(graph.relationships):
            if existing.rel_id == rel.rel_id:
                graph.relationships[i] = rel
                break
        else:
            graph.relationships.append(rel)
        self.save_graph(graph)

    def list_relationships(
        self,
        from_claim: str | None = None,
        to_claim: str | None = None,
        type_: str | None = None,
    ) -> list[Relationship]:
        rels = self.get_graph().relationships
        if from_claim is not None:
            rels = [r for r in rels if r.from_claim == from_claim]
        if to_claim is not None:
            rels = [r for r in rels if r.to_claim == to_claim]
        if type_ is not None:
            rels = [r for r in rels if r.type.value == type_]
        return rels

    # ─── Audit Flags ─────────────────────────────────────

    def save_audit_flags(self, voice_name: str, flags: list[AuditFlag]) -> None:
        all_flags: dict[str, list[dict]] = {}
        if self.audit_flags_path.exists():
            all_flags = self._read_json(self.audit_flags_path)
        all_flags[voice_name] = [f.model_dump(mode="json") for f in flags]
        self._atomic_write_text(self.audit_flags_path, json.dumps(all_flags, indent=2))

    def list_audit_flags(self, voice_name: str) -> list[AuditFlag]:
        if not self.audit_flags_path.exists():
            return []
        data = self._read_json(self.audit_flags_path)
        return [AuditFlag.model_validate(f) for f in data.get(voice_name, [])]

    def update_flag_decision(
        self, flag_id: str, decision: str, rationale: str | None = None
    ) -> None:
        if not self.audit_flags_path.exists():
            raise KeyError(f"Flag not found: {flag_id}")
        data = self._read_json(self.audit_flags_path)
        now_iso = _utcnow().isoformat()
        found = False
        for flags in data.values():
            for f in flags:
                if f.get("flag_id") == flag_id:
                    f["decision"] = decision
                    f["decision_at"] = now_iso
                    if rationale is not None:
                        f["decision_rationale"] = rationale
                    found = True
        if not found:
            raise KeyError(f"Flag not found: {flag_id}")
        self._atomic_write_text(self.audit_flags_path, json.dumps(data, indent=2))
        self._append_decision_log(
            self.flag_decisions_path,
            {
                "flag_id": flag_id,
                "decision": decision,
                "rationale": rationale,
                "at": now_iso,
            },
        )

    # ─── Edit Proposals ──────────────────────────────────

    def save_edit_proposals(
        self, cluster_id: str, proposals: list[EditProposal]
    ) -> None:
        self.edit_proposals_dir.mkdir(parents=True, exist_ok=True)
        path = self.edit_proposals_dir / f"{cluster_id}.json"
        payload = json.dumps(
            {
                "cluster_id": cluster_id,
                "proposals": [p.model_dump(mode="json") for p in proposals],
            },
            indent=2,
        )
        self._atomic_write_text(path, payload)

    def list_edit_proposals(
        self, cluster_id: str | None = None
    ) -> list[EditProposal]:
        if cluster_id is not None:
            path = self.edit_proposals_dir / f"{cluster_id}.json"
            if not path.exists():
                return []
            data = self._read_json(path)
            return [EditProposal.model_validate(p) for p in data.get("proposals", [])]
        proposals: list[EditProposal] = []
        if not self.edit_proposals_dir.exists():
            return proposals
        for path in sorted(self.edit_proposals_dir.glob("*.json")):
            data = self._read_json(path)
            proposals.extend(
                EditProposal.model_validate(p) for p in data.get("proposals", [])
            )
        return proposals

    def update_proposal_decision(self, proposal_id: str, decision: str) -> None:
        now_iso = _utcnow().isoformat()
        status_map = {
            "accepted": "accepted",
            "rejected": "rejected",
            "deferred": "deferred",
        }
        found = False
        for path in self.edit_proposals_dir.glob("*.json"):
            data = self._read_json(path)
            changed = False
            for p in data.get("proposals", []):
                if p.get("proposal_id") == proposal_id:
                    p["decision"] = decision
                    p["decision_at"] = now_iso
                    p["status"] = status_map.get(decision, "pending")
                    found = True
                    changed = True
            if changed:
                self._atomic_write_text(path, json.dumps(data, indent=2))
        if not found:
            raise KeyError(f"Edit proposal not found: {proposal_id}")
        self._append_decision_log(
            self.edit_decisions_path,
            {"proposal_id": proposal_id, "decision": decision, "at": now_iso},
        )

    def _append_decision_log(self, path: Path, entry: dict) -> None:
        log: list[dict] = []
        if path.exists():
            log = self._read_json(path)
        log.append(entry)
        self._atomic_write_text(path, json.dumps(log, indent=2))

    # ─── Snapshots (Phase 7) ─────────────────────────────

    def create_snapshot(
        self,
        kind: SnapshotKind = SnapshotKind.manual,
        *,
        actor: str = "user",
        message: str = "",
    ) -> Snapshot:
        """Capture the current project state as a single bundled
        snapshot and persist it to ``.lattice/snapshots/``.

        The bundle is restorable as a unit via ``revert_to_snapshot``.
        Snapshots are cheap-to-take but not free; the activity
        dispatcher creates one per major activity rather than per
        individual ``save_*`` call.
        """
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        now = _utcnow()
        # Stable, sortable snapshot ID. Microsecond suffix prevents
        # collisions when two snapshots are taken in the same second
        # (e.g. dispatcher snapshot followed by pre-revert snapshot).
        stamp = now.strftime("%Y%m%dT%H%M%S")
        snapshot_id = f"snap.{stamp}.{now.microsecond:06d}.{kind.value}"

        graph: AuthorGraph | None = None
        if self.author_graph_path.exists():
            graph = self.get_graph()
        clusters = self._load_clusters()
        sources = self.list_sources()

        audit_flags: dict[str, list[AuditFlag]] = {}
        if self.audit_flags_path.exists():
            try:
                raw = self._read_json(self.audit_flags_path)
                if isinstance(raw, dict):
                    for voice, flags in raw.items():
                        if isinstance(flags, list):
                            audit_flags[voice] = [
                                AuditFlag.model_validate(f) for f in flags
                                if isinstance(f, dict)
                            ]
            except (json.JSONDecodeError, OSError):
                pass

        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            kind=kind,
            created_at=now,
            actor=actor,
            message=message,
            graph=graph,
            clusters=clusters,
            sources=sources,
            audit_flags=audit_flags,
        )
        target = self.snapshots_dir / f"{snapshot_id}.json"
        self._atomic_write_text(target, snapshot.model_dump_json(indent=2))
        return snapshot

    def list_snapshots(self) -> list[Snapshot]:
        """Return every snapshot, newest first. Robust to partial
        files: a malformed snapshot is skipped rather than blowing
        up the whole listing."""
        if not self.snapshots_dir.exists():
            return []
        out: list[Snapshot] = []
        for path in self.snapshots_dir.glob("*.json"):
            try:
                out.append(Snapshot.model_validate_json(
                    path.read_text(encoding="utf-8")
                ))
            except Exception:  # pragma: no cover — corrupt-file resilience
                continue
        out.sort(key=lambda s: s.created_at, reverse=True)
        return out

    def load_snapshot(self, snapshot_id: str) -> Snapshot:
        target = self.snapshots_dir / f"{snapshot_id}.json"
        if not target.exists():
            raise KeyError(f"Snapshot not found: {snapshot_id}")
        return Snapshot.model_validate_json(
            target.read_text(encoding="utf-8")
        )

    def revert_to_snapshot(
        self, snapshot_id: str, *, take_pre_revert: bool = True,
    ) -> Snapshot:
        """Restore project state from a snapshot.

        Writes the embedded graph, clusters, sources, and audit
        flags back to their canonical paths. By default, takes a
        ``pre_revert`` snapshot first so the revert itself is
        recoverable (set ``take_pre_revert=False`` only when called
        from inside an automatic-revert flow that already snapshotted).
        Returns the restored snapshot.
        """
        snapshot = self.load_snapshot(snapshot_id)
        if take_pre_revert:
            self.create_snapshot(
                kind=SnapshotKind.pre_revert,
                actor="system",
                message=f"Pre-revert snapshot before restoring {snapshot_id}",
            )
        # Restore each artefact type. Missing entries in the snapshot
        # mean "this didn't exist when snapshotted" — clear the
        # canonical path rather than leaving stale state behind.
        if snapshot.graph is not None:
            self._atomic_write_text(
                self.author_graph_path,
                snapshot.graph.model_dump_json(indent=2),
            )
        elif self.author_graph_path.exists():
            self.author_graph_path.unlink()

        if snapshot.clusters:
            payload = json.dumps(
                {"clusters": [c.model_dump(mode="json") for c in snapshot.clusters]},
                indent=2,
            )
            self._atomic_write_text(self.cluster_plan_path, payload)
        elif self.cluster_plan_path.exists():
            self.cluster_plan_path.unlink()

        if snapshot.sources:
            payload = json.dumps(
                {"sources": [s.model_dump(mode="json") for s in snapshot.sources]},
                indent=2,
            )
            self._atomic_write_text(self.source_store_path, payload)
        elif self.source_store_path.exists():
            self.source_store_path.unlink()

        if snapshot.audit_flags:
            payload = json.dumps(
                {
                    voice: [f.model_dump(mode="json") for f in flags]
                    for voice, flags in snapshot.audit_flags.items()
                },
                indent=2,
            )
            self._atomic_write_text(self.audit_flags_path, payload)
        elif self.audit_flags_path.exists():
            self.audit_flags_path.unlink()

        return snapshot

    # ─── Token tracking ──────────────────────────────────

    def log_tokens(
        self, stage: str, run_id: str, input_tokens: int, output_tokens: int
    ) -> None:
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        tokens_path = run_dir / "tokens.json"
        data: dict[str, dict[str, int]] = {}
        if tokens_path.exists():
            data = self._read_json(tokens_path)
        stage_data = data.setdefault(stage, {"input": 0, "output": 0, "calls": 0})
        stage_data["input"] += input_tokens
        stage_data["output"] += output_tokens
        stage_data["calls"] += 1
        self._atomic_write_text(tokens_path, json.dumps(data, indent=2))

    def total_cost(self, run_id: str | None = None) -> dict[str, int]:
        if run_id is not None:
            run_dirs = [self.runs_dir / run_id]
        elif self.runs_dir.exists():
            run_dirs = [d for d in self.runs_dir.iterdir() if d.is_dir()]
        else:
            run_dirs = []
        total = {"input": 0, "output": 0, "calls": 0}
        for run_dir in run_dirs:
            tokens_path = run_dir / "tokens.json"
            if not tokens_path.exists():
                continue
            for stage_data in self._read_json(tokens_path).values():
                total["input"] += stage_data.get("input", 0)
                total["output"] += stage_data.get("output", 0)
                total["calls"] += stage_data.get("calls", 0)
        return total
