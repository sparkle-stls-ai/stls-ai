# 16_client_device_agent

Holds only the CLIENT half of the device pair used by
[`samples/server/16_server_device_agent/`](../../server/16_server_device_agent/README.md)
— `aq-sf-device-agent-16-client.json`/`_metadata.json` (gitignored,
like every other real device config in this repo).

No script of its own: see that sample's README for how to launch
the device-agent and how to point `samples/client/10_client_ms/client_multi_session.py`
at this config to run a real client session against it.
