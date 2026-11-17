"""Validate extractive selections; source text is data, never an instruction."""
from pydantic import BaseModel, ConfigDict, Field


class EvidenceExcerpt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    evidence_ref: str = Field(min_length=1, max_length=128)
    quote: str = Field(min_length=1, max_length=1024)


class ExcerptSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    excerpts: list[EvidenceExcerpt] = Field(max_length=8)


def validate_selection(selection, sources):
    if not selection.excerpts:
        return None
    seen, accepted = set(), []
    for item in selection.excerpts:
        pair = (item.evidence_ref, item.quote)
        if (not item.quote.strip() or pair in seen or item.evidence_ref not in sources
                or not any(item.quote in text for text in sources[item.evidence_ref])):
            return None
        seen.add(pair)
        accepted.append(pair)
    return accepted


def source_texts(payload):
    """Extract only explicit data fields from known evidence payload shapes."""
    if not isinstance(payload, dict):
        return ()
    texts = []
    if payload.get('schema_version') == 'dod-tool-result-v1' and payload.get('status') == 'ok':
        data = payload.get('data')
        if isinstance(data, dict) and isinstance(data.get('matches'), list):
            for item in data['matches'][:10]:
                if isinstance(item, dict) and isinstance(item.get('content'), str):
                    texts.append(item['content'][:4096])
    elif all(key in payload for key in ('commit', 'path', 'sha256', 'lines')) and isinstance(payload['lines'], list):
        for line in payload['lines'][:100]:
            if isinstance(line, dict) and isinstance(line.get('text'), str):
                texts.append(line['text'][:4096])
    return tuple(text for text in texts if text.strip())
