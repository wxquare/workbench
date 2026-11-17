"""Read optional trusted workflow seeds without following links or oversized files."""
import os
import stat
import yaml
from workbench_agent.domain.workflows import WorkflowDefinition


def read_workflow_seeds(root):
    flags=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors=[]
    try:
        try:
            directory=os.open(root,flags); descriptors.append(directory)
            directory=os.open('config',flags,dir_fd=directory); descriptors.append(directory)
            directory=os.open('workflows',flags,dir_fd=directory); descriptors.append(directory)
        except FileNotFoundError:
            return ()
        names=sorted(name for name in os.listdir(directory) if name.endswith('.yaml'))
        if len(names)>32: raise ValueError('too many workflow seeds')
        results=[]
        for name in names:
            fd=os.open(name,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,dir_fd=directory)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size>65536: raise ValueError('invalid workflow seed')
                with os.fdopen(fd,'rb',closefd=False) as stream: raw=stream.read(65537)
                if len(raw)>65536: raise ValueError('workflow seed too large')
            finally:
                os.close(fd)
            try:
                definition=WorkflowDefinition.model_validate(yaml.safe_load(raw))
            except (yaml.YAMLError, ValueError, TypeError):
                raise ValueError('invalid workflow seed') from None
            if name[:-5] != definition.name: raise ValueError('workflow seed name mismatch')
            results.append(definition)
        if len({item.name for item in results}) != len(results): raise ValueError('duplicate workflow seed')
        return tuple(results)
    finally:
        for fd in reversed(descriptors): os.close(fd)
