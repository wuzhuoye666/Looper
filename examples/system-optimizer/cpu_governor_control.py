"""Uniform cpufreq governor control for the Alibaba ECS CPU component loop.

The guest exposes one cpufreq policy per vCPU (policy0..policy7). The optimizer
treats the governor as ONE logical setting applied uniformly to every policy;
any divergence is a hard failure (fail closed), never a partial success.

Subcommands:
    read                       print the governor when all policies agree
    apply --value GOVERNOR     write GOVERNOR to every policy and verify readback
"""

from __future__ import annotations

import sys
from pathlib import Path

POLICY_ROOT = Path("/sys/devices/system/cpu/cpufreq")


def _governors() -> dict[str, str]:
    policies = {}
    for policy_dir in sorted(POLICY_ROOT.glob("policy*")):
        governor_file = policy_dir / "scaling_governor"
        if governor_file.exists():
            policies[policy_dir.name] = governor_file.read_text(encoding="utf-8").strip()
    if not policies:
        print(f"no cpufreq policies under {POLICY_ROOT}", file=sys.stderr)
        raise SystemExit(2)
    return policies


def read_uniform() -> None:
    policies = _governors()
    values = set(policies.values())
    if len(values) != 1:
        print(f"mixed governors: {policies}", file=sys.stderr)
        raise SystemExit(3)
    print(next(iter(values)))


def apply_uniform(value: str) -> None:
    if not value or not value.strip() or len(value.split()) != 1:
        print("governor value must be a single non-empty token", file=sys.stderr)
        raise SystemExit(5)
    value = value.strip()
    for policy_dir in sorted(POLICY_ROOT.glob("policy*")):
        governor_file = policy_dir / "scaling_governor"
        if not governor_file.exists():
            continue
        governor_file.write_text(value + "\n", encoding="utf-8")
    policies = _governors()
    mismatched = {name: seen for name, seen in policies.items() if seen != value}
    if mismatched:
        print(f"readback mismatch after apply: {mismatched}", file=sys.stderr)
        raise SystemExit(4)
    print(value)


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        print("usage: cpu_governor_control.py read | apply --value GOVERNOR", file=sys.stderr)
        raise SystemExit(1)
    command = argv[1]
    if command == "read":
        if len(argv) != 2:
            raise SystemExit(1)
        read_uniform()
        return
    if command == "apply":
        if len(argv) != 4 or argv[2] != "--value":
            raise SystemExit(1)
        apply_uniform(argv[3])
        return
    print(f"unknown command: {command}", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv)
