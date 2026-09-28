import json
import logging
import re
from typing import Any, Optional

from app.schemas.incident import HistoricalMatch, IncidentAnalysisResult, IncidentRecord, RecommendationType
from app.services.hindsight_service import HindsightService, hindsight_service
from app.services.llm_service import LLMService, llm_service

logger = logging.getLogger("aftermath.reasoning")


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _actions(memory: dict) -> list[dict]:
    try:
        obj = json.loads(memory.get("content", "{}"))
        if isinstance(obj, dict):
            return obj.get("actions") or obj.get("attempts") or []
    except (TypeError, json.JSONDecodeError):
        pass
    meta = memory.get("metadata") or {}
    try:
        return json.loads(meta.get("actions_json", "[]"))
    except (TypeError, json.JSONDecodeError):
        return memory.get("actions") or []


class ContextComparisonEngine:
    """Conservative comparison. Historical recommendations require exact structured service, environment and error matches."""

    def __init__(self, memory: HindsightService = hindsight_service, llm: LLMService = llm_service):
        self.memory = memory
        self.llm = llm

    @staticmethod
    def _current_facts(incident: IncidentRecord) -> list[str]:
        facts = [
            f"Incident: {incident.id} — {incident.title}",
            f"Service: {incident.service}",
            f"Environment: {incident.environment}",
            f"Severity: {incident.severity}",
            f"Error: {incident.error_message}",
            f"Symptoms: {incident.symptoms}",
        ]
        if incident.impact:
            facts.append(f"Impact: {incident.impact}")
        if incident.recent_changes:
            facts.append(f"Recent changes: {incident.recent_changes}")
        if incident.dependencies:
            facts.append(f"Dependencies: {', '.join(incident.dependencies)}")
        if incident.context:
            facts.append(f"Context: {incident.context}")
        return facts

    @staticmethod
    def _memory_facts(memory: dict) -> dict:
        metadata = memory.get("metadata") or {}
        content = memory.get("content") or ""
        # Hindsight stores user metadata as strings. Parse only explicit labeled facts if metadata is absent.
        parsed = dict(metadata)
        labels = {
            "service": r"^Service:\s*(.+)$",
            "environment": r"^Environment:\s*(.+)$",
            "error_message": r"^Error(?: Code)?:\s*(.+)$",
            "symptoms": r"^Symptoms:\s*(.+)$",
            "recent_changes": r"^Recent changes:\s*(.+)$|^Recent Changes:\s*(.+)$",
        }
        for key, pattern in labels.items():
            if not parsed.get(key):
                match = re.search(pattern, content, re.I | re.M)
                if match:
                    parsed[key] = next((group for group in match.groups() if group), "")
        for field in ("recent_changes", "dependencies", "symptoms", "root_cause", "successful_fix"):
            if _norm(parsed.get(field)) in {"none", "none recorded", "not recorded", "not confirmed"}:
                parsed[field] = ""
        parsed.setdefault("incident_id", memory.get("incident_id") or memory.get("id"))
        parsed.setdefault("successful_fix", "")
        parsed.setdefault("root_cause", "")
        parsed.setdefault("dependencies", "")
        parsed.setdefault("title", "")
        lessons = parsed.get("lessons_learned") or parsed.get("lessons_json") or ""
        try:
            decoded = json.loads(lessons) if isinstance(lessons, str) else lessons
            parsed["lessons_learned"] = "; ".join(decoded) if isinstance(decoded, list) else str(decoded)
        except json.JSONDecodeError:
            parsed["lessons_learned"] = lessons
        return parsed

    @staticmethod
    def _symptom_phrases(current: str, historical: str) -> list[str]:
        current_words = re.findall(r"[a-z0-9]+", _norm(current))
        historical_words = re.findall(r"[a-z0-9]+", _norm(historical))
        historical_trigrams = {tuple(historical_words[i:i + 3]) for i in range(max(0, len(historical_words) - 2))}
        phrases = []
        for i in range(max(0, len(current_words) - 2)):
            phrase = tuple(current_words[i:i + 3])
            if phrase in historical_trigrams:
                rendered = " ".join(phrase)
                if rendered not in phrases:
                    phrases.append(rendered)
        return phrases

    @staticmethod
    def _matches(incident: IncidentRecord, memories: list[dict]) -> list[tuple[dict, dict, list[str]]]:
        matches = []
        for memory in memories:
            facts = ContextComparisonEngine._memory_facts(memory)
            same_service = _norm(facts.get("service")) == _norm(incident.service)
            same_environment = _norm(facts.get("environment")) == _norm(incident.environment)
            same_error = _norm(facts.get("error_message")) == _norm(incident.error_message)
            symptom_phrases = ContextComparisonEngine._symptom_phrases(incident.symptoms, facts.get("symptoms", ""))
            old_dependencies = {_norm(value) for value in str(facts.get("dependencies") or "").split(",") if _norm(value)}
            shared_dependencies = old_dependencies.intersection({_norm(value) for value in incident.dependencies if _norm(value)})
            similarities = [
                f"Service matches: {incident.service}." if same_service else "",
                f"Environment matches: {incident.environment}." if same_environment else "",
                f"Error matches: {incident.error_message}." if same_error else "",
                *(f"Symptom phrase appears in both records: '{phrase}'." for phrase in symptom_phrases),
                *(f"Dependency appears in both records: {item}." for item in sorted(shared_dependencies)),
            ]
            # Exact service/environment/error plus corroborating symptom phrase or dependency is required.
            if same_service and same_environment and same_error and (symptom_phrases or shared_dependencies):
                matches.append((memory, facts, [value for value in similarities if value]))
        return matches

    @staticmethod
    def _diffs(incident: IncidentRecord, facts: dict) -> list[str]:
        differences = []
        old_change, new_change = _norm(facts.get("recent_changes")), _norm(incident.recent_changes)
        if old_change != new_change and (old_change or new_change):
            differences.append(f"Recent changes differ. Historical: {facts.get('recent_changes') or 'not recorded'}. Current: {incident.recent_changes or 'none reported'}.")
        old_deps = {_norm(value) for value in str(facts.get("dependencies") or "").split(",") if _norm(value)}
        new_deps = {_norm(value) for value in incident.dependencies if _norm(value)}
        if old_deps != new_deps and (old_deps or new_deps):
            differences.append(f"Dependency context differs. Historical: {', '.join(sorted(old_deps)) or 'not recorded'}. Current: {', '.join(sorted(new_deps)) or 'none reported'}.")
        return differences

    def analyze_incident(self, incident: IncidentRecord) -> IncidentAnalysisResult:
        query = "\n".join(self._current_facts(incident))
        memory_status = self.memory.runtime_status()
        memories = self.memory.recall(query=query, organization_id=incident.organization_id, incident=incident.model_dump(mode="json"), limit=10)
        matched = self._matches(incident, memories)
        current_facts = self._current_facts(incident)

        if not matched:
            reason = "No sufficiently relevant historical experience was found."
            if memory_status["mode"] == "MEMORY_UNAVAILABLE":
                reason = "Historical memory is unavailable, so no historical recommendation can be made."
            return IncidentAnalysisResult(
                summary=reason,
                recommendation_type=RecommendationType.UNKNOWN,
                recommendation_summary="UNKNOWN / INSUFFICIENT EVIDENCE",
                detailed_recommendation=reason,
                confidence_level="insufficient_evidence",
                current_observations=current_facts,
                inference="The incident requires investigation using current operational evidence.",
                recommendation="Do not apply a historical fix based on this analysis.",
                key_similarities=[],
                key_differences=[],
                actions_to_avoid=[],
                historical_matches=[],
                historical_evidence=[],
                suggested_investigation_steps=["Inspect current service telemetry, logs, recent changes, and dependency health."],
                reasoning_evidence=[reason],
            )

        historical_evidence = []
        matches = []
        all_differences = []
        all_failed_actions = []
        all_failed_action_names = []
        all_reusable_fixes = []
        for memory, facts, evidence_similarities in matched:
            incident_id = str(facts.get("incident_id") or memory.get("id") or "historical-record")
            changes = self._diffs(incident, facts)
            all_differences.extend(changes)
            service_fact = f"Service matches: {incident.service}."
            env_fact = f"Environment matches: {incident.environment}."
            error_fact = f"Error matches: {incident.error_message}."
            similarities = list(dict.fromkeys([service_fact, env_fact, error_fact, *evidence_similarities]))
            actions = _actions(memory)
            failed = []
            for action in actions:
                outcome = str(action.get("result", "")).upper()
                if outcome == "FAILED":
                    all_failed_action_names.append(_norm(action.get("action")))
                    line = f"{action.get('action', 'Recorded action')} — FAILED"
                    if action.get("notes") or action.get("reason_or_why"):
                        line += f": {action.get('notes') or action.get('reason_or_why')}"
                    failed.append(line)
                    all_failed_actions.append(f"{incident_id}: {line}")
            fix = str(facts.get("successful_fix") or "").strip()
            if fix and not any(_norm(fix) == name or _norm(fix) in name or name in _norm(fix) for name in all_failed_action_names if name):
                all_reusable_fixes.append((incident_id, fix))
            evidence = {
                "incident_id": incident_id,
                "title": memory.get("title") or incident_id,
                "service": facts.get("service", ""),
                "environment": facts.get("environment", ""),
                "error_message": facts.get("error_message", ""),
                "symptoms": facts.get("symptoms", ""),
                "recent_changes": facts.get("recent_changes", ""),
                "dependencies": facts.get("dependencies", ""),
                "root_cause": facts.get("root_cause", ""),
                "successful_fix": fix,
                "lessons_learned": facts.get("lessons_learned", ""),
                "actions": actions,
                "source": memory.get("source", memory_status["mode"]),
            }
            historical_evidence.append(evidence)
            matches.append(HistoricalMatch(
                incident_id=incident_id,
                title=str(memory.get("title") or f"Resolved incident {incident_id}"),
                service=str(facts.get("service") or incident.service),
                similarity_score=None,
                similarities=similarities,
                differences=changes,
                failed_attempts=failed,
                successful_fix=fix or None,
                root_cause=str(facts.get("root_cause") or "") or None,
                lesson_learned=str(facts.get("lessons_learned") or "") or None,
            ))

        context_changed = bool(all_differences)
        if context_changed:
            state = RecommendationType.CAUTION
            recommendation = "Investigate the documented context difference before reusing a historical resolution."
            summary = "CAUTION — relevant history found, with a material context difference."
            inference = "The prior incident shares the same service, environment, and error, but its recorded context differs from the current incident."
            evidence_status = "relevant_historical_evidence"
        elif all_reusable_fixes:
            state = RecommendationType.REUSE
            fix_ids = ", ".join(dict.fromkeys(item[0] for item in all_reusable_fixes))
            fixes = "; ".join(dict.fromkeys(item[1] for item in all_reusable_fixes))
            recommendation = f"Consider the previously successful resolution recorded in {fix_ids}: {fixes}. Verify the current operational evidence before applying it."
            summary = "REUSE — matching historical experience is available; validate the fix against current evidence."
            inference = "The matched historical incidents agree on service, environment, error, and the recorded change/dependency context."
            evidence_status = "strong_historical_match"
        elif all_failed_actions:
            state = RecommendationType.AVOID
            recommendation = "Avoid repeating the failed actions listed in the historical evidence; no successful fix was recorded."
            summary = "AVOID — comparable history records failed actions and no verified successful fix."
            inference = "The historical record supports avoiding its failed actions, but does not support a successful resolution."
            evidence_status = "relevant_historical_evidence"
        else:
            state = RecommendationType.UNKNOWN
            recommendation = "No successful historical resolution is recorded; continue investigation using current evidence."
            summary = "UNKNOWN / INSUFFICIENT EVIDENCE"
            inference = "A relevant prior incident exists, but the stored record does not establish a successful resolution or failed action."
            evidence_status = "weak_historical_evidence"

        ai_inference, ai_citations = self._optional_llm_inference(incident, current_facts, historical_evidence)
        if ai_inference:
            inference = ai_inference
        avoid = list(dict.fromkeys(all_failed_actions))
        steps = ["Compare current telemetry and deployment state with the cited historical record."]
        if context_changed:
            steps.insert(0, "Validate the specific recent-change and dependency differences listed above before considering a prior fix.")
        if avoid:
            steps.append("Do not repeat failed actions without new evidence that the underlying conditions have changed.")
        return IncidentAnalysisResult(
            summary=summary,
            recommendation_type=state,
            recommendation_summary=summary,
            detailed_recommendation=recommendation,
            historical_matches=matches,
            key_similarities=list(dict.fromkeys(item for match in matches for item in match.similarities)),
            key_differences=list(dict.fromkeys(all_differences)),
            actions_to_avoid=avoid,
            suggested_investigation_steps=steps,
            confidence_level=evidence_status,
            reasoning_evidence=ai_citations or [f"Retrieved structured historical experience {item['incident_id']} with exact service, environment, and error match." for item in historical_evidence],
            current_observations=current_facts,
            historical_evidence=historical_evidence,
            inference=inference,
            recommendation=recommendation,
        )

    def _optional_llm_inference(self, incident: IncidentRecord, current_facts: list[str], evidence: list[dict]) -> tuple[Optional[str], list[str]]:
        if not self.llm.available or not evidence:
            return None, []
        sources = {f"CURRENT:{incident.id}": "\n".join(current_facts)}
        for item in evidence:
            sources[item["incident_id"]] = json.dumps(item, ensure_ascii=False)
        prompt = "Give at most two cautious inferences from only these sources. Cite exact quotes, using their source IDs. Do not recommend actions or add historical claims.\n" + json.dumps(sources, ensure_ascii=False)
        response = self.llm.generate_json(prompt)
        if not response:
            return None, []
        accepted, citations = [], []
        for entry in response.get("inferences", [])[:2]:
            text = entry.get("text") if isinstance(entry, dict) else None
            cites = entry.get("evidence", []) if isinstance(entry, dict) else []
            if not isinstance(text, str) or not text.strip() or not cites:
                continue
            valid = True
            for cite in cites:
                source, quote = cite.get("source_id"), cite.get("quote")
                if source not in sources or not isinstance(quote, str) or len(quote) < 8 or quote not in sources[source]:
                    valid = False
                    break
            if valid:
                accepted.append(text.strip())
                citations.extend([f"{cite['source_id']}: {cite['quote']}" for cite in cites])
        if not accepted:
            logger.warning("Gemini inference rejected: citations were missing or not exact source excerpts.")
            return None, []
        return " ".join(accepted), citations


reasoning_engine = ContextComparisonEngine()
