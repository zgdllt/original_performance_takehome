# Execution Plan

1. Reconfirm the current promoted candidate with `/Users/limh24/.platformio/python3/bin/python3.11 tests/submission_tests.py`.
2. Generate a slot/opcode profile from `KernelBuilder.build_kernel(10, 2047, 256, 16)`.
3. Implement one work-reduction candidate at a time in `perf_takehome.py`.
4. Measure each candidate with the official frozen submission tests.
5. Keep only candidates that preserve correctness and reduce the measured cycle count.
6. Record each kept or rejected candidate in `benchmark.csv` and `candidates.jsonl`.
7. Promote only when the official benchmark is below 1000 cycles.

## Current Checkpoint

- Best official result: `1043` cycles.
- Current slot profile: `load=2008`, `flow=920`, `valu=6018`, `alu=11937`, `store=32`.
- Active lower bounds: `load=1004`, `valu=1003`, `alu=995`.
- Next candidates should either lower both load and VALU pressure or shorten the front/final non-load tails; extra depth4 selection alone has not improved beyond `1043`.
