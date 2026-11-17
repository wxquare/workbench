"""Build the system instruction from the run's immutable profile snapshot."""

SYSTEM = ('Answer the user directly. Do not claim that you consulted external sources '
          'or verified facts unless provided in the question.')


def system_instruction(context):
    parts = [SYSTEM]
    if context.system_prompt.strip():
        parts.append('Agent instructions:\n' + context.system_prompt.strip())
    if context.response_language == 'zh':
        parts.append('Respond in Chinese.')
    elif context.response_language == 'en':
        parts.append('Respond in English.')
    if context.response_format == 'markdown':
        parts.append('Format the response as Markdown.')
    elif context.response_format == 'json':
        parts.append('Format the response as JSON.')
    return '\n\n'.join(parts)
