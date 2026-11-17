"""A tracked-source wheel contains delivery without relying on local untracked files."""
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile


def test_tracked_source_wheel_includes_delivery(tmp_path):
    project=Path(__file__).resolve().parents[2]
    repo=Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=project, text=True).strip())
    relative=project.relative_to(repo)
    archive=subprocess.run(['git','archive','HEAD',str(relative)],cwd=repo,check=True,capture_output=True).stdout
    import io
    with tarfile.open(fileobj=io.BytesIO(archive),mode='r:') as tar:
        tar.extractall(tmp_path,filter='data')
    source=tmp_path/relative
    shutil.copyfile(project/'pyproject.toml',source/'pyproject.toml')
    wheel_dir=tmp_path/'wheels';wheel_dir.mkdir()
    subprocess.run([sys.executable,'-m','build','--wheel','--no-isolation','--outdir',str(wheel_dir),str(source)],
        check=True,capture_output=True)
    with zipfile.ZipFile(next(wheel_dir.glob('*.whl'))) as wheel:
        names=set(wheel.namelist())
        assert {'workbench_agent/delivery/dispatcher.py','workbench_agent/delivery/reconcile.py'} <= names
        assert not any(name.startswith(('skills/','prompts/','config/','web/')) for name in names)
