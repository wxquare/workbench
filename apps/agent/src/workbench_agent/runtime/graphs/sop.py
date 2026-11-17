"""Bounded local workflow evaluator; production Run integration is not enabled."""
import asyncio
import json
from dataclasses import dataclass
from graphlib import TopologicalSorter
from typing import Any
from workbench_agent.domain.workflows import WorkflowDefinition

@dataclass(frozen=True)
class StepResult:
    status: str
    output: dict[str, Any]

@dataclass(frozen=True)
class WorkflowResult:
    status: str
    steps: dict[str, StepResult]
    unresolved_items: tuple[str, ...]

async def execute_workflow(definition: WorkflowDefinition, invoke):
    """Run finite steps sequentially; caller owns authorization and dispatch fencing.

    This evaluator is not a Worker executor and cannot itself dispatch tools safely.
    """
    by_id = {step.step_id: step for step in definition.steps}
    order = TopologicalSorter({step.step_id: set(step.depends_on) for step in definition.steps}).static_order()
    results = {}
    unresolved = []
    stopped = False
    for name in order:
        step = by_id[name]
        if stopped or any(results[dep].status != 'ok' for dep in step.depends_on):
            results[name] = StepResult('blocked', {})
            if step.required:
                unresolved.append(f'{name}: blocked')
            continue
        inputs = {}
        missing = False
        for target, source in step.inputs.items():
            parent, field = source.split('.')
            if field not in results[parent].output:
                missing = True
                break
            inputs[target] = results[parent].output[field]
        if missing:
            result = StepResult('invalid_input', {})
        else:
            try:
                raw = await asyncio.wait_for(invoke(step, inputs), timeout=step.timeout_seconds)
                status, output = raw.get('status'), raw.get('output')
                if status not in ('ok', 'empty', 'unavailable', 'timeout', 'denied', 'invalid_input', 'unknown') or not isinstance(output, dict):
                    raise ValueError('invalid result')
                if len(json.dumps(output, ensure_ascii=False, allow_nan=False).encode()) > 65536:
                    raise ValueError('output too large')
                result = StepResult(status, output if status == 'ok' else {})
            except TimeoutError:
                result = StepResult('timeout', {})
            except (ValueError, TypeError, AttributeError):
                result = StepResult('unknown', {})
        results[name] = result
        if result.status != 'ok':
            if step.required:
                unresolved.append(f'{name}: {result.status}')
            if step.failure_policy == 'stop':
                stopped = True
    return WorkflowResult('partial' if unresolved else 'completed', results, tuple(unresolved))
