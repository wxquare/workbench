import pytest
from workbench_agent.runner.readiness import runner_readiness
from workbench_agent.runner.service import ContainerRunner


@pytest.mark.asyncio
async def test_runner_readiness_checks_identity_capacity_and_all_pinned_images(tmp_path):
    cli=tmp_path/'docker';log=tmp_path/'calls'
    cli.write_text('''#!/usr/bin/env python3
import sys
from pathlib import Path
Path('''+repr(str(log))+''').open('a').write(' '.join(sys.argv[1:])+'\\n')
if sys.argv[1]=='info' and sys.argv[-1]=='{{.ID}}': print('engine-A')
elif sys.argv[1]=='info': print('4 4294967296')
elif sys.argv[1:3]==['image','inspect']: print(sys.argv[-1])
else: sys.exit(2)
''');cli.chmod(0o755)
    runner=ContainerRunner(str(cli));image='sha256:'+'a'*64
    report=await runner_readiness(runner,'engine-A',[image])
    assert report=={'ready':True,'daemon':'ready','capacity':'ready','images':'ready'}
    assert 'image inspect' in log.read_text()
    assert 'pull ' not in log.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['wrong_daemon','low_capacity','missing_image'])
async def test_runner_readiness_fails_closed(tmp_path,case):
    cli=tmp_path/'docker'
    cli.write_text('''#!/usr/bin/env python3
import sys
case='''+repr(case)+'''
if sys.argv[1]=='info' and sys.argv[-1]=='{{.ID}}': print('wrong' if case=='wrong_daemon' else 'engine-A')
elif sys.argv[1]=='info': print('1 1073741824' if case=='low_capacity' else '4 4294967296')
elif sys.argv[1:3]==['image','inspect']:
    if case=='missing_image': sys.exit(1)
    print(sys.argv[-1])
else: sys.exit(2)
''');cli.chmod(0o755)
    report=await runner_readiness(ContainerRunner(str(cli)),'engine-A',['sha256:'+'a'*64])
    assert report['ready'] is False


@pytest.mark.asyncio
async def test_readiness_rejects_empty_config_and_daemon_change(tmp_path):
    cli=tmp_path/'docker';counter=tmp_path/'counter'
    cli.write_text('''#!/usr/bin/env python3
import sys
from pathlib import Path
p=Path('''+repr(str(counter))+''')
if sys.argv[1]=='info' and sys.argv[-1]=='{{.ID}}':
    n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); print('engine-A' if n==0 else 'engine-B')
elif sys.argv[1]=='info': print('4 4294967296')
elif sys.argv[1:3]==['image','inspect']: print(sys.argv[-1])
''');cli.chmod(0o755)
    runner=ContainerRunner(str(cli))
    assert not (await runner_readiness(runner,'engine-A',[]))['ready']
    result=await runner_readiness(runner,'engine-A',['sha256:'+'a'*64])
    assert result['ready'] is False and result['daemon']=='unavailable'


def test_runner_check_cli_reports_json_and_failure(monkeypatch, capsys):
    from types import SimpleNamespace
    import workbench_agent.cli.main as entry
    import workbench_agent.runner.readiness as readiness
    async def fake(runner,daemon,images):
        return {'ready':False,'daemon':'unavailable','capacity':'unknown','images':'unknown'}
    storage=SimpleNamespace(docker_binary=None,docker_host='unix:///tmp/docker.sock',runner_identity_mode='docker_engine',runner_daemon_id='engine-A',runner_bindings=[])
    monkeypatch.setattr(entry,'load_settings',lambda _:SimpleNamespace(storage=storage))
    monkeypatch.setattr(readiness,'runner_readiness',fake)
    monkeypatch.setattr('sys.argv',['workbench','runner','check','--config','example.yaml'])
    with pytest.raises(SystemExit) as error: entry.main()
    assert error.value.code==1
    assert '"ready": false' in capsys.readouterr().out
