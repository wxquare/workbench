"""Explicit version pinning and bounded routing for expert implementations."""


class ExpertRegistry:
    def __init__(self):
        self._experts = {}

    def register(self, name, version, expert):
        if (
            name not in {'knowledge', 'diagnosis', 'code'}
            or not isinstance(version, str)
            or not version
        ):
            raise ValueError('invalid expert identity')
        if (name, version) in self._experts:
            raise ValueError('expert version already registered')
        self._experts[name, version] = expert

    def resolve(self, name, version):
        try:
            return self._experts[name, version]
        except KeyError:
            raise LookupError('pinned expert version unavailable') from None


_ROUTES = {
    'qa': ('knowledge',),
    'rca': ('diagnosis', 'knowledge'),
    'code': ('knowledge', 'code'),
    'sop': ('knowledge',),
    'inspection': ('diagnosis', 'knowledge'),
}


def select_experts(mode, published, *, classifier_choice=None, published_modes=frozenset()):
    """Explicit mode wins; automatic routing can only select a published mode."""
    if mode == 'auto':
        mode = (
            classifier_choice
            if classifier_choice in _ROUTES and classifier_choice in published_modes
            else None
        )
    if mode not in _ROUTES:
        return ()
    route = _ROUTES[mode]
    # A partial route cannot silently satisfy a missing required expert.
    return route if set(route) <= set(published) else ()
