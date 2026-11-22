"""plays spotify on a spare device while youre away"""

from spotiafk.client import Client
from spotiafk.config import Config, ConfigError

__version__ = "3.0.0"
__all__ = ["Client", "Config", "ConfigError", "__version__"]
