"""Durable local-only fake services. This module never performs network I/O."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from uuid import UUID,uuid5
from workbench_agent.application.idempotency import digest


def expected_external_id(action_id,kind):
    if kind not in LocalSimulator._TOOLS or str(UUID(action_id))!=action_id: raise ValueError('invalid simulated receipt identity')
    return 'sim-'+kind+'-'+str(uuid5(UUID(action_id),'local-simulator:'+kind))


class SimulatedLostResponse(Exception):
    """The local fake committed its receipt but suppressed its response."""


class LocalSimulator:
    _TOOLS={'jira':'CreateTicket','dod':'CreateDutyEvent','seatalk':'SendNotification'}

    def __init__(self,path,kind,*,lose_first_response=False,fail_before_send=0):
        path=Path(path)
        if not path.is_absolute() or path.is_symlink() or kind not in self._TOOLS:
            raise ValueError('explicit absolute local simulator path and kind required')
        self.path,self.kind=path,kind
        if type(fail_before_send) is not int or fail_before_send<0: raise ValueError('invalid simulated failure count')
        self.lose_first_response=lose_first_response
        self.fail_before_send=fail_before_send

    def _validate(self,payload,action_id):
        try:
            if str(UUID(action_id))!=action_id or not isinstance(payload,dict): raise ValueError()
            target=payload['target']
            if target!={'server':'local.mock.'+self.kind,'credential_scope':'local-mock','tool_name':self._TOOLS[self.kind]}:
                raise ValueError()
            if not isinstance(payload['arguments'],dict): raise ValueError()
            return digest(payload)
        except (ValueError,TypeError,KeyError,AttributeError): raise ValueError('invalid simulated destination') from None

    def _connect(self):
        # A dedicated marker prevents adding simulator tables to another database.
        if self.path.is_symlink(): raise ValueError('simulator database cannot be a symlink')
        existed=self.path.exists()
        connection=sqlite3.connect(str(self.path),timeout=5)
        try:
            if not existed:
                connection.execute('CREATE TABLE simulator_marker (marker TEXT PRIMARY KEY CHECK (marker = \'local-simulator-v1\'))')
                connection.execute("INSERT INTO simulator_marker VALUES ('local-simulator-v1')")
                connection.execute('CREATE TABLE simulator_receipts (kind TEXT NOT NULL, action_id TEXT NOT NULL, payload_hash TEXT NOT NULL, external_id TEXT NOT NULL, PRIMARY KEY (kind,action_id))')
                connection.commit()
            elif connection.execute('SELECT marker FROM simulator_marker').fetchall()!=[('local-simulator-v1',)]:
                raise ValueError('not a local simulator database')
            return connection
        except Exception:
            connection.close()
            raise

    def _write(self,payload_hash,action_id):
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload_hash,external_id FROM simulator_receipts WHERE kind=? AND action_id=?',(self.kind,action_id)).fetchone()
            if row and row[0]!=payload_hash: raise ValueError('action identity conflict')
            if row: return {'external_id':row[1]},False
            external_id=expected_external_id(action_id,self.kind)
            db.execute('INSERT INTO simulator_receipts VALUES (?,?,?,?)',(self.kind,action_id,payload_hash,external_id))
            return {'external_id':external_id},True

    async def send(self,payload,action_id):
        payload_hash=self._validate(payload,action_id)
        if self.fail_before_send:
            self.fail_before_send-=1
            from workbench_agent.delivery.dispatcher import DeliveryNotSent
            raise DeliveryNotSent('simulated pre-send failure')
        receipt,created=await asyncio.to_thread(self._write,payload_hash,action_id)
        if created and self.lose_first_response: raise SimulatedLostResponse()
        return receipt

    async def lookup(self,payload,action_id):
        payload_hash=self._validate(payload,action_id)
        if not self.path.exists(): return None
        def read():
            with closing(self._connect()) as db:
                row=db.execute('SELECT payload_hash,external_id FROM simulator_receipts WHERE kind=? AND action_id=?',(self.kind,action_id)).fetchone()
            if row and row[0]!=payload_hash: raise ValueError('action identity conflict')
            return {'external_id':row[1]} if row else None
        return await asyncio.to_thread(read)
