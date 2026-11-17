from sqlalchemy import insert, select, func
from workbench_agent.persistence.schema import teams

class SeedTeams:
    def __init__(self, uow):
        self.uow = uow
    async def add(self, name):
        await self.uow.execute(insert(teams).values(team_id=name, name=name, status="active"))
    async def count(self):
        return (await self.uow.execute(select(func.count()).select_from(teams))).scalar_one()
