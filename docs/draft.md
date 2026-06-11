# Kernel Optimization Draft

## Task Contract

- Objective: optimize `KernelBuilder.build_kernel` so the official frozen submission tests report fewer than 1000 cycles.
- Correctness: preserve the exact final `inp.values` behavior of `reference_kernel2` on the frozen simulator.
- Constraints: do not modify `tests/`; keep the solution inside this repository's kernel builder.
- Validation: `/Users/limh24/.platformio/python3/bin/python3.11 tests/submission_tests.py`
- Promotion criteria: the submission tests pass and the measured cycle count on the official `10,16,256` benchmark is below 1000.

## Baseline

- Current implementation is a scalar/unpacked kernel.
- Official submission tests currently report `CYCLES: 147734`.
- The frozen tests fail all performance thresholds and pass correctness.

## Main Risks

- The obvious vectorization path still leaves a large load bottleneck.
- `match` syntax requires Python 3.10+, so the verifier must use a modern interpreter.
- The submitted program must remain a valid instruction stream for the frozen simulator.

## Candidate Directions

1. Reduce the remaining deep gather load count, because the current `load` lower bound is `1065` cycles.
2. Reduce or rebalance VALU work, because the current `valu` lower bound is `1047` cycles.
3. Preserve the already-good wavefront scheduling and only change work that lowers one of the active floors.

## First Steps

1. Trace the current 1098-cycle candidate by engine and opcode counts.
2. Try depth-4 node lookup replacement for the two depth-4 rounds to cut gather loads.
3. Try moving selected simple VALU ops to scalar ALU lanes where ALU slack can absorb them.
4. Measure one candidate at a time with the official submission test.

## Evidence to Record

- `benchmark.csv`: candidate name, cycle count, notes.
- `candidates.jsonl`: candidate lineage and promote/reject decision.

## Next Target Note

- The active target is below 1000 cycles.
- The current best correct candidate is 1043 cycles.
- Current engine floors are still just above 1000 for load and VALU, so further progress requires reducing total work and shortening the selector/hash tails, not just improving scheduling.
- Current slot counts are `load=2008`, `valu=6018`, `alu=11937`, `flow=920`, `store=32`, giving lower bounds of `1004`, `1003`, `995`, `920`, and `16` cycles respectively.
- A valid instruction-stream solution below 1000 likely needs either a cheaper representation for selected tree nodes, a deeper algebraic fold in the hash/index boundary, or a larger rebalancing that removes at least one more gather vector while also reducing VALU pressure.

## 1097-Cycle Checkpoint

- Current best legal candidate: `deep_gather_xor_scalar`, measured at `1097` cycles on the official frozen submission tests.
- Current slot counts are `load=2130`, `valu=5829`, `alu=11777`, `flow=799`, `store=32`, giving lower bounds of `1065`, `972`, `982`, `799`, and `16` cycles respectively.
- Remaining load slots are dominated by `2048` `load_offset` operations from the eight deep gather rounds. Reaching `<1000` now requires removing at least `132` executed/static load slots while keeping added VALU/ALU/FLOW work within the remaining slack.
- Rejected simple load-floor candidates:
  - Moving constant generation from `load const` to `flow add_imm` reduced load slots by `35` but stretched the schedule to `1140` cycles.
  - Replacing a depth-4 gather with a straightforward broadcast/select tree would save load slots, but a `vselect` implementation adds at least `240` flow operations per depth-4 round, pushing the flow lower bound above `1000`.
  - A per-lane ALU equality selection tree for depth-4 broadcast adds at least `16*8*32 = 4096` ALU comparisons per depth-4 round before selection, which is also too expensive.
- Rejected mixed selection-tree candidate:
  - `final_d4_select20` reduced load slots to `1987`, but the streaming tree serialized through shared temporaries, produced an `1137`-instruction schedule before validation, and failed correctness. A viable selection-tree path needs more careful lifetime/temporary planning and local schedule balance, not just global slot balance.
- Public discussion points at broadcast grouping, sparse indirection, speculative preloading, and early branch-bit extraction as the real path forward. In this ISA, the next viable candidate likely needs a representation change for grouped lanes, not a local replacement of `load_offset`.

## 1043-Cycle Checkpoint

- Current best legal candidate: `final_d4_block20_pair_rebalance`, measured at `1043` cycles on the official frozen submission tests.
- The current depth4 strategy preloads nodes 22..37 and replaces selected early/final depth4 gathers:
  - final selected blocks: `{2,4,5,6,8,9,10,12,17,18,19,20,27,29}`
  - early selected blocks: `{7,20,29}`
  - `d4_flow_pairs={0,3,4}` and scalar bit extraction for `{19,20,29}`.
- Remaining bottlenecks:
  - `load_offset=1912`, total `load=2008`, load lower bound `1004`.
  - `valu=6018`, VALU lower bound `1003`.
  - schedule has a front non-load bubble around cycles 39-57 and final non-load tail around cycles 1033-1042.
- Rejected since 1043:
  - extra depth4 block search: 651 build-only block-set variants found no cycle below `1043`.
  - second selector temp bank: increased schedule length.
  - moving depth4 `22/23` update back to vselect: `1078`.
  - early deep parity before final `^ c5`: `1048`.
  - root-round `c5` pre-xor with parity inversion: correct but `1085`.
