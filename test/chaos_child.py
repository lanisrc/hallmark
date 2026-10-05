"""Run ``hm`` in a process that kills itself at a chosen point.

Used by test_chaos_crash.py; pytest does not collect this module.
``HALLMARK_CHAOS_KILL`` holds a JSON object:

* ``target``: ``"module:Class.method"`` or ``"module:function"`` to wrap;
* ``nth``: which matching call triggers the kill (1-based);
* ``when``: ``"before"`` or ``"after"`` the wrapped call runs;
* ``match``: optional text that must appear in the call's arguments.

The process sends itself SIGKILL, so no cleanup handlers run, as in a
power cut or an out-of-memory kill. Arguments are passed to ``hm``, or
``--api CODE`` runs Python with ``Repo`` imported, in the current folder.
"""

import importlib
import json
import os
import signal
import sys


def _install(spec):
    module_name, _, qualname = spec["target"].partition(":")
    owner = importlib.import_module(module_name)
    *parents, attribute = qualname.split(".")
    for name in parents:
        owner = getattr(owner, name)
    original = getattr(owner, attribute)
    count = 0

    def wrapper(*args, **kwargs):
        nonlocal count
        if spec.get("match", "") in " ".join(map(str, args)):
            count += 1
            if count == spec["nth"] and spec.get("when", "before") == "before":
                os.kill(os.getpid(), signal.SIGKILL)
            result = original(*args, **kwargs)
            if count == spec["nth"] and spec.get("when") == "after":
                os.kill(os.getpid(), signal.SIGKILL)
            return result
        return original(*args, **kwargs)

    setattr(owner, attribute, wrapper)


def main(argv):
    spec = os.environ.get("HALLMARK_CHAOS_KILL")
    if spec:
        _install(json.loads(spec))
    if argv[:1] == ["--api"]:
        from hallmark import Repo
        exec(argv[1], {"Repo": Repo})
        return 0
    from hallmark.cli import hallmark
    return hallmark.main(args=argv, prog_name="hm")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
