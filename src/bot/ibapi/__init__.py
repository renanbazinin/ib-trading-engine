"""
Copyright (C) 2023 Interactive Brokers LLC. All rights reserved. This code is subject to the terms
 and conditions of the IB API Non-Commercial License or the IB API Commercial License, as applicable.
"""

""" Package implementing the Python API for the TWS/IB Gateway """

import os
import sys

# IB API protobuf modules use legacy absolute imports like
# `from protobuf.X_pb2 import ...` and `import Y_pb2`.
# Ensure both folders are discoverable in all runtimes (local and Docker).
_ibapi_dir = os.path.dirname(__file__)
_protobuf_dir = os.path.join(_ibapi_dir, "protobuf")

if _ibapi_dir not in sys.path:
    sys.path.insert(0, _ibapi_dir)

if _protobuf_dir not in sys.path:
    sys.path.insert(0, _protobuf_dir)

VERSION = {"major": 10, "minor": 35, "micro": 1}


def get_version_string():
    version = "{major}.{minor}.{micro}".format(**VERSION)
    return version


__version__ = get_version_string()
