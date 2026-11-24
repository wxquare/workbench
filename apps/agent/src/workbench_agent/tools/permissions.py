"""Deployment-owned tool capability intersection, default deny at every layer."""


class DeploymentToolPermissions:
    def __init__(self, provider):
        self._provider = provider

    def allows(self, context, permission, expert):
        rules = self._provider()
        if not isinstance(rules, dict):
            return False
        skill_keys = [
            f'{ref.resource_id}:{ref.version}:{ref.content_hash}' for ref in context.skills
        ] or ['__none__']
        checks = (
            ('users', context.subject),
            ('teams', context.team_id),
            ('agents', context.agent_id),
            ('experts', expert),
        )

        def granted(layer, key):
            table = rules.get(layer, {})
            values = table.get(key, ()) if isinstance(table, dict) else ()
            return (
                isinstance(values, (list, tuple, set, frozenset))
                and all(isinstance(value, str) for value in values)
                and permission in values
            )

        return all(granted(layer, key) for layer, key in checks) and all(
            granted('skills', key) for key in skill_keys
        )
