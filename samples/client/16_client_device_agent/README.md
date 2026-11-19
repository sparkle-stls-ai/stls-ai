# 16_client_device_agent

The CLIENT half of the device pair used by
[`samples/server/16_server_device_agent/`](../../server/16_server_device_agent/README.md)
— `aq-sf-device-agent-16-client.json`/`_metadata.json` (gitignored,
like every other real device config in this repo; absolute
`DEVICE_METADATA_FILEPATH`/etc., unlike the relative-path convention
used elsewhere — see `ARQIT_PEERING_NOTES.md`'s cwd-pitfall follow-up
for why).

[`client_session.py`](./client_session.py) — one real, sequential A2A
session against a `server_consumer_*` replica, confirmed working
end-to-end. See the server sample's own README ("Client" section) for
the exact commands and prerequisites (device-agent + at least one
`server_consumer_*` already running).

[`client_concurrent.py`](./client_concurrent.py) — two DIFFERENT real
sessions resolved CONCURRENTLY, one per consumer replica
(`server_consumer_1` + `server_consumer_2`, both must be running):

```bash
cd samples/client/16_client_device_agent
python client_concurrent.py
```

Confirmed working end-to-end (2026-11-19): two distinct kids, two
concurrent `HTTP 200 OK` responses, no cross-talk between the two
consumer replicas' Redis reads.
