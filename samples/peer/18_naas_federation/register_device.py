"""
register_device.py — samples/peer/18_naas_federation (issue #38)
------------------------------------------------------------------
Registers and fully provisions ONE real Arqit device, writing its
config+metadata into this sample's own devices/ directory (gitignored
— contains real credentials). Run once per device in the table below
(see this sample's README, "Running with arqit_py"):

  python register_device.py sf-p18-ab-rfq-client   9101
  python register_device.py sf-p18-ab-rfq-server   9101
  python register_device.py sf-p18-ab-reply-client 9103
  python register_device.py sf-p18-ab-reply-server 9103
  python register_device.py sf-p18-ac-rfq-client   9105
  python register_device.py sf-p18-ac-rfq-server   9105
  python register_device.py sf-p18-ac-reply-client 9107
  python register_device.py sf-p18-ac-reply-server 9107

The port is each device PAIR's shared SOCKET_LISTENING_PORT (Arqit's
own bilocation/peering socket addressing — a CLIENT and its paired
SERVER must use the SAME port; see the README for why). This script
only sets each device's own listening port; SOCKET_RECEIVER_IP_ADDRESS/
PORT (where a CLIENT dials its paired SERVER) default to localhost and
must be fixed up afterward for the two CLIENT devices specifically —
see fix_client_receiver.py, run after this script for every device.

Requires the root venv with stls-core's [arqit_py] extra installed
(see python/stls-core/README.md) and real Arqit account credentials
in the template config referenced below.
"""
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STLS_CORE_SRC = REPO_ROOT / "python" / "stls-core" / "src"
sys.path.insert(0, str(STLS_CORE_SRC))

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s - %(message)s")

from uk.arqit.device.common import QCConfig
from uk.arqit.device.common.authentication.authentication import AuthenticationClient
from uk.arqit.device.common.authentication.authentication_session_manager import AuthenticationSessionManager
from uk.arqit.device.provisioning.provisioning import ProvisioningClient
from uk.arqit.device.registration.registration import RegistrationClient

SAMPLE_DIR = Path(__file__).resolve().parent
OUT_DIR = SAMPLE_DIR / "devices"

# Any already-registered device config in this repo with real account
# credentials filled in works as the template (REALM_*/Q_KEY are
# account-level bootstrap values, not device-specific).
TEMPLATE_PATH = Path(
    os.environ.get(
        "ARQIT_TEMPLATE_CONFIG",
        str(REPO_ROOT / "samples" / "server" / "10_server_ms" / "server_ms_1" / "aq-sf-server-ms-1.json"),
    )
).resolve()


def build_config(device_id: str, port: int) -> dict:
    with open(TEMPLATE_PATH) as f:
        cfg = json.load(f)
    cfg["DEVICE_METADATA_FILEPATH"] = str(OUT_DIR / f"aq-{device_id}_metadata.json")
    cfg["DEVICE_PROPERTIES_FILEPATH"] = str(OUT_DIR / f"aq-{device_id}_properties.json")
    cfg["DEVICE_POLICY_FILEPATH"] = str(OUT_DIR / f"aq-{device_id}_policy.json")
    cfg["SOCKET_RECEIVER_IP_ADDRESS"] = "localhost"
    cfg["SOCKET_RECEIVER_PORT"] = port
    cfg["SOCKET_LISTENING_PORT"] = port
    return cfg


def main() -> None:
    if len(sys.argv) != 3:
        print(f"usage: python {sys.argv[0]} <device_id> <port>")
        sys.exit(1)
    device_id, port = sys.argv[1], int(sys.argv[2])

    OUT_DIR.mkdir(exist_ok=True)
    cfg_path = OUT_DIR / f"aq-{device_id}.json"
    with open(cfg_path, "w") as f:
        json.dump(build_config(device_id, port), f, indent=2)

    qc_config = QCConfig(str(cfg_path))
    user_auth_token = AuthenticationClient.get_user_auth_token(qc_config)
    reg_client = RegistrationClient(qc_config)
    reg_client.register_device_v3(
        registration_mode="QKEY",
        device_id=device_id,
        app_name="Arqit Example App",
        manufacturer_id="GenericManufacturer",
        user_auth_token=user_auth_token,
    )
    meta_path = qc_config.device_metadata_filepath
    if not os.path.exists(meta_path):
        print(f"REGISTER_FAILED: {device_id} -- no metadata file written at {meta_path}")
        sys.exit(1)
    with open(meta_path) as f:
        meta = json.load(f)
    print(f"REGISTER_OK {device_id}: device_uid={meta['deviceUniqueId']} port={port}")

    provisioning_client = ProvisioningClient(qc_config)
    mgr = AuthenticationSessionManager(qc_config, reprovision_handler=provisioning_client.transition_provisioning_state)
    session = mgr.authenticate_device()
    provisioning_client.transition_provisioning_state(session)
    mgr.authenticate_device()
    with open(meta_path) as f:
        meta = json.load(f)
    print(f"PROVISION_OK {device_id}: provisioningState={meta.get('provisioningState')!r}")
    print("DONE")


if __name__ == "__main__":
    main()
