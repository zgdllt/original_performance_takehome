# Kernel Optimization Draft

## Task Contract

- Objective: optimize `KernelBuilder.build_kernel` so the official frozen submission tests report fewer than 1200 cycles.
- Correctness: preserve the exact final `inp.values` behavior of `reference_kernel2` on the frozen simulator.
- Constraints: do not modify `tests/`; keep the solution inside this repository's kernel builder.
- Validation: `/Users/limh24/.platformio/python3/bin/python3.11 tests/submission_tests.py`
- Promotion criteria: the submission tests pass and the measured cycle count on the official `10,16,256` benchmark is below 1200.

## Baseline

- Current implementation is a scalar/unpacked kernel.
- Official submission tests currently report `CYCLES: 147734`.
- The frozen tests fail all performance thresholds and pass correctness.

## Main Risks

- The obvious vectorization path still leaves a large load bottleneck.
- `match` syntax requires Python 3.10+, so the verifier must use a modern interpreter.
- The submitted program must remain a valid instruction stream for the frozen simulator.

## Candidate Directions

1. Vectorize per-batch work and reduce hash cost with `multiply_add` where the stage algebra allows it.
2. Rework memory access so repeated node loads are shared across lanes and rounds where possible.
3. Fuse load/compute/update scheduling to keep the `load`, `valu`, and `alu` engines busy together.

## First Steps

1. Build a cycle model for a naive vectorized kernel.
2. Implement the cheapest hash-stage algebra first.
3. Measure one candidate at a time with the official submission test.

## Evidence to Record

- `benchmark.csv`: candidate name, cycle count, notes.
- `candidates.jsonl`: candidate lineage and promote/reject decision.

## Next Target Note

- The next target is below 1000 cycles.
- The current best correct candidate is 1102 cycles.
- Current engine floors are still above 1000 for load, VALU, and ALU, so further progress requires reducing total work, not just improving scheduling.
- Current slot counts are `load=2130`, `valu=6276`, `alu=12544`, `flow=799`, `store=32`, giving lower bounds of `1065`, `1046`, `1046`, `799`, and `16` cycles respectively.
- A valid instruction-stream solution below 1000 needs a qualitatively different work-reduction idea: fewer deep gathers, fewer hash-equivalent operations, or a different way to compute/route paths. Pure scheduling and local engine reassignment are exhausted among the variants tested so far.
