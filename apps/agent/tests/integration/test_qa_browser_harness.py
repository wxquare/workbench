import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


@pytest.mark.parametrize('mode', ['knowledge', 'direct'])
def test_browser_harness_serves_http_and_runs_separate_worker(mode):
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', 0))
        except PermissionError:
            pytest.skip('localhost listeners are unavailable in this sandbox')
        port = sock.getsockname()[1]
    script = Path(__file__).resolve().parents[1] / 'support' / 'qa_browser_harness.py'
    process = subprocess.Popen([sys.executable, str(script), '--port', str(port), '--mode', mode],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f'http://127.0.0.1:{port}'
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            deadline = time.monotonic() + 40
            while True:
                assert process.poll() is None
                try:
                    fixture = client.get(base + '/fixture').json()
                    break
                except (httpx.TransportError, ValueError):
                    assert time.monotonic() < deadline
                    time.sleep(.1)
            assert client.get(base + '/docs').status_code == 200
            assert client.get(base + '/app', headers={'Accept': 'text/html'}).status_code == 200
            headers = {'Authorization': 'Bearer ' + fixture['token'], 'Idempotency-Key': 'browser-test'}
            response = client.post(base + '/v1/runs', headers=headers, json={
                'agent_id': fixture['agent_id'], 'mode': 'qa',
                'input': {'text': 'What should I do before retrying?'},
                'business_context': {'query_scope': fixture['query_scope']} if mode == 'knowledge' else {}})
            assert response.status_code == 202
            run_id = response.json()['run_id']
            while True:
                result = client.get(base + '/v1/runs/' + run_id, headers=headers).json()
                if result['status'] in ('partial', 'succeeded', 'failed'):
                    break
                assert time.monotonic() < deadline
                time.sleep(.1)
            if mode == 'knowledge':
                assert result['status'] == 'partial'
                assert result['outcome']['findings'][0]['summary'] == 'Source excerpt: Check the queue before retrying.'
            else:
                assert result['status'] == 'succeeded'
                assert 'Fixture direct answer.' in result['outcome']['summary']
                assert result['outcome']['evidence_refs'] == []
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
