"""
fix_client_receiver.py — samples/peer/18_naas_federation (issue #38)
------------------------------------------------------------------
Points a CLIENT device's own peering-socket receiver address at its
paired SERVER device's real, network-reachable NodePort (see the
README, "Running with arqit_py" — localhost only works when both
paired devices run in the SAME pod, never true for this sample's
owner-mode-in-one-pod-but-different-company CLIENT devices).

Run once per CLIENT device, AFTER register_device.py and AFTER
k8s/arqit_py/01-peering-sockets.yaml has been applied (its own NodePort
numbers are this script's <port> argument):

  python fix_client_receiver.py sf-p18-ab-rfq-client   <minikube ip> 30910
  python fix_client_receiver.py sf-p18-ab-reply-client <minikube ip> 30911
  python fix_client_receiver.py sf-p18-ac-rfq-client   <minikube ip> 30912
  python fix_client_receiver.py sf-p18-ac-reply-client <minikube ip> 30913
"""
import json
import sys
from pathlib import Path

SAMPLE_DIR = Path(__file__).resolve().parent
OUT_DIR = SAMPLE_DIR / "devices"


def main() -> None:
    if len(sys.argv) != 4:
        print(f"usage: python {sys.argv[0]} <device_id> <ip> <port>")
        sys.exit(1)
    device_id, ip, port = sys.argv[1], sys.argv[2], int(sys.argv[3])

    cfg_path = OUT_DIR / f"aq-{device_id}.json"
    with open(cfg_path) as f:
        cfg = json.load(f)
    cfg["SOCKET_RECEIVER_IP_ADDRESS"] = ip
    cfg["SOCKET_RECEIVER_PORT"] = port
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"{device_id}: receiver={ip}:{port}")


if __name__ == "__main__":
    main()
