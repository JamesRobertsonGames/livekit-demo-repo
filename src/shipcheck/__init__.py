"""Shipcheck voice agent."""

# Settings load first: they switch prometheus_client to multiprocess mode, which
# has to happen before livekit.agents imports prometheus_client.
from . import config as config
