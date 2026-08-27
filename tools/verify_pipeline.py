#!/usr/bin/env python3
"""Verify that a debug pipeline trace stays within its JSON definition."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


def _read_json(value: str) -> object:
    if value == "-":
        return json.load(sys.stdin)
    if value.lstrip().startswith(("{", "[")):
        return json.loads(value)
    path = Path(value)
    return json.loads(path.read_text(encoding="utf-8"))


def _trace_from(payload: object) -> list[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        trace = payload.get("pipeline_trace")
        if isinstance(trace, list):
            return trace
        data = payload.get("data")
        if isinstance(data, dict):
            trace = data.get("pipeline_trace")
            if isinstance(trace, list):
                return trace
    raise ValueError("pipeline_trace 배열을 찾을 수 없습니다.")


def _step_trace_names(step: dict[str, object]) -> set[str]:
    names: set[str] = set()
    for key in ("extra_trace_name", "trace_name"):
        value = step.get(key)
        if isinstance(value, str):
            names.add(value)
    modes = step.get("modes")
    if isinstance(modes, dict):
        for mode in modes.values():
            if not isinstance(mode, dict):
                continue
            trace_names = mode.get("trace_names")
            if isinstance(trace_names, list):
                names.update(name for name in trace_names if isinstance(name, str))
    return names


def _execution_contract(
    definition: dict[str, object],
) -> tuple[
    dict[str, int],
    dict[int, dict[str, object]],
    list[tuple[str, ...]],
    list[str],
]:
    raw_steps = definition.get("steps")
    if not isinstance(raw_steps, list):
        return {}, {}, [], ["정의에 steps 배열이 없습니다."]

    errors: list[str] = []
    by_order: dict[int, dict[str, object]] = {}
    orders_by_name: dict[str, set[int]] = {}
    shared_duration_groups: list[tuple[str, ...]] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            errors.append(f"정의 steps[{index}]가 객체가 아닙니다.")
            continue
        order = raw.get("order")
        if isinstance(order, bool) or not isinstance(order, int):
            errors.append(f"정의 steps[{index}]의 order가 integer가 아닙니다.")
            continue
        if order in by_order:
            errors.append(f"정의의 단계 order {order}가 중복되었습니다.")
            continue
        by_order[order] = raw
        for name in _step_trace_names(raw):
            orders_by_name.setdefault(name, set()).add(order)

        modes = raw.get("modes")
        if isinstance(modes, dict):
            for mode in modes.values():
                if not isinstance(mode, dict):
                    continue
                shared = mode.get("shared_duration_ms")
                if (
                    isinstance(shared, list)
                    and len(shared) > 1
                    and all(isinstance(name, str) for name in shared)
                ):
                    group = tuple(shared)
                    if group not in shared_duration_groups:
                        shared_duration_groups.append(group)

    order_by_name: dict[str, int] = {}
    for name, orders in orders_by_name.items():
        if len(orders) != 1:
            errors.append(
                f"정의의 trace name {name!r}이 여러 실행 단계에 속합니다: {sorted(orders)}"
            )
        else:
            order_by_name[name] = next(iter(orders))
    return order_by_name, by_order, shared_duration_groups, errors


def _is_mandatory(step: dict[str, object]) -> bool:
    if step.get("always_run") is True or step.get("cannot_be_disabled") is True:
        return True
    return not isinstance(step.get("enabled_by"), str) and not isinstance(
        step.get("precondition"), str
    )


def _terminates_on_failure(step: dict[str, object], item: dict[str, object]) -> bool:
    if item.get("passed") is not False:
        return False
    for key in ("on_block", "on_failure"):
        policy = step.get(key)
        if isinstance(policy, dict) and policy.get("terminate") is True:
            return True
    return step.get("subsequent_steps_on_block") == "none"


def _terminates_with_empty_result(
    step: dict[str, object], item: dict[str, object], *, is_last: bool
) -> bool:
    policy = step.get("on_empty_result")
    return (
        is_last
        and item.get("results_count") == 0
        and isinstance(policy, dict)
        and policy.get("terminate") is True
    )


def verify_trace(trace: list[object], definition: dict[str, object]) -> list[str]:
    allowlist = definition.get("trace_name_allowlist")
    schema = definition.get("trace_item_schema")
    if not isinstance(allowlist, list) or not isinstance(schema, dict):
        return ["정의에 trace_name_allowlist 또는 trace_item_schema가 없습니다."]
    if not all(isinstance(name, str) for name in allowlist):
        return ["정의의 trace_name_allowlist는 문자열 배열이어야 합니다."]
    required = set(schema)
    allowed = set(allowlist)
    order_by_name, steps_by_order, shared_groups, contract_errors = (
        _execution_contract(definition)
    )
    errors: list[str] = list(contract_errors)
    names: list[str] = []
    valid_items: list[tuple[int, str, dict[str, object]]] = []

    for index, item in enumerate(trace):
        label = f"trace[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label}: 객체가 아닙니다.")
            continue
        missing = required - set(item)
        extra = set(item) - required
        if missing:
            errors.append(f"{label}: 필수 필드 누락 {sorted(missing)}")
        if extra:
            errors.append(f"{label}: 정의되지 않은 필드 {sorted(extra)}")
        name = item.get("name")
        if not isinstance(name, str) or name not in allowed:
            errors.append(f"{label}: 허용되지 않은 name {name!r}")
        elif name not in order_by_name:
            errors.append(f"{label}: 실행 단계에 연결되지 않은 name {name!r}")
        else:
            names.append(name)
            valid_items.append((index, name, item))
        if not isinstance(item.get("passed"), bool):
            errors.append(f"{label}: passed는 boolean이어야 합니다.")
        duration = item.get("duration_ms")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration < 0
        ):
            errors.append(f"{label}: duration_ms는 0 이상의 number여야 합니다.")
        count = item.get("results_count")
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or count < 0
        ):
            errors.append(f"{label}: results_count는 null 또는 0 이상의 integer여야 합니다.")
        detail = item.get("detail")
        if detail is not None and not isinstance(detail, dict):
            errors.append(f"{label}: detail은 null 또는 object여야 합니다.")

    positions = [order_by_name[name] for name in names]
    if positions != sorted(positions):
        errors.append("추적 이름 순서가 steps[].order 실행 순서를 벗어났습니다.")

    terminal_index: int | None = None
    terminal_order: int | None = None
    for index, name, item in valid_items:
        order = order_by_name[name]
        step = steps_by_order[order]
        literal = step.get("duration_ms_literal")
        if isinstance(literal, (int, float)) and not isinstance(literal, bool):
            duration = item.get("duration_ms")
            if (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and duration != literal
            ):
                errors.append(
                    f"trace[{index}]: {name} duration_ms는 정의값 {float(literal)}이어야 합니다."
                )
        if terminal_index is None and (
            _terminates_on_failure(step, item)
            or _terminates_with_empty_result(
                step, item, is_last=index == len(trace) - 1
            )
        ):
            terminal_index, terminal_order = index, order

    if terminal_index is not None and terminal_index + 1 < len(trace):
        errors.append(
            f"trace[{terminal_index}]: 종료 단계 뒤에 추적 항목이 존재합니다."
        )

    observed = set(names)
    for order, step in sorted(steps_by_order.items()):
        if terminal_order is not None and order >= terminal_order:
            break
        step_names = _step_trace_names(step)
        if _is_mandatory(step) and step_names and observed.isdisjoint(step_names):
            errors.append(
                f"필수 실행 단계 order {order}의 추적 항목이 없습니다: {sorted(step_names)}"
            )

    durations: dict[str, list[float]] = {}
    for _, name, item in valid_items:
        value = item.get("duration_ms")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            durations.setdefault(name, []).append(float(value))
    for group in shared_groups:
        paired = min((len(durations.get(name, ())) for name in group), default=0)
        for occurrence in range(paired):
            values = [durations[name][occurrence] for name in group]
            if any(value != values[0] for value in values[1:]):
                errors.append(
                    "공유 duration_ms 불일치: "
                    + ", ".join(
                        f"{name}={durations[name][occurrence]}" for name in group
                    )
                )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", required=True, help="debug 응답 JSON 파일, 인라인 JSON, 또는 -")
    parser.add_argument(
        "definition",
        nargs="?",
        default="specs/pipeline/search.pipeline.json",
        help="파이프라인 정의 JSON",
    )
    args = parser.parse_args(argv)
    try:
        payload = _read_json(args.trace)
        definition = json.loads(Path(args.definition).read_text(encoding="utf-8"))
        trace = _trace_from(payload)
        errors = verify_trace(trace, definition)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"검증 실패: {error}")
        return 1
    if errors:
        print("검증 실패")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"검증 통과: 추적 {len(trace)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
