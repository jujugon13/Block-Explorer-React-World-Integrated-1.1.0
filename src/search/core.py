"""Definition-driven search pipeline skeleton for stage 7."""

from __future__ import annotations

import ast
import importlib
import json
import re
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


DEFAULT_DEFINITION = (
    Path(__file__).resolve().parents[2] / "specs" / "pipeline" / "search.pipeline.json"
)
_STEP_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class PipelineDefinitionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StepOutcome:
    passed: bool = True
    detail: dict[str, object] | None = None
    terminate: bool = False
    mode_trace_stages: tuple[int, ...] | None = None
    variant_query_count: int = 0
    extra_trace_executed: bool = False


class PipelineInterpreter:
    """Load step order, conditions, handlers, and trace names from one JSON file."""

    def __init__(
        self,
        definition: str | Path = DEFAULT_DEFINITION,
        *,
        handler_package: str = "src.search.steps",
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.definition_path = Path(definition)
        self.definition = json.loads(self.definition_path.read_text(encoding="utf-8"))
        self._timer = timer
        self._handler_package = handler_package
        self.steps = self._validated_steps(self.definition)
        self.trace_allowlist = tuple(self.definition["trace_name_allowlist"])
        self._handlers = {
            step["id"]: self._load_handler(step["id"]) for step in self.steps
        }

    @staticmethod
    def _validated_steps(definition: Mapping[str, object]) -> tuple[dict[str, object], ...]:
        raw = definition.get("steps")
        allowlist = definition.get("trace_name_allowlist")
        if not isinstance(raw, list) or not isinstance(allowlist, list):
            raise PipelineDefinitionError("steps and trace_name_allowlist are required")
        steps: list[dict[str, object]] = []
        ids: set[str] = set()
        orders: set[int] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise PipelineDefinitionError("each step must be an object")
            step_id, order = item.get("id"), item.get("order")
            if not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id):
                raise PipelineDefinitionError("invalid step id")
            if not isinstance(order, int) or isinstance(order, bool):
                raise PipelineDefinitionError("invalid step order")
            if step_id in ids or order in orders:
                raise PipelineDefinitionError("step ids and orders must be unique")
            ids.add(step_id)
            orders.add(order)
            steps.append(item)
        return tuple(sorted(steps, key=lambda step: int(step["order"])))

    def _load_handler(self, step_id: str) -> Callable[[MutableMapping[str, object]], object]:
        module = importlib.import_module(f"{self._handler_package}.{step_id}")
        handler = getattr(module, "handle", None)
        if not callable(handler):
            raise PipelineDefinitionError(f"step {step_id} has no handle function")
        return handler

    def run(
        self,
        state: MutableMapping[str, object] | None = None,
        *,
        settings: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        context = state if state is not None else {}
        config = dict(settings or {})
        context["settings"] = MappingProxyType(config)
        context["pipeline_definition"] = MappingProxyType(self.definition)
        trace: list[dict[str, object]] = []

        for step in self.steps:
            if not self._enabled(step, config, context):
                continue
            modes = step.get("modes")
            if isinstance(modes, dict):
                context["active_mode"] = self._selected_mode(step, config)
            trace_is_noop = self._trace_is_noop(step, config, context)
            started = self._timer()
            raw = self._handlers[str(step["id"])](context)
            finished = self._timer()
            outcome = self._outcome(raw)
            if not trace_is_noop:
                duration = float(
                    step.get("duration_ms_literal", max(0.0, (finished - started) * 1000.0))
                )
                for name in self._trace_names(step, config, context, outcome):
                    if name not in self.trace_allowlist:
                        raise PipelineDefinitionError(f"trace name is not allowed: {name}")
                    results = context.get("results")
                    trace.append(
                        {
                            "name": name,
                            "passed": outcome.passed,
                            "duration_ms": duration,
                            "results_count": len(results) if isinstance(results, (list, tuple)) else None,
                            "detail": outcome.detail,
                        }
                    )
            if outcome.terminate:
                break

        return {"state": context, "pipeline_trace": trace}

    @staticmethod
    def _outcome(value: object) -> StepOutcome:
        if value is None:
            return StepOutcome()
        if isinstance(value, StepOutcome):
            return value
        raise TypeError("step handlers return StepOutcome or None")

    def _enabled(
        self,
        step: Mapping[str, object],
        settings: Mapping[str, object],
        state: Mapping[str, object],
    ) -> bool:
        enabled_by = step.get("enabled_by")
        mandatory = step.get("always_run") is True or step.get("cannot_be_disabled") is True
        if not mandatory and isinstance(enabled_by, str) and not bool(settings.get(enabled_by, False)):
            return False
        precondition = step.get("precondition")
        return not isinstance(precondition, str) or self._condition(
            precondition, settings, state
        )

    def _trace_is_noop(
        self,
        step: Mapping[str, object],
        settings: Mapping[str, object],
        state: Mapping[str, object],
    ) -> bool:
        expression = step.get("no_op_when")
        return (
            step.get("no_op_emits_trace") is False
            and isinstance(expression, str)
            and self._condition(expression, settings, state)
        )

    def _trace_names(
        self,
        step: Mapping[str, object],
        settings: Mapping[str, object],
        state: Mapping[str, object],
        outcome: StepOutcome,
    ) -> tuple[str, ...]:
        names: list[str] = []
        modes = step.get("modes")
        if isinstance(modes, dict):
            active = str(state.get("active_mode", self._selected_mode(step, settings)))
            fallback = str(step.get("unknown_mode_fallback", ""))
            selected = modes.get(active, modes.get(fallback))
            if not isinstance(selected, dict) or not isinstance(selected.get("trace_names"), list):
                raise PipelineDefinitionError("mode step has no trace_names")
            selected_names = tuple(str(name) for name in selected["trace_names"])
            stages = selected.get("stages")
            if outcome.mode_trace_stages is not None and isinstance(stages, list):
                names.extend(
                    self._names_for_stages(
                        selected_names, stages, set(outcome.mode_trace_stages)
                    )
                )
            else:
                names.extend(selected_names)
            branches = step.get("branches")
            variant = branches.get("variant_query") if isinstance(branches, dict) else None
            variant_names: list[str] = []
            if isinstance(variant, dict):
                for key in ("run", "then"):
                    value = variant.get(key, ())
                    if isinstance(value, str):
                        variant_names.append(value)
                    elif isinstance(value, list):
                        variant_names.extend(str(item) for item in value)
            for _ in range(outcome.variant_query_count):
                names.extend(variant_names)
        extra = step.get("extra_trace_name")
        if isinstance(extra, str) and outcome.extra_trace_executed:
            names.append(extra)
        primary = step.get("trace_name")
        if isinstance(primary, str):
            names.append(primary)
        return tuple(names)

    def _selected_mode(
        self, step: Mapping[str, object], settings: Mapping[str, object]
    ) -> str:
        modes = step.get("modes")
        fallback = str(step.get("unknown_mode_fallback", ""))
        if not isinstance(modes, dict):
            return fallback
        override = self.definition.get("request_override")
        rules = override.get("rules", ()) if isinstance(override, dict) else ()
        candidates = [
            rule.get("target")
            for rule in rules
            if isinstance(rule, dict)
            and isinstance(rule.get("target"), str)
            and isinstance(settings.get(str(rule["target"])), str)
        ]
        requested = str(settings.get(str(candidates[0]), fallback)) if candidates else fallback
        return requested if requested in modes else fallback

    @staticmethod
    def _names_for_stages(
        trace_names: tuple[str, ...], stages: list[object], executed: set[int]
    ) -> tuple[str, ...]:
        """Partition a mode's declared traces at its declared stage-eval markers."""
        stage_numbers = [
            int(item["stage"])
            for item in stages
            if isinstance(item, dict) and isinstance(item.get("stage"), int)
        ]
        segments: dict[int, tuple[str, ...]] = {}
        start = 0
        for position, number in enumerate(stage_numbers):
            if position == len(stage_numbers) - 1:
                end = len(trace_names)
            else:
                marker = f"stage{number}"
                end = next(
                    (index + 1 for index in range(start, len(trace_names)) if marker in trace_names[index]),
                    start,
                )
            segments[number] = trace_names[start:end]
            start = end
        return tuple(
            name
            for number in stage_numbers
            if number in executed
            for name in segments.get(number, ())
        )

    def _condition(
        self,
        expression: str,
        settings: Mapping[str, object],
        state: Mapping[str, object],
    ) -> bool:
        results = state.get("results", ())
        document_ids = {
            item.get("document_id", item.get("documentId"))
            for item in results
            if isinstance(item, Mapping)
        } if isinstance(results, (list, tuple)) else set()
        values: dict[str, object] = {
            **settings,
            **state,
            "results": results,
            "answer_exists": bool(state.get("answer")),
            "distinct_document_count": len(document_ids),
        }
        normalized = expression.replace(" AND ", " and ").replace(" OR ", " or ")
        tree = ast.parse(normalized, mode="eval")
        allowed = (
            ast.Expression,
            ast.BoolOp,
            ast.And,
            ast.Or,
            ast.UnaryOp,
            ast.Not,
            ast.Compare,
            ast.Eq,
            ast.NotEq,
            ast.Lt,
            ast.LtE,
            ast.Gt,
            ast.GtE,
            ast.Name,
            ast.Load,
            ast.Constant,
            ast.Call,
        )
        for node in ast.walk(tree):
            if not isinstance(node, allowed):
                raise PipelineDefinitionError(f"unsupported condition: {expression}")
            if isinstance(node, ast.Call) and not (
                isinstance(node.func, ast.Name)
                and node.func.id == "len"
                and len(node.args) == 1
                and not node.keywords
            ):
                raise PipelineDefinitionError(f"unsupported condition call: {expression}")
            if isinstance(node, ast.Name) and node.id not in values and node.id != "len":
                raise PipelineDefinitionError(f"unknown condition name: {node.id}")
        return bool(self._evaluate(tree.body, values))

    def _evaluate(self, node: ast.AST, values: Mapping[str, object]) -> object:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.Call):
            return len(self._evaluate(node.args[0], values))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(self._evaluate(node.operand, values))
        if isinstance(node, ast.BoolOp):
            items = [bool(self._evaluate(item, values)) for item in node.values]
            return all(items) if isinstance(node.op, ast.And) else any(items)
        if isinstance(node, ast.Compare):
            left = self._evaluate(node.left, values)
            for operator, comparator in zip(node.ops, node.comparators):
                right = self._evaluate(comparator, values)
                if isinstance(operator, ast.Eq):
                    passed = left == right
                elif isinstance(operator, ast.NotEq):
                    passed = left != right
                elif isinstance(operator, ast.Lt):
                    passed = left < right
                elif isinstance(operator, ast.LtE):
                    passed = left <= right
                elif isinstance(operator, ast.Gt):
                    passed = left > right
                elif isinstance(operator, ast.GtE):
                    passed = left >= right
                else:  # pragma: no cover - filtered by the AST whitelist
                    raise PipelineDefinitionError("unsupported comparison")
                if not passed:
                    return False
                left = right
            return True
        raise PipelineDefinitionError("unsupported condition node")
