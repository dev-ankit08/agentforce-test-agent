"""Makes the flat top-level modules importable from tests/ under pytest
(pytest would otherwise only put the tests/ dir on sys.path)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
