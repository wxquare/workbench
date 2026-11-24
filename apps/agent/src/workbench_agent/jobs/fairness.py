"""Bounded candidate ordering, priority first then round robin across teams."""

from collections import defaultdict, deque


class Fairness:
    def __init__(self):
        self.last_team: dict[int, str] = {}

    @staticmethod
    def level(candidate):
        kind = candidate['kind']
        if kind in {'resume', 'recover'}:
            return 0
        return {'alert': 2, 'schedule': 3}.get(kind, 1)

    def ordered(self, candidates):
        levels = defaultdict(lambda: defaultdict(deque))
        for candidate in candidates:
            levels[self.level(candidate)][candidate['team_id']].append(candidate)
        result = []
        for level, teams in sorted(levels.items()):
            order = sorted(teams, key=lambda team: (teams[team][0]['created_at'], team))
            if self.last_team.get(level) in order:
                index = order.index(self.last_team[level]) + 1
                order = order[index:] + order[:index]
            while any(teams.values()):
                for team in order:
                    if teams[team]:
                        result.append(teams[team].popleft())
        return result

    def claimed(self, candidate):
        self.last_team[self.level(candidate)] = candidate['team_id']
