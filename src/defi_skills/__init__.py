"""defi-skills — translate natural language into DeFi transactions."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("defi-skills")
except PackageNotFoundError:
    __version__ = "0.0.0"
