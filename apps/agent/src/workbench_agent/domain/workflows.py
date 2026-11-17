"""Immutable, finite workflow declarations. Data cannot embed executable code."""
import json
import math
from graphlib import CycleError, TopologicalSorter
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from jsonschema import Draft202012Validator

def validate_output_schema(schema):
    """Accept only bounded structural JSON Schema; never resolve remote code/refs."""
    if len(json.dumps(schema,allow_nan=False).encode()) > 16384: raise ValueError('output schema too large')
    allowed={'type','properties','required','additionalProperties','items','minItems','maxItems',
             'minLength','maxLength','minimum','maximum','enum','const'}
    nodes=[0]
    def walk(value,depth):
        nodes[0]+=1
        if nodes[0]>128 or depth>8 or not isinstance(value,dict) or set(value)-allowed:
            raise ValueError('unsupported output schema')
        if 'properties' in value:
            props=value['properties']
            if not isinstance(props,dict) or len(props)>64 or any(not isinstance(key,str) or len(key)>128 for key in props):
                raise ValueError('invalid output properties')
            for child in props.values(): walk(child,depth+1)
        if 'items' in value: walk(value['items'],depth+1)
        additional=value.get('additionalProperties',False)
        if isinstance(additional,dict): walk(additional,depth+1)
        elif type(additional) is not bool: raise ValueError('invalid additionalProperties')
    walk(schema,0)
    try: Draft202012Validator.check_schema(schema)
    except Exception: raise ValueError('invalid output schema') from None
    return schema

class StepCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    source: str
    equals: Any
    negate: bool = Field(default=False, strict=True)
    @model_validator(mode='after')
    def scalar_value(self):
        if type(self.equals) not in (str,int,float,bool,type(None)) or isinstance(self.equals,float) and not math.isfinite(self.equals):
            raise ValueError('condition value must be a finite JSON scalar')
        return self

class WorkflowStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    step_id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,63}$')
    tool: str = Field(min_length=1, max_length=128)
    required: bool = True
    depends_on: tuple[str, ...] = ()
    inputs: dict[str, str] = Field(default_factory=dict)
    arguments: dict[str, Any] = Field(default_factory=dict)
    condition: StepCondition | None = Field(default=None,exclude_if=lambda value:value is None)
    output_schema: dict[str, Any] | None = Field(default=None,exclude_if=lambda value:value is None)
    timeout_seconds: int = Field(default=20, ge=1, le=60)
    failure_policy: Literal['stop', 'continue'] = 'stop'

class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    name: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,127}$')
    steps: tuple[WorkflowStep, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def validate_graph(self):
        names = {step.step_id for step in self.steps}
        if len(names) != len(self.steps):
            raise ValueError('duplicate workflow step')
        dependencies = {}
        for step in self.steps:
            if step.output_schema is not None: validate_output_schema(step.output_schema)
            if step.condition is not None:
                source = step.condition.source.split('.')
                if len(source)!=2 or source[0] not in step.depends_on or not source[1].isidentifier():
                    raise ValueError('condition must reference a direct dependency output')
            if set(step.arguments) & set(step.inputs):
                raise ValueError('literal and mapped inputs must not overlap')
            if len(set(step.depends_on)) != len(step.depends_on) or not set(step.depends_on) <= names:
                raise ValueError('invalid workflow dependency')
            for variable, source in step.inputs.items():
                parts = source.split('.')
                if not variable.isidentifier() or len(parts) != 2 or parts[0] not in step.depends_on or not parts[1].isidentifier():
                    raise ValueError('input must reference a direct dependency output')
            dependencies[step.step_id] = set(step.depends_on)
        try:
            tuple(TopologicalSorter(dependencies).static_order())
        except CycleError:
            raise ValueError('workflow contains a cycle') from None
        return self

class WorkflowVersion(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    workflow_version_id: str
    agent_id: str
    version: int
    content_hash: str
    definition: WorkflowDefinition
