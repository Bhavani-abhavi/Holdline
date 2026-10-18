"""HoldLine: an AI voice assistant that reserves real bakery inventory.

The package is organised as layers. Lower layers never import higher ones:

    config, exceptions, clock, logging_setup      (no dependencies)
    db, events                                     (storage)
    inventory, availability, holds, dietary,
    notifications, expiry                          (domain core, Phase 2)
    tools, guardrails, llm, agent                  (the agent, Phase 3)
    main, run                                      (HTTP interface, Phase 4)
"""

__version__ = "0.1.0"
