"""Agent se importuje **jako soubor**, ne jako balíček.

`track_agent.py` je jeden soubor bez instalace (README: *„nemá co
instalovat a nemá co se rozbít"*), takže se cesta k němu přidá sem a ne do
`pyproject.toml`, který repozitář nemá.
"""
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))
