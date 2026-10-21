"""Gap-directed collection within the existing closer, never acceptance authority.

The existing readiness rail supplies the observations. This module classifies their
necessity, retains read progress and groups confirmed collector defects. The normal
closer still judges sufficiency and the native action guards still own effects.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

LEVELS = {"necessary", "useful", "nice_to_have", "possibly_useful"}
SECTIONS = {
    "source",
    "delivery_inventory",
    "diff_files",
    "review_conversations",
    "ci_artifacts",
    "native_merge_verifier",
}
CAUSES = {
    "producer_omission",
    "collector_contract",
    "parser_failure",
    "transient_api_failure",
    "legitimate_absence",
    "competing_pr_ambiguity",
    "unknown",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def classify_requirements(verdict, snapshot, packet, *, retained_dir=None):
    """Add necessity to the SAME readiness result; do not evaluate acceptance."""
    result = dict(verdict)
    rows = []
    action = verdict.get("action")
    sections = packet.get("sections", {}) if isinstance(packet, dict) else {}
    implementation = False
    if retained_dir is not None and isinstance(packet, dict):
        try:
            from closer_disposition import _receipt_index

            implementation = bool(_receipt_index(packet, Path(retained_dir)))
        except (KeyError, TypeError, ValueError, OSError):
            pass
    for gap in verdict.get("gaps", []):
        name = gap.split(":", 1)[0]
        section = {
            "delivery_pr": "delivery_inventory",
            "delivery": "source",
            "evidence": "diff_files",
        }.get(name, name)
        level = "necessary"
        # Checks and diagnostic enrichment help investigation. They are not proof
        # that the implementation is already accepted. Final guards are unchanged.
        if name == "checks":
            level = "useful" if implementation else "necessary"
        elif name == "diff_files" and implementation:
            level = "useful"
        elif name == "native_merge_verifier" and action == "collect_evidence" and implementation:
            level = "useful"
        row = {
            "id": digest([action, ":".join(gap.split(":")[:2])]),
            "gap": gap,
            "section": section,
            "necessity": level,
            "reason": gap,
            "origin": "readiness_rail",
        }
        if name == "evidence":
            row["alternatives"] = ["diff_files", "ci_artifacts"]
        rows.append(row)
    known = {r["section"] for r in rows}
    for name in ("ci_artifacts", "test_coverage_artifacts", "native_merge_verifier"):
        section = sections.get(name) or {}
        if name not in known and section.get("status") not in {"complete", "not_applicable"}:
            rows.append(
                {
                    "id": digest([action, name]),
                    "gap": name + ":optional_enrichment",
                    "section": name,
                    "necessity": (
                        "nice_to_have" if name != "native_merge_verifier" else "possibly_useful"
                    ),
                    "reason": section.get("reason") or "additional context",
                    "origin": "readiness_rail",
                }
            )
    result["requirements"] = rows
    result["evaluation_ready"] = not any(r["necessity"] == "necessary" for r in rows)
    return result


def validate_requests(proposal, snapshot, packet, screen):
    """A model may ask for a SPECIFIC extra, bound to a criterion or finding.

    Classification alone never makes an arbitrary artifact globally mandatory.
    """
    import closer_dispatcher
    import closer_disposition

    supplied = proposal.get("evidence_requests", [])
    if not isinstance(supplied, list) or len(supplied) > 20:
        raise ValueError("invalid evidence request inventory")
    allowed = {r["id"]: r for r in screen.get("requirements", [])}
    criteria = {
        (s["target"], hashlib.sha256(c.encode()).hexdigest())
        for s in snapshot.get("sources", [])
        for c in [s["body"], *closer_dispatcher.requirements(s["body"])]
    }
    findings = {
        r["finding_id"] for r in closer_disposition.pending_nonpass_findings(snapshot, packet)
    }
    out = []
    for request in supplied:
        if not isinstance(request, dict) or request.get("necessity") not in LEVELS:
            raise ValueError("evidence request must declare necessity")
        level = request["necessity"]
        reason = request.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("evidence request needs a bounded explanation")
        named = allowed.get(request.get("requirement_id"))
        if named:
            # A model cannot downgrade the rail's identity/raw-input requirements.
            if named["necessity"] == "necessary" and level != "necessary":
                raise ValueError("cannot downgrade a necessary rail requirement")
            if level == "necessary" and named["necessity"] != "necessary":
                raise ValueError(
                    "optional enrichment cannot become globally mandatory; name a specific locator and criterion"
                )
            row = {**named, "reason": reason, "necessity": level}
            selected_section = request.get("retrieval_section", named["section"])
            if selected_section != named["section"] and selected_section not in named.get(
                "alternatives", []
            ):
                raise ValueError("unsupported evidence alternative")
            row["section"] = selected_section
        else:
            locator = request.get("locator")
            if level != "necessary" and locator is None:
                out.append(
                    {
                        "id": digest([snapshot["target"], reason]),
                        "section": request.get("section", "suggestion"),
                        "necessity": level,
                        "reason": reason,
                        "origin": "closer_agent",
                    }
                )
                continue
            if (
                not isinstance(locator, dict)
                or locator.get("kind") != "job_log"
                or type(locator.get("job_id")) is not int
                or locator["job_id"] <= 0
                or set(locator) != {"kind", "job_id"}
            ):
                raise ValueError(
                    "extra evidence needs one exact supported locator; never all artifacts"
                )
            basis = request.get("basis") or {}
            if not isinstance(basis, dict):
                raise ValueError("evidence basis must be an object")
            criterion_sha = basis.get("criterion_sha256")
            if isinstance(basis.get("criterion"), str):
                criterion_sha = hashlib.sha256(basis["criterion"].encode()).hexdigest()
            valid_criterion = (basis.get("source"), criterion_sha) in criteria
            valid_finding = basis.get("finding_id") in findings
            if level == "necessary" and not valid_criterion and not valid_finding:
                raise ValueError(
                    "necessary extra must name an original criterion or current finding"
                )
            question = request.get("question")
            if level == "necessary" and (
                not isinstance(question, str) or not question.strip() or len(question) > 2000
            ):
                raise ValueError(
                    "necessary extra must explain the bounded question it prevents evaluating"
                )
            basis = (
                {"source": basis.get("source"), "criterion_sha256": criterion_sha}
                if valid_criterion
                else {"finding_id": basis.get("finding_id")}
            )
            row = {
                "id": digest([snapshot["target"], snapshot.get("head"), locator, basis]),
                "section": "requested_evidence",
                "necessity": level,
                "reason": reason,
                "question": request.get("question"),
                "locator": locator,
                "basis": basis,
                "origin": "closer_agent",
            }
        if row["id"] not in {x["id"] for x in out}:
            out.append(row)
    return out


def apply_requests(screen, requests, packet):
    result = dict(screen)
    rows = {r["id"]: r for r in screen.get("requirements", [])}
    observed = (packet.get("sections", {}).get("requested_evidence") or {}).get("content") or []
    for request in requests:
        if request["id"] in rows:
            continue
        if request.get("origin") == "readiness_rail":
            # Re-screening removed this requirement: retain its history without
            # recreating the old gap as an unsatisfied model extra.
            continue
        raw = next(
            (
                r
                for r in observed
                if r.get("requirement_id") == request["id"]
                and r.get("status") in {"complete", "partial"}
            ),
            None,
        )
        if raw:
            import closer_evidence

            ref = raw.get("raw_retained") or {}
            text = raw.get("raw")
            if (
                not closer_evidence._retained_ref_valid(ref)
                or raw.get("head") != packet.get("head")
                or raw.get("target") != packet.get("subject_pr")
                or raw.get("job_id") != request.get("locator", {}).get("job_id")
                or not isinstance(text, str)
                or ref.get("sha256") != hashlib.sha256(text.encode()).hexdigest()
                or ref.get("bytes") != len(text.encode())
            ):
                raw = None
        rows[request["id"]] = {
            **request,
            "satisfied": raw is not None,
            "observation_status": raw.get("status") if raw else "unavailable",
        }
    result["requirements"] = list(rows.values())
    result["evaluation_ready"] = not any(
        r["necessity"] == "necessary" and not r.get("satisfied") for r in rows.values()
    )
    return result


def required(screen):
    return [
        r
        for r in screen.get("requirements", [])
        if r["necessity"] == "necessary" and not r.get("satisfied")
    ]


@lru_cache(maxsize=1)
def collector_revision():
    import closer_dispatcher
    import closer_evidence

    return digest(
        [
            hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (closer_dispatcher, closer_evidence)
        ]
        + [hashlib.sha256(Path(__file__).read_bytes()).hexdigest()]
    )


def progress_fingerprint(packet, sections):
    """Raw retained progress, excluding failure wording, clocks and manifests."""
    import closer_evidence

    found = []

    def visit(value, context=()):
        if isinstance(value, dict):
            context = context + tuple(
                (k, str(value[k]))
                for k in (
                    "run_id",
                    "run_attempt",
                    "artifact_id",
                    "head_sha",
                    "filename",
                    "job_id",
                    "requirement_id",
                )
                if k in value
            )
            for key, child in value.items():
                if (
                    isinstance(child, dict)
                    and child.get("bytes", 0) > 0
                    and closer_evidence._retained_ref_valid(child)
                ):
                    found.append((key, child["sha256"], child["bytes"], context))
                elif key not in {"retained", "sha256", "reason", "unavailable"}:
                    visit(child, context)
        elif isinstance(value, list):
            for child in value:
                visit(child, context)

    for name in sorted(sections):
        visit((packet.get("sections", {}).get(name) or {}).get("content"), (("section", name),))
    return digest(sorted(found))


def binding(snapshot):
    return {
        **{k: snapshot.get(k) for k in ("target", "head", "base", "revision", "review_revision")},
        "collector_revision": collector_revision(),
    }


def compact_plan(plan, requests):
    if not plan:
        return None
    return {
        **plan,
        "evidence_requests": [
            (
                {
                    "requirement_id": r["id"],
                    "necessity": r["necessity"],
                    "reason": r["reason"],
                    "retrieval_section": r["section"],
                }
                if r.get("origin") == "readiness_rail"
                else {
                    k: r.get(k)
                    for k in ("necessity", "reason", "question", "locator", "basis", "section")
                }
            )
            for r in requests
        ],
    }


def restored_observations(latest, root):
    import closer_evidence

    rows = []
    for saved in latest.get("observations", {}).values():
        row = dict(saved)
        ref = row.get("raw_retained") or {}
        if ref:
            try:
                path = Path(ref["path"]).resolve()
                path.relative_to(Path(root).resolve())
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != ref.get("sha256") or len(raw) != ref.get(
                    "bytes"
                ):
                    raise ValueError("retained log observation differs")
                row["raw"] = raw.decode("utf-8")
            except (OSError, ValueError, KeyError, UnicodeDecodeError):
                row.update(
                    status="unavailable",
                    cause="unknown",
                    error_type="retained_observation_unavailable",
                )
        rows.append(row)
    return (
        closer_evidence._section(
            "complete" if all(r.get("status") == "complete" for r in rows) else "partial",
            rows,
            provenance={"acceptance_authority": False, "collector": "gap-directed-native-reads/v1"},
        )
        if rows
        else None
    )


class ReadProgress:
    """Retain completed read subrequests before a later page/archive is deferred.

    Only immutable positive observations in this exact source/review/head frame
    are reused. Empty, pending and malformed observations are not cached.
    """

    def __init__(self, root, snapshot, *, trigger=None):
        self.retained_root = Path(root)
        self.root = self.retained_root / "recovery_reads" / digest([binding(snapshot), trigger])
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _read(self, key):
        import closer_evidence

        path = self.root / (key + ".json")
        if not path.exists():
            return None
        ref = json.loads(path.read_bytes())
        if not closer_evidence._retained_ref_valid(ref):
            return None
        return Path(ref["path"]).read_bytes()

    def _save(self, key, raw, suffix):
        import closer_evidence

        ref = closer_evidence._retain_bytes(
            self.retained_root if suffix == "zip" else self.root, raw, suffix
        )
        closer_evidence._atomic_write(self.root / (key + ".json"), closer_evidence._bytes(ref))

    def api(self, call, endpoint, **kwargs):
        method = kwargs.get("method", "GET")
        if method != "GET" and not (
            endpoint == "graphql"
            and str((kwargs.get("fields") or {}).get("query", "")).lstrip().startswith("query")
        ):
            raise ValueError("read progress cannot replay a mutation")
        key = digest([endpoint, kwargs])
        prior = self._read(key)
        if prior is not None:
            return json.loads(prior)
        value = call(endpoint, **kwargs)
        connection = (
            (((value.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get(
                "reviewThreads"
            )
            if isinstance(value, dict)
            else None
        )
        cacheable = isinstance(value, dict) and (
            (
                type(value.get("id")) is int
                and value.get("head_sha")
                and value.get("status") == "completed"
            )
            or (
                isinstance(value.get("files"), list)
                and bool(value["files"])
                and all(isinstance(row.get("patch"), str) for row in value["files"])
            )
            or (isinstance(value.get("artifacts"), list) and 0 < len(value["artifacts"]) < 100)
            or (isinstance(connection, dict) and bool(connection.get("nodes")))
        )
        if cacheable:
            self._save(key, json.dumps(value, sort_keys=True).encode(), "json")
        return value

    def artifact(self, artifact, fetch, *, deadline, max_bytes):
        import io
        import zipfile

        if time.monotonic() >= deadline:
            raise TimeoutError("artifact request deadline exhausted")
        key = digest(["artifact", artifact.get("id")])
        prior = self._read(key)
        if prior is not None:
            if len(prior) > max_bytes:
                raise ValueError("retained archive exceeds current request bound")
            return prior
        raw = fetch(artifact, deadline=deadline, max_bytes=max_bytes)
        if zipfile.is_zipfile(io.BytesIO(raw)):
            self._save(key, raw, "zip")
        return raw


class RecoveryStore:
    """Durable read journal; no queue/claim/history/acceptance state is modified."""

    def __init__(self, root):
        self.root = Path(root) / "recovery"

    @contextmanager
    def locked(self, key):
        import closer_evidence

        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (key + ".json")
        with path.with_suffix(".lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            record = json.loads(path.read_text()) if path.exists() else {}
            try:
                yield record
            finally:
                closer_evidence._atomic_write(path, closer_evidence._bytes(record))

    def latest(self, snapshot):
        with self.locked(digest(binding(snapshot))) as record:
            return dict(record.get("latest") or {})

    def attempt_key(self, snapshot, packet, screen):
        observations = progress_fingerprint(packet, {r["section"] for r in required(screen)})
        return digest(
            [
                binding(snapshot),
                sorted(r["id"] for r in required(screen)),
                observations,
                screen.get("retry_trigger"),
            ]
        )

    @staticmethod
    def can_retry(attempt):
        if attempt.get("state") in {"not_started", "known_provider_failure"}:
            return True
        reset = attempt.get("reset_at")
        return (
            attempt.get("state") == "capacity_deferred"
            and isinstance(reset, (int, float))
            and not isinstance(reset, bool)
            and math.isfinite(reset)
            and time.time() >= reset
        )

    def defer_capacity(self, snapshot, key, reset_at):
        if (
            not isinstance(reset_at, (int, float))
            or isinstance(reset_at, bool)
            or not math.isfinite(reset_at)
        ):
            return
        with self.locked(digest(binding(snapshot))) as record:
            record["attempts"][key].update(state="capacity_deferred", reset_at=reset_at)
            record.setdefault("latest", {})["capacity_reset_at"] = reset_at

    def blocked(self, snapshot, packet, screen):
        key = self.attempt_key(snapshot, packet, screen)
        with self.locked(digest(binding(snapshot))) as record:
            reset = record.get("latest", {}).get("capacity_reset_at")
            if isinstance(reset, (int, float)) and math.isfinite(reset) and time.time() < reset:
                return True
            attempt = record.get("attempts", {}).get(key)
            return bool(attempt and not self.can_retry(attempt))

    def begin(self, snapshot, packet, screen):
        key = self.attempt_key(snapshot, packet, screen)
        with self.locked(digest(binding(snapshot))) as record:
            reset = record.get("latest", {}).get("capacity_reset_at")
            if isinstance(reset, (int, float)) and math.isfinite(reset) and time.time() < reset:
                return None
            attempts = record.setdefault("attempts", {})
            if key in attempts and not self.can_retry(attempts[key]):
                return None
            attempts[key] = {"state": "started", "requirements": required(screen)}
        return key

    def retryable(self, snapshot, key, state):
        if state not in {"not_started", "known_provider_failure"}:
            raise ValueError("unknown collection outcome cannot be retried")
        with self.locked(digest(binding(snapshot))) as record:
            record["attempts"][key]["state"] = state

    def finish(
        self, snapshot, key, *, prep, screen, requests, outcome, causes, plan=None, actual=None
    ):
        with self.locked(digest(binding(snapshot))) as record:
            attempt = record["attempts"][key]
            attempt.update(
                state="not_started" if outcome == "next_required_request" else "settled",
                outcome=outcome,
                remaining=required(screen),
                causes=causes,
            )
            record["latest"] = {
                "binding": binding(snapshot),
                "prepare": prep,
                "requests": requests,
                "remaining": required(screen),
                "outcome": outcome,
                "pending_plan": compact_plan(plan, requests) if outcome != "recovered" else None,
                "observations": (record.get("latest") or {}).get("observations", {}),
                "plan_actual": actual,
            }

    def observe(self, snapshot, key, row, *, prep, requests, plan, actual):
        with self.locked(digest(binding(snapshot))) as record:
            latest = record.setdefault("latest", {})
            latest.setdefault("prepare", prep)
            latest.update(
                binding=binding(snapshot),
                requests=requests,
                pending_plan=compact_plan(plan, requests),
                plan_actual={"run_id": actual.get("run_id")},
            )
            latest.setdefault("observations", {})[row["requirement_id"]] = {
                k: v for k, v in row.items() if k != "raw"
            }

    def defect(self, defect, case):
        with self.locked("root-" + defect["key"]) as record:
            record.setdefault("defect", defect)
            record.setdefault("cases", {})[digest(case)] = case


def collect_job_logs(gh, snapshot, requests, root, *, observe=None):
    """Only explicitly necessary exact-job reads; retain raw provenance and failure."""
    import closer_evidence

    subject = snapshot.get("pr_target", snapshot["target"])
    repo, number = subject.split("#")
    rows = []
    for request in requests:
        if (
            request["necessity"] != "necessary"
            or request.get("locator", {}).get("kind") != "job_log"
        ):
            continue
        job_id = request["locator"]["job_id"]
        row = {
            "requirement_id": request["id"],
            "request": request,
            "target": subject,
            "head": snapshot.get("head"),
        }
        try:
            job = gh.api(f"repos/{repo}/actions/jobs/{job_id}")
            run_id = job.get("run_id")
            if job.get("id") != job_id or type(run_id) is not int:
                raise ValueError("job identity differs")
            run = gh.api(f"repos/{repo}/actions/runs/{run_id}")
            if (
                run.get("id") != run_id
                or run.get("head_sha") != snapshot.get("head")
                or int(number) not in [r.get("number") for r in run.get("pull_requests", [])]
            ):
                raise ValueError("job run is not bound to the exact PR head")
            raw = gh.read_text(f"repos/{repo}/actions/jobs/{job_id}/logs")
            if not isinstance(raw, str):
                raise ValueError("log transport did not preserve text")
            original_sha = hashlib.sha256(raw.encode()).hexdigest()
            text, cut = closer_evidence._safe_text(raw)
            normalization_applied = text != raw
            text, redacted = closer_evidence._redact_credentials(text)
            ref = closer_evidence._retain_bytes(Path(root), text.encode(), "txt")
            row.update(
                status="partial" if cut or redacted or normalization_applied else "complete",
                raw_original_sha256=original_sha,
                raw_original_bytes=len(raw.encode()),
                normalization_applied=normalization_applied,
                job_id=job_id,
                run_id=run_id,
                run_attempt=run.get("run_attempt"),
                raw=text,
                raw_retained=ref,
                truncated=cut,
                redaction_applied=redacted,
                url=f"https://github.com/{repo}/actions/runs/{run_id}/job/{job_id}",
            )
        except Exception as exc:
            if closer_evidence._is_round_control_exception(exc):
                raise
            row.update(
                status="unavailable",
                error_type=type(exc).__name__,
                cause=(
                    "transient_api_failure"
                    if isinstance(exc, (TimeoutError, ConnectionError))
                    else "unknown"
                ),
            )
        rows.append(row)
        if observe is not None:
            observe(row)
    return rows


def confirmed_defects(before, after, causes, *, retained_dir):
    """A missing artifact is not a defect. Require a reproducible contract omission
    plus subsequently retained raw evidence at the same delivery identity.
    """
    if any(before.get(k) != after.get(k) for k in ("target", "subject_pr", "head", "base")):
        return []
    import closer_evidence
    from closer_disposition import _native_resolution_index, _receipt_index

    if not closer_evidence.validate_packet(before) or not closer_evidence.validate_packet(after):
        return []
    found = []
    for cause in causes:
        if cause.get("kind") not in {"producer_omission", "collector_contract", "parser_failure"}:
            continue
        name = cause.get("section")
        old, new = before["sections"].get(name) or {}, after["sections"].get(name) or {}
        if old.get("status") == "complete" or new.get("status") not in {"complete", "partial"}:
            continue
        reason = str(old.get("reason") or "")
        if (
            name == "native_merge_verifier"
            and "downloader unavailable" in reason
            and _native_resolution_index(
                after, Path(retained_dir), (new.get("content") or {}).get("verifier") or {}
            )
        ):
            family = "artifact-downloader-binding"
        elif (
            name == "ci_artifacts"
            and any(
                value.get("kind") != "codepatch"
                for group in _receipt_index(after, Path(retained_dir)).values()
                for value in (group if isinstance(group, list) else [group])
                if isinstance(value, dict)
            )
            and any(
                r.get("reason") == "artifact content unavailable" and r.get("expired") is False
                for r in (old.get("content") or {}).get("unavailable", [])
            )
        ):
            family = "artifact-downloader-binding"
        elif (
            name == "review_conversations"
            and "pagination" in reason
            and new.get("provenance", {}).get("pagination_complete") is True
            and any(
                closer_evidence._retained_ref_valid(comment.get("body_retained"))
                for thread in new.get("content", [])
                for comment in thread.get("comments", [])
            )
        ):
            family = "review-pagination-binding"
        else:
            continue
        found.append(
            {
                "key": digest(
                    ["stranske/Orchestrator", "closer_dispatcher.collect_native_evidence", family]
                ),
                "repository": "stranske/Orchestrator",
                "family": family,
                "component": "src/closer_dispatcher.py:collect_native_evidence",
                "regression": "A packet with the same input identity retains the required raw evidence through native and standalone collection.",
            }
        )
    return found


def causes_from(proposal):
    rows = proposal.get("collection_causes", [])
    if not isinstance(rows, list) or any(
        not isinstance(r, dict) or r.get("kind") not in CAUSES or r.get("section") not in SECTIONS
        for r in rows
    ):
        raise ValueError("invalid collection cause classification")
    return rows


COLLECTION_INSTRUCTIONS = (
    "You are the gap-directed second-pass evidence collection agent in the existing closer. "
    "Use the named necessary requirements and retained observations; do not request everything again. "
    "Return the normal target/head/base/classification/substantive_needs/findings envelope, plus "
    "evidence_requests: [{requirement_id, necessity, reason, retrieval_section}] selecting the supplied rail requirements and only their listed alternatives. "
    "For one specific missing job log, use locator:{kind:job_log,job_id:<exact ID>}, necessity, reason, "
    "question, and basis:{source,criterion_sha256} or {finding_id}. The coordinator performs bounded reads. "
    "Classify collection_causes [{section,kind,reason}] as producer_omission, collector_contract, "
    "parser_failure, transient_api_failure, legitimate_absence, competing_pr_ambiguity, or unknown. "
    "Confirmed producer/collector defects must be grouped upstream, searched against existing "
    "issues/PRs/improvement records, and filed or updated once with cases/provenance/regression expectations. "
    "The coordinator owns that idempotent filing after processing; never file per packet. An API "
    "failure, absent artifact, or competing PR is not by itself a bug. Upstream repair never blocks "
    "an individually recovered PR. Never grant acceptance or invent criteria. "
)


def case_pages(header, cases, *, limit=48000):
    pages, lines = [], list(header)
    for case in cases.values():
        target = case["target"]
        route = "/pull/" if case.get("subject_pr") == target else "/issues/"
        url = "https://github.com/" + target.replace("#", route)
        delivery = ""
        if case.get("subject_pr") and case["subject_pr"] != target:
            delivery = "; delivery https://github.com/" + case["subject_pr"].replace("#", "/pull/")
        line = (
            f"- {url}{delivery} head `{case.get('head')}`; before `{(case.get('before') or {}).get('sha256')}`, after `{(case.get('after') or {}).get('sha256')}`; requirements "
            + ", ".join(r["id"] for r in case.get("requirements", []))
        )
        if len(("\n".join(lines) + "\n" + line).encode()) > limit:
            if lines == header:
                raise ValueError("case provenance exceeds one API body")
            pages.append("\n".join(lines))
            lines = list(header)
        lines.append(line)
    pages.append("\n".join(lines))
    return [
        body.replace(header[0], header[0].replace(" -->", f":page:{index} -->")) if index else body
        for index, body in enumerate(pages)
    ]


def publish_case_pages(api, repo, number, pages, marker, publication, record, path):
    import adversarial

    for index, body in enumerate(pages):
        page_hash = digest(body)
        if index == 0 and publication.get("body_page0_digest") == page_hash:
            continue
        page = publication.setdefault("pages", {}).setdefault(str(index), {})
        if page.get("digest") == page_hash:
            continue
        page_marker = marker if index == 0 else marker.replace(" -->", f":page:{index} -->")
        comment_id = page.get("comment_id")
        if not comment_id:
            comments = api(f"repos/{repo}/issues/{number}/comments?per_page=100")
            if not isinstance(comments, list) or len(comments) >= 100:
                raise RuntimeError("upstream comment inventory incomplete")
            matches = [r for r in comments if page_marker in str(r.get("body") or "")]
            if len(matches) > 1:
                raise RuntimeError("multiple grouped report comments")
            if matches:
                comment_id = matches[0]["id"]
            elif page.get("state") == "create_pending":
                raise RuntimeError("grouped report comment outcome unresolved")
        if comment_id:
            api(f"repos/{repo}/issues/comments/{comment_id}", method="PATCH", fields={"body": body})
        else:
            page["state"] = "create_pending"
            adversarial._write_followup_record(path, record)
            comment = api(
                f"repos/{repo}/issues/{number}/comments", method="POST", fields={"body": body}
            )
            comment_id = comment.get("id") if isinstance(comment, dict) else None
            if type(comment_id) is not int or comment_id <= 0:
                raise RuntimeError("grouped report comment outcome unresolved")
        page.update(state="known", comment_id=comment_id, digest=page_hash)
        adversarial._write_followup_record(path, record)


def flush_defects(store, api, *, improvement_lookup=None):
    """One grouped reporting pass AFTER processing. Failures never block a PR.

    Reuse the existing duplicate matcher, complete-search guard and durable
    pre-POST writer. Never invoke the adversarial source-closure actor.
    """
    from types import SimpleNamespace

    import adversarial
    import improvement_log
    import keepalive_evidence

    improvement_lookup = improvement_lookup or improvement_log.search
    results = []
    if not store.root.exists():
        return results
    for path in sorted(store.root.glob("root-*.json")):
        key = path.stem
        try:
            with store.locked(key) as record:
                defect = record["defect"]
                repo = defect["repository"]
                if repo not in {"stranske/Orchestrator", "stranske/Workflows"}:
                    raise ValueError("upstream owner is outside the authorized repositories")
                family, root_key = defect["family"], defect["key"]
                cases = record.get("cases", {})
                case_digest = digest(cases)
                publication = record.setdefault("publication", {})
                if (
                    publication.get("cases_digest") == case_digest
                    and publication.get("state") == "published"
                ):
                    results.append(
                        {"key": root_key, "status": "unchanged", "url": publication["url"]}
                    )
                    continue
                title = "Closer evidence contract: " + family
                marker = f"<!-- closer-evidence-root:{root_key} -->"

                def search(query):
                    answer = api(
                        "search/issues?q="
                        + quote(f"repo:{repo} {query}", safe="")
                        + "&per_page=100",
                        resource="search",
                    )
                    rows = answer.get("items") if isinstance(answer, dict) else None
                    if (
                        not isinstance(rows, list)
                        or answer.get("incomplete_results") is not False
                        or answer.get("total_count") != len(rows)
                        or len(rows) >= 100
                    ):
                        raise RuntimeError("upstream search is incomplete; no create permitted")
                    if any(
                        not isinstance(r, dict)
                        or type(r.get("number")) is not int
                        or not isinstance(r.get("title"), str)
                        or not isinstance(r.get("body"), str)
                        for r in rows
                    ):
                        raise RuntimeError("upstream search is malformed; no create permitted")
                    return rows

                def search_runner(_argv):
                    rows = search('"' + title + '" in:title')
                    return SimpleNamespace(returncode=0, stdout=json.dumps(rows))

                exact = adversarial._find_followup(
                    search_runner, repo, title, root_key, marker_prefix="closer-evidence-root"
                )
                number = exact or publication.get("number")
                if not number and publication.get("state") == "create_pending":
                    results.append({"key": root_key, "status": "create_outcome_unresolved"})
                    continue
                if not number:
                    existing = search("collector in:title,body")
                    candidate = {
                        "seed": "Closer collector bindings",
                        "evidence": {"title": "Closer collector bindings"},
                    }
                    keepalive_evidence._dedupe_candidate(
                        candidate,
                        repo,
                        issue_search_fn=lambda *_a: [
                            {**r, "title": r["title"] + "\n" + r["body"]} for r in existing
                        ],
                        linked_issues_fn=lambda *_a: [],
                    )
                    improvement = improvement_lookup(family, limit=20)
                    record["improvement_lookup"] = {
                        k: improvement.get(k)
                        for k in ("present", "absent_reason", "total_matches", "truncated")
                    }
                    if candidate.get("possible_duplicate"):
                        match = next(r for r in existing if r["number"] == candidate["dup_issue"])
                        text = match["body"].lower()
                        if (
                            not (
                                "collector bindings" in text
                                and "native" in text
                                and "standalone" in text
                            )
                            and marker not in match["body"]
                        ):
                            results.append(
                                {
                                    "key": root_key,
                                    "status": "candidate_needs_root_confirmation",
                                    "candidate": candidate["dup_issue"],
                                }
                            )
                            continue
                        number = candidate["dup_issue"]
                        publication["url"] = (
                            match.get("html_url") or f"https://github.com/{repo}/issues/{number}"
                        )
                    elif improvement.get("total_matches"):
                        results.append({"key": root_key, "status": "review_existing_improvement"})
                        continue
                lines = [
                    marker,
                    "Confirmed closer evidence contract gap: " + family,
                    "",
                    "Component: " + defect["component"],
                    "Regression expectation: " + defect["regression"],
                    "",
                    "Affected cases (retained packets remain authoritative):",
                ]
                pages = case_pages(lines, cases)
                if not number:
                    publication.update(state="create_pending")
                    adversarial._write_followup_record(path, record)
                    made = api(
                        f"repos/{repo}/issues",
                        method="POST",
                        fields={"title": title, "body": pages[0]},
                    )
                    number = made.get("number") if isinstance(made, dict) else None
                    if type(number) is not int or number <= 0:
                        raise RuntimeError("upstream issue create outcome unresolved")
                    publication.update(
                        number=number,
                        url=f"https://github.com/{repo}/issues/{number}",
                        state="issue_known",
                        body_page0_digest=digest(pages[0]),
                    )
                    adversarial._write_followup_record(path, record)
                else:
                    publication.update(
                        number=number,
                        url=publication.get("url") or f"https://github.com/{repo}/issues/{number}",
                    )
                publish_case_pages(api, repo, number, pages, marker, publication, record, path)
                publication.update(state="published", cases_digest=case_digest)
                results.append({"key": root_key, "status": "published", "url": publication["url"]})
        except Exception as exc:
            results.append(
                {"key": key, "status": "reporting_deferred", "error_type": type(exc).__name__}
            )
    return results
