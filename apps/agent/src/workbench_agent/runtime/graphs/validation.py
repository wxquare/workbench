"""Deterministic evidence checks, independent of model confidence."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    valid_refs: tuple[str, ...]
    invalid_refs: tuple[str, ...]
    missing_steps: tuple[str, ...]
    missing_scope: bool
    missing_evidence: bool


class EvidenceValidator:
    def validate(self, scope, evidence, required_steps, *, completed_steps, referenced_ids):
        missing_scope = not isinstance(scope, str) or not scope.strip()
        valid, invalid = [], []
        for reference in sorted(referenced_ids):
            row = evidence.get(reference)
            if (not missing_scope and row and row.get('evidence_id') == reference
                    and row.get('scope_id') == scope and row.get('quality') == 'valid'
                    and row.get('source') and row.get('reference_accessible') is True):
                valid.append(reference)
            else:
                invalid.append(reference)
        missing_steps = tuple(sorted(set(required_steps) - set(completed_steps)))
        missing_evidence = not valid
        return ValidationResult(not (missing_scope or missing_evidence or missing_steps or invalid),
                                tuple(valid), tuple(invalid), missing_steps, missing_scope, missing_evidence)
