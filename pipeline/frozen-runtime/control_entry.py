"""Reference/empty controls also run offline in the expanded experiment."""
from pathlib import Path
import os
import sys

entry=Path(__file__).resolve().parent/'enter-expanded.py'
os.execv(sys.executable,[sys.executable,str(entry),'--offline-agent',*sys.argv[1:]])
