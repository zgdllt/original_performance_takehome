# Execution Plan

1. Establish a baseline cycle model from the current scalar kernel.
2. Implement a first vector candidate with algebraic hash simplifications.
3. Measure on `tests/submission_tests.py`.
4. Only keep changes that improve the official cycle count without breaking correctness.
5. Iterate until the official benchmark is below 1200 cycles.
6. Continue the promoted 1191-cycle candidate toward the next target, below 1000 cycles, by reducing actual engine work rather than only changing schedule order.
