"""Provide the metered-device and 10s subsystem shared by host integrations.

The package never imports a host; ``DOMAIN`` is deliberately absent so each host owns it. Code outside a package imports
from the package's ``__init__`` (its public header), never from its files. Three layers, each importing only the ones
below: ``contract``; ``inbound`` and ``outbound/partners``; ``orchestration`` and ``setup_flow``.
"""