from pydantic import ValidationError

def validate_json_output(text, schema, finish_reason=None):
    if not isinstance(text,str) or not text.strip() or finish_reason=='length':
        raise ValueError('empty or truncated structured output')
    try: return schema.model_validate_json(text,strict=True)
    except (ValueError,TypeError,ValidationError):
        raise ValueError('invalid structured output') from None
