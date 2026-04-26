"""Micro-benchmarks for performance-critical features.

These are regular pytest tests that measure wall-clock throughput and
print comparison tables. They run as part of the normal suite (fast
enough — each samples a few hundred iterations) and assert on
*minimum* speedups that have significant safety margin relative to
what we observe on the baseline hardware.

Each benchmark test:

1. Runs the legacy / disabled path first and records its timing.
2. Runs the optimised path second and records its timing.
3. Asserts on a conservative speedup ratio (e.g. \u22651.5\u00d7 where we
   observe 3\u20136\u00d7) so the test doesn't become flaky on slow CI.
4. Emits a pytest ``print`` block with the measured numbers so
   ``pytest -s`` gives an operator a quick eyeballable report.
"""
