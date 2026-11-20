"""Deployment-pinned, non-executable tool argument mappings."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from workbench_agent.tools.model_schema import validate_model_schema


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')


class FieldMapping(_Frozen):
    origin: Literal['model', 'query_scope', 'grant', 'context']
    source: str = Field(min_length=1, max_length=128)
    target: tuple[str, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode='after')
    def safe_paths(self):
        for value in (self.source, *self.target):
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]{0,127}', value):
                raise ValueError('unsafe mapping path')
            if '..' in value:
                raise ValueError('unsafe mapping path')
        return self


class ArgumentMapping(_Frozen):
    schema_version: Literal['scope-map-v1'] = 'scope-map-v1'
    fields: tuple[FieldMapping, ...] = Field(min_length=1, max_length=64)
    model_input_schema: dict


def mapping_hash(mapping: ArgumentMapping) -> str:
    return hashlib.sha256(
        json.dumps(
            mapping.model_dump(mode='json'),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


def mapping_fingerprint(mapping: ArgumentMapping, full_schema: dict) -> str:
    """Pin both the approved declaration and the exact published input schema."""
    return hashlib.sha256(
        json.dumps(
            {
                'mapping': mapping.model_dump(mode='json'),
                'input_schema': full_schema,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


def _at(value: dict, path: str):
    current = value
    for part in path.split('.'):
        if not isinstance(current, dict) or part not in current:
            raise PermissionError('MAPPING_SOURCE_UNAVAILABLE')
        current = current[part]
    return current


def _schema_at(schema: dict, target: tuple[str, ...]):
    current = schema
    for index, part in enumerate(target):
        if not isinstance(current, dict):
            return None
        properties = current.get('properties')
        if not isinstance(properties, dict) or part not in properties:
            return None
        current = properties[part]
        if index < len(target) - 1 and current.get('type') != 'object':
            return None
    return current


def _policy_fields(policy: dict, origin: str) -> set[str]:
    key = {
        'model': 'model_input_fields',
        'query_scope': 'query_scope_fields',
        'grant': 'grant_fields',
        'context': 'context_fields',
    }[origin]
    values = policy.get(key)
    if not isinstance(values, list) or any(
        not isinstance(item, str) or not item for item in values
    ):
        raise ValueError('invalid mapping policy fields')
    return set(values)


def validate_mapping(mapping: ArgumentMapping, full_schema: dict, deployment_policy: dict) -> None:
    if not isinstance(mapping, ArgumentMapping) or not isinstance(full_schema, dict):
        raise ValueError('invalid mapping')
    if not isinstance(deployment_policy, dict):
        raise ValueError('missing mapping policy')
    if deployment_policy.get('mapping_hash') != mapping_hash(mapping):
        raise ValueError('mapping hash is not deployment-approved')
    if (
        deployment_policy.get('schema_hash') is not None
        and deployment_policy.get('schema_hash')
        != hashlib.sha256(
            json.dumps(
                full_schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            ).encode('utf-8')
        ).hexdigest()
    ):
        raise ValueError('input schema is not deployment-approved')
    validate_model_schema(mapping.model_input_schema)
    targets = [field.target for field in mapping.fields]
    for index, target in enumerate(targets):
        for other in targets[index + 1:]:
            if target == other:
                raise ValueError('duplicate mapping target')
            if len(target) < len(other) and other[:len(target)] == target:
                raise ValueError('mapping parent-child overlap')
            if len(other) < len(target) and target[:len(other)] == other:
                raise ValueError('mapping parent-child overlap')
    protected = {
        tuple(value)
        for value in deployment_policy.get('protected_targets', [])
        if isinstance(value, list)
    }
    for field in mapping.fields:
        if field.source not in _policy_fields(deployment_policy, field.origin):
            raise ValueError('mapping source is not deployment-approved')
        if field.target in protected and field.origin == 'model':
            raise ValueError('model cannot write protected target')
        spec = _schema_at(full_schema, field.target)
        if not isinstance(spec, dict) or spec.get('type') not in {
            'string',
            'integer',
            'number',
            'boolean',
        }:
            raise ValueError('mapping target is missing or non-scalar')
    for required in full_schema.get('required', []):
        if not any(field.target == (required,) for field in mapping.fields) and required not in {
            target[0] for target in targets if len(target) > 1
        }:
            raise ValueError('required target is not mapped')
    try:
        Draft202012Validator.check_schema(full_schema)
    except Exception:
        raise ValueError('invalid full schema') from None


def bind_arguments(
    mapping: ArgumentMapping,
    model_args: dict,
    *,
    query_scope: dict,
    grant: dict,
    context: dict,
) -> dict:
    if not isinstance(model_args, dict):
        raise PermissionError('INVALID_MODEL_ARGUMENTS')
    validate_model_schema(mapping.model_input_schema)
    if not Draft202012Validator(mapping.model_input_schema).is_valid(model_args):
        raise PermissionError('INVALID_MODEL_ARGUMENTS')
    allowed_model = {
        field.source for field in mapping.fields if field.origin == 'model'
    }
    if set(model_args) - allowed_model:
        raise PermissionError('UNDECLARED_MODEL_ARGUMENT')
    if any(not isinstance(value, (str, int, float, bool)) for value in model_args.values()):
        raise PermissionError('NON_SCALAR_MODEL_ARGUMENT')
    sources = {
        'model': model_args,
        'query_scope': query_scope,
        'grant': grant,
        'context': context,
    }
    result = {}
    for field in mapping.fields:
        value = _at(sources[field.origin], field.source)
        if not isinstance(value, (str, int, float, bool)):
            raise PermissionError('NON_SCALAR_MAPPED_VALUE')
        current = result
        for part in field.target[:-1]:
            child = current.setdefault(part, {})
            if not isinstance(child, dict):
                raise PermissionError('MAPPING_TARGET_CONFLICT')
            current = child
        current[field.target[-1]] = value
    return result
