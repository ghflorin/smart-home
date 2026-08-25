"""Cluster and attribute names, straight out of the Matter SDK.

Hand-maintaining this table would be thousands of lines and wrong within a
release. The SDK already has every id and every name; this just walks it.
"""
import json, inspect
from chip.clusters import Objects as C

out = {}
for name in dir(C):
    cl = getattr(C, name)
    if not inspect.isclass(cl) or not hasattr(cl, "id"):
        continue
    try:
        cid = int(cl.id)
    except Exception:
        continue
    attrs = {}
    A = getattr(cl, "Attributes", None)
    if A is not None:
        for an in dir(A):
            a = getattr(A, an)
            if inspect.isclass(a) and hasattr(a, "attribute_id"):
                try:
                    attrs[str(int(a.attribute_id))] = an
                except Exception:
                    pass
    cmds = {}
    K = getattr(cl, "Commands", None)
    if K is not None:
        for kn in dir(K):
            k = getattr(K, kn)
            if inspect.isclass(k) and hasattr(k, "command_id"):
                try:
                    cmds[str(int(k.command_id))] = kn
                except Exception:
                    pass
    out[str(cid)] = {"name": name, "attributes": attrs, "commands": cmds}

json.dump(out, open("/tmp/matter_names.json","w"), separators=(",",":"), sort_keys=True)
print("clusters:", len(out), " attributes:", sum(len(v["attributes"]) for v in out.values()))
