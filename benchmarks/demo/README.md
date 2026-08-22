# Compression demo benchmark

This trusted local benchmark measures zlib compression throughput, latency, and output ratio for a deterministic payload. It validates byte-for-byte decompression and emits Looper v1alpha1 metrics and result files.

It is intentionally small enough for a workstation smoke test. It is not an IaaS performance claim and should not be used to compare cloud instance families without a declared experimental design and environment matrix.
