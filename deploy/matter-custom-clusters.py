"""Teach python-matter-server how to WRITE our own cluster.

WHY THIS EXISTS
---------------
Our switch carries a vendor cluster, 0xFFF1FC30, with two writable attributes:

    0xFFF10002  Locked  (boolean)  the switch ignores presses
    0xFFF10003  Role    (uint8)    0 = light, 1 = lock

Reading them works out of the box, because decoding TLV needs no schema. Writing
does not. matter-server encodes a write like this:

    attribute = ALL_ATTRIBUTES[cluster_id][attribute_id]()

`ALL_ATTRIBUTES` holds the 140 clusters the Matter SDK generates code for, and a
vendor cluster is in none of them - so the lookup raises `KeyError(4294048816)`,
which arrives at the panel as an "error" whose entire text is that number. The
lock silently stopped working when the panel moved from chip-tool, which could
write a custom attribute because you told it the type on the command line.

So we describe the two attributes ourselves and put them in the same table. The
classes are the same four fields the SDK generates for every attribute; there is
nothing clever here, only something missing.

INSTALLED VIA A `.pth` FILE, NOT `sitecustomize.py`
--------------------------------------------------
`sitecustomize` is a single name and Raspberry Pi OS already owns it: ours went
into the venv, `/usr/lib/python3.13/sitecustomize.py` won the import, and the
registration silently never ran - no error, because nothing had failed. A `.pth`
whose line starts with `import` is executed at startup too, and EVERY `.pth` in
site-packages runs, so nothing can shadow it.

    SP=/opt/smarthome/.venv-matter/lib/python3.13/site-packages
    sudo install -m644 deploy/matter-custom-clusters.py $SP/smarthome_clusters.py
    echo 'import smarthome_clusters' | sudo tee $SP/smarthome-clusters.pth
    sudo systemctl restart smarthome-matter

Neither file belongs to matter-server, so removing the two puts everything back
exactly as it was.

FAILURE
-------
Every line below runs while matter-server is starting, so nothing here may raise:
a traceback at this point would stop the service, and with it every light,
sensor and switch in the house. The whole thing is wrapped, and a failure costs
exactly what we had before - a lock that cannot be written.
"""

try:
    from dataclasses import dataclass

    from chip import ChipUtility
    from chip.clusters.ClusterObjects import (
        ALL_ATTRIBUTES,
        ClusterAttributeDescriptor,
        ClusterObjectFieldDescriptor,
    )

    _CLUSTER_ID = 0xFFF1FC30

    @dataclass
    class _Locked(ClusterAttributeDescriptor):
        @ChipUtility.classproperty
        def cluster_id(cls) -> int:  # noqa: N805 - the SDK's own shape
            return _CLUSTER_ID

        @ChipUtility.classproperty
        def attribute_id(cls) -> int:  # noqa: N805
            return 0xFFF10002

        @ChipUtility.classproperty
        def attribute_type(cls) -> ClusterObjectFieldDescriptor:  # noqa: N805
            return ClusterObjectFieldDescriptor(Type=bool)

        value: bool = False

    @dataclass
    class _Role(ClusterAttributeDescriptor):
        @ChipUtility.classproperty
        def cluster_id(cls) -> int:  # noqa: N805
            return _CLUSTER_ID

        @ChipUtility.classproperty
        def attribute_id(cls) -> int:  # noqa: N805
            return 0xFFF10003

        @ChipUtility.classproperty
        def attribute_type(cls) -> ClusterObjectFieldDescriptor:  # noqa: N805
            return ClusterObjectFieldDescriptor(Type=int)

        value: int = 0

    # setdefault, not assignment: if a future SDK ever generates this cluster
    # itself, its own definition wins and this becomes dead weight rather than a
    # silent override.
    ALL_ATTRIBUTES.setdefault(_CLUSTER_ID, {})
    ALL_ATTRIBUTES[_CLUSTER_ID].setdefault(0xFFF10002, _Locked)
    ALL_ATTRIBUTES[_CLUSTER_ID].setdefault(0xFFF10003, _Role)

except Exception as exc:  # noqa: BLE001 - see FAILURE above
    import sys

    print(f"custom cluster registration skipped: {exc}", file=sys.stderr)
