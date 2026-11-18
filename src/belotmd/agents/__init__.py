"""
Agent registry.

Two ways an agent gets here:

1. **In-process** — call `register("name")` on a factory, as `random_agent`
   does. Fine for an agent defined in your own program.

2. **Entry point** — a separately installed package declares

       [project.entry-points."belotmd.agents"]
       ppo = "my_package.agent:PPOAgent"

   and `pip install my-package` makes `belot-bot --agent ppo` work with no
   change to this library. This is how a heavyweight agent (torch, a
   checkpoint, a search engine) stays out of the SDK's dependency tree while
   still being a first-class citizen on the command line.

Entry points are resolved lazily: the target module is imported only when
that agent is actually built, so a broken or heavy plugin never slows down
`--list-agents` or the bot running some other agent.
"""

from .base import Agent, available, get_agent, register
from . import heuristic_agent  # noqa: F401  (registers "heuristic")
from . import random_agent  # noqa: F401  (registers "random")
from . import strong_agent  # noqa: F401  (registers "strong")
from . import strong2_agent  # noqa: F401  (registers "strong2")
from . import fallen_agent  # noqa: F401  (registers "fallen")
from . import fallen2_agent  # noqa: F401  (registers "fallen2" v3 expected-points)
from . import fallen3_agent  # noqa: F401  (registers "fallen3" mid-trick EXP + deeper EV)

__all__ = ["Agent", "available", "get_agent", "register"]
