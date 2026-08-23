"""Session helper: run one cloud command, log ledger entry + stdout, print tail."""
import datetime
import hashlib
import json
import sys
from pathlib import Path

import paramiko

HOST = "8.134.104.213"
BASE = "/opt/looper-system-opt-3b01722"
ROOT = Path(".artifacts/system-opt/m2r-memory-thp-static-20260823")
LOGS = ROOT / "logs"
PY = f"PYTHONPATH={BASE}/packages/core:{BASE}/services/api python3 -m looper_api.cli"


def main() -> None:
    cid, cmd = sys.argv[1], sys.argv[2]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    _KEY = paramiko.RSAKey.from_private_key_file(r"E:/wujiahao/CProjectAllStudies/TencentMiniProject/Looper.pem")
    client.connect(HOST, username="root", pkey=_KEY, timeout=15, allow_agent=False, look_for_keys=False)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=600)
    out = stdout.read().decode()
    err = stderr.read().decode()
    code = stdout.channel.recv_exit_status()
    client.close()
    (LOGS / f"{cid}.stdout").write_text(out, encoding="utf-8", newline="\n")
    (LOGS / f"{cid}.stderr").write_text(err, encoding="utf-8", newline="\n")
    entry = {
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
        "host": HOST,
        "cmd": cmd,
        "exit_code": code,
        "stdout_sha256": hashlib.sha256(out.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(err.encode()).hexdigest(),
        "stdout_bytes": len(out.encode()),
    }
    (LOGS / f"{cid}.json").write_text(
        json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
    )
    tail = "\n".join(out.strip().splitlines()[-25:])
    print(f"[{cid}] exit={code} stdout={len(out)}B")
    print(tail)
    if err.strip():
        print("STDERR:", err.strip()[-500:])


if __name__ == "__main__":
    main()
