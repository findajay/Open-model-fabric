"""Open Model Fabric — provider-neutral AI application platform.

Package layout (hexagonal architecture):

* ``omf.core``      — domain entities, ports (interfaces) and application services.
                      Must not import adapters, web frameworks or provider SDKs.
* ``omf.adapters``  — implementations of the ports (inference, compute, storage,
                      metadata, secrets, telemetry).
* ``omf.bootstrap`` — configuration loading and the composition root that wires
                      adapters into services.
"""

__version__ = "0.1.0"
