import os
import sys
import numpy as np
import pmt
from gnuradio import gr


def _project():
    """Make the wavelink package importable (this file is generated into grc/ or build/grc/)."""
    here = os.path.dirname(os.path.abspath(globals().get("__file__", os.path.join(os.getcwd(), "x"))))
    for cand in (here, os.path.join(here, ".."), os.path.join(here, "..", ".."),
                 os.getcwd(), os.path.join(os.getcwd(), "..")):
        if os.path.isdir(os.path.join(cand, "wavelink")):
            cand = os.path.abspath(cand)
            if cand not in sys.path:
                sys.path.insert(0, cand)
            return True
    return "wavelink" in sys.modules


def _register(node_id, role, obj):
    try:
        if _project():
            from wavelink.grc_phy import register
            register(node_id, role, obj)
    except ImportError:
        pass                     # opened in GNU Radio Companion outside the project: ports only
