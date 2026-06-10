"""
# Anthropic's Original Performance Engineering Take-home (Release version)

Copyright Anthropic PBC 2026. Permission is granted to modify and use, but not
to publish or redistribute your solutions so it's hard to find spoilers.

# Task

- Optimize the kernel (in KernelBuilder.build_kernel) as much as possible in the
  available time, as measured by test_kernel_cycles on a frozen separate copy
  of the simulator.

Validate your results using `python tests/submission_tests.py` without modifying
anything in the tests/ folder.

We recommend you look through problem.py next.
"""

from collections import defaultdict
import random
import unittest

from problem import (
    Engine,
    DebugInfo,
    SLOT_LIMITS,
    VLEN,
    N_CORES,
    SCRATCH_SIZE,
    Machine,
    Tree,
    Input,
    HASH_STAGES,
    reference_kernel,
    build_mem_image,
    reference_kernel2,
)


class KernelBuilder:
    def __init__(self):
        self.instrs = []
        self.scratch = {}
        self.scratch_debug = {}
        self.scratch_ptr = 0
        self.const_map = {}

    def debug_info(self):
        return DebugInfo(scratch_map=self.scratch_debug)

    def build(self, slots: list[tuple[Engine, tuple]], vliw: bool = False):
        # Simple slot packing that just uses one slot per instruction bundle
        instrs = []
        for engine, slot in slots:
            instrs.append({engine: [slot]})
        return instrs

    def add(self, engine, slot):
        self.instrs.append({engine: [slot]})

    def alloc_scratch(self, name=None, length=1):
        addr = self.scratch_ptr
        if name is not None:
            self.scratch[name] = addr
            self.scratch_debug[addr] = (name, length)
        self.scratch_ptr += length
        assert self.scratch_ptr <= SCRATCH_SIZE, "Out of scratch space"
        return addr

    def scratch_const(self, val, name=None):
        if val not in self.const_map:
            addr = self.alloc_scratch(name)
            self.add("load", ("const", addr, val))
            self.const_map[val] = addr
        return self.const_map[val]

    def build_hash(self, val_hash_addr, tmp1, tmp2, round, i):
        slots = []

        for hi, (op1, val1, op2, op3, val3) in enumerate(HASH_STAGES):
            slots.append(("alu", (op1, tmp1, val_hash_addr, self.scratch_const(val1))))
            slots.append(("alu", (op3, tmp2, val_hash_addr, self.scratch_const(val3))))
            slots.append(("alu", (op2, val_hash_addr, tmp1, tmp2)))
            slots.append(("debug", ("compare", val_hash_addr, (round, i, "hash_stage", hi))))

        return slots

    def build_scheduled(self, tasks):
        dependents = [[] for _ in tasks]
        dep_count = [0] * len(tasks)
        for i, task in enumerate(tasks):
            dep_count[i] = len(task["deps"])
            for dep in task["deps"]:
                dependents[dep].append(i)

        priority = [0] * len(tasks)
        for i in range(len(tasks) - 1, -1, -1):
            priority[i] = 1 + max((priority[j] for j in dependents[i]), default=0)

        ready = [i for i, count in enumerate(dep_count) if count == 0]
        ready_set = set(ready)
        scheduled = [False] * len(tasks)
        instrs = []
        remaining = len(tasks)

        engine_order = ("load", "valu", "alu", "store", "flow")
        while remaining:
            bundle = {}
            selected = []
            for engine in engine_order:
                limit = SLOT_LIMITS[engine]
                candidates = [
                    i for i in ready if not scheduled[i] and tasks[i]["engine"] == engine
                ]
                candidates.sort(key=lambda i: (-priority[i], i))
                for i in candidates[:limit]:
                    selected.append(i)
                    bundle.setdefault(engine, []).append(tasks[i]["slot"])
                    scheduled[i] = True
                    ready_set.remove(i)

            if not selected:
                raise RuntimeError("Scheduler made no progress")

            instrs.append(bundle)
            remaining -= len(selected)

            for i in selected:
                for dep in dependents[i]:
                    dep_count[dep] -= 1
                    if dep_count[dep] == 0 and dep not in ready_set:
                        ready.append(dep)
                        ready_set.add(dep)

            if len(ready) > 4096:
                ready = [i for i in ready if not scheduled[i]]
                ready_set = set(ready)

        return instrs

    def build_kernel(
        self, forest_height: int, n_nodes: int, batch_size: int, rounds: int
    ):
        """
        Fully unrolled SIMD kernel for the submission shape.

        Values stay in scratch across all rounds. Tree positions are represented
        as absolute memory addresses so a lane can gather with load_offset
        without recomputing forest_values_p + idx every round.
        """
        assert batch_size % VLEN == 0

        tasks = []
        last_writer = {}
        last_readers = defaultdict(set)

        def add_task(engine, slot, reads=(), writes=()):
            reads = tuple(reads)
            writes = tuple(writes)
            deps = set()
            for addr in reads:
                if addr in last_writer:
                    deps.add(last_writer[addr])
            for addr in writes:
                if addr in last_writer:
                    deps.add(last_writer[addr])
                deps.update(last_readers[addr])

            task_id = len(tasks)
            tasks.append({"engine": engine, "slot": slot, "deps": deps})

            write_set = set(writes)
            for addr in reads:
                if addr not in write_set:
                    last_readers[addr].add(task_id)
            for addr in writes:
                last_readers[addr].clear()
                last_writer[addr] = task_id
            return task_id

        def vec_op(op, dest, a, b):
            add_task(
                "valu",
                (op, dest, a, b),
                reads=tuple(range(a, a + VLEN)) + tuple(range(b, b + VLEN)),
                writes=range(dest, dest + VLEN),
            )

        def vec_madd(dest, a, b, c):
            add_task(
                "valu",
                ("multiply_add", dest, a, b, c),
                reads=(
                    tuple(range(a, a + VLEN))
                    + tuple(range(b, b + VLEN))
                    + tuple(range(c, c + VLEN))
                ),
                writes=range(dest, dest + VLEN),
            )

        def vec_select(dest, cond, a, b):
            add_task(
                "flow",
                ("vselect", dest, cond, a, b),
                reads=(
                    tuple(range(cond, cond + VLEN))
                    + tuple(range(a, a + VLEN))
                    + tuple(range(b, b + VLEN))
                ),
                writes=range(dest, dest + VLEN),
            )

        def alloc_vec(name):
            return self.alloc_scratch(name, VLEN)

        def load_scalar_const(addr, val):
            add_task("load", ("const", addr, val), writes=(addr,))

        def init_vec_const(name, val):
            scalar = self.alloc_scratch(name + "_scalar")
            vec = alloc_vec(name)
            load_scalar_const(scalar, val)
            add_task(
                "valu",
                ("vbroadcast", vec, scalar),
                reads=(scalar,),
                writes=range(vec, vec + VLEN),
            )
            return vec

        def alu_lanes(op, dest, a, b):
            for lane in range(VLEN):
                add_task(
                    "alu",
                    (op, dest + lane, a + lane, b + lane),
                    reads=(a + lane, b + lane),
                    writes=(dest + lane,),
                )

        def alu_lanes_scalar(op, dest, a, b_scalar):
            for lane in range(VLEN):
                add_task(
                    "alu",
                    (op, dest + lane, a + lane, b_scalar),
                    reads=(a + lane, b_scalar),
                    writes=(dest + lane,),
                )

        def scalar_parity(dest, val):
            for lane in range(VLEN):
                add_task(
                    "alu",
                    ("&", dest + lane, val + lane, one_s),
                    reads=(val + lane, one_s),
                    writes=(dest + lane,),
                )

        scratch_addr = self.alloc_scratch("scratch_addr")
        scratch_scalar = self.alloc_scratch("scratch_scalar")

        def load_tree_node_vec(name, abs_addr):
            vec = alloc_vec(name)
            load_scalar_const(scratch_addr, abs_addr)
            add_task(
                "load",
                ("load", scratch_scalar, scratch_addr),
                reads=(scratch_addr,),
                writes=(scratch_scalar,),
            )
            add_task(
                "valu",
                ("vbroadcast", vec, scratch_scalar),
                reads=(scratch_scalar,),
                writes=range(vec, vec + VLEN),
            )
            return vec

        one_v = init_vec_const("one_v", 1)
        two_v = init_vec_const("two_v", 2)
        m4097_v = init_vec_const("m4097_v", 4097)
        m33_v = init_vec_const("m33_v", 33)
        m9_v = init_vec_const("m9_v", 9)
        c0_v = init_vec_const("c0_v", 0x7ED55D16)
        c2_v = init_vec_const("c2_v", 0x165667B1)
        c4_v = init_vec_const("c4_v", 0xFD7046C5)
        sh9_v = init_vec_const("sh9_v", 9)
        sh16_v = init_vec_const("sh16_v", 16)
        sh19_v = init_vec_const("sh19_v", 19)
        depth4_base_v = init_vec_const("depth4_base_v", 22)
        add_even_v = init_vec_const("add_even_v", -6)
        add_odd_v = init_vec_const("add_odd_v", -5)

        one_s = self.alloc_scratch("one_s")
        c1_s = self.alloc_scratch("c1_s")
        c3_s = self.alloc_scratch("c3_s")
        c5_s = self.alloc_scratch("c5_s")
        load_scalar_const(one_s, 1)
        load_scalar_const(c1_s, 0xC761C23C)
        load_scalar_const(c3_s, 0xD3A2646C)
        load_scalar_const(c5_s, 0xB55A4F09)

        root_node_v = load_tree_node_vec("root_node_v", 7)

        d1_n0 = load_tree_node_vec("d1_n0", 8)
        d1_n1 = load_tree_node_vec("d1_n1", 9)

        d2_n0 = load_tree_node_vec("d2_n0", 10)
        d2_n1 = load_tree_node_vec("d2_n1", 11)
        d2_diff0 = load_tree_node_vec("d2_diff0", 12)
        d2_diff1 = load_tree_node_vec("d2_diff1", 13)
        vec_op("-", d2_diff0, d2_diff0, d2_n0)
        vec_op("-", d2_diff1, d2_diff1, d2_n1)

        d3_n0 = load_tree_node_vec("d3_n0", 14)
        d3_n1 = load_tree_node_vec("d3_n1", 15)
        d3_diff_lo0 = load_tree_node_vec("d3_diff_lo0", 16)
        d3_diff_lo1 = load_tree_node_vec("d3_diff_lo1", 17)
        d3_n4 = load_tree_node_vec("d3_n4", 18)
        d3_n5 = load_tree_node_vec("d3_n5", 19)
        d3_diff_hi0 = load_tree_node_vec("d3_diff_hi0", 20)
        d3_diff_hi1 = load_tree_node_vec("d3_diff_hi1", 21)
        vec_op("-", d3_diff_lo0, d3_diff_lo0, d3_n0)
        vec_op("-", d3_diff_lo1, d3_diff_lo1, d3_n1)
        vec_op("-", d3_diff_hi0, d3_diff_hi0, d3_n4)
        vec_op("-", d3_diff_hi1, d3_diff_hi1, d3_n5)

        bit0 = alloc_vec("bit0")
        bit1 = alloc_vec("bit1")
        bit2 = alloc_vec("bit2")
        mix = alloc_vec("mix")
        pair = alloc_vec("pair")

        n_vecs = batch_size // VLEN
        inp_values_p = 7 + n_nodes + batch_size

        vals = []
        idxs = []
        tmp0s = []
        tmp1s = []
        store_addrs = []
        for block in range(n_vecs):
            val_v = alloc_vec(f"val_{block}")
            idx_v = alloc_vec(f"idx_{block}")
            tmp0_v = alloc_vec(f"tmp0_{block}")
            tmp1_v = alloc_vec(f"tmp1_{block}")
            base = self.alloc_scratch(f"value_base_{block}")
            load_scalar_const(base, inp_values_p + block * VLEN)
            add_task(
                "load",
                ("vload", val_v, base),
                reads=(base,),
                writes=range(val_v, val_v + VLEN),
            )
            vals.append(val_v)
            idxs.append(idx_v)
            tmp0s.append(tmp0_v)
            tmp1s.append(tmp1_v)
            store_addrs.append(base)

        tile_ids = list(range(n_vecs))
        n_groups = 16
        stagger = 2
        group_ids = [
            tile_ids[g * n_vecs // n_groups : (g + 1) * n_vecs // n_groups]
            for g in range(n_groups)
        ]

        def emit_hash(ids):
            for block in ids:
                vec_madd(vals[block], vals[block], m4097_v, c0_v)

            for block in ids:
                vec_op(">>", tmp0s[block], vals[block], sh19_v)
                alu_lanes_scalar("^", vals[block], vals[block], c1_s)
            for block in ids:
                vec_op("^", vals[block], vals[block], tmp0s[block])

            for block in ids:
                vec_madd(vals[block], vals[block], m33_v, c2_v)

            for block in ids:
                vec_op("<<", tmp0s[block], vals[block], sh9_v)
                alu_lanes_scalar("+", vals[block], vals[block], c3_s)
            for block in ids:
                vec_op("^", vals[block], vals[block], tmp0s[block])

            for block in ids:
                vec_madd(vals[block], vals[block], m9_v, c4_v)

            for block in ids:
                vec_op(">>", tmp0s[block], vals[block], sh16_v)
                alu_lanes_scalar("^", vals[block], vals[block], c5_s)
            for block in ids:
                vec_op("^", vals[block], vals[block], tmp0s[block])

        def emit_parity(dest, val, use_scalar=False):
            if use_scalar:
                scalar_parity(dest, val)
            else:
                vec_op("&", dest, val, one_v)

        def round_root(ids, use_scalar_and):
            for block in ids:
                vec_op("^", vals[block], vals[block], root_node_v)
            emit_hash(ids)
            for block in ids:
                emit_parity(idxs[block], vals[block], use_scalar_and)

        def round_depth1(ids):
            for block in ids:
                vec_select(tmp0s[block], idxs[block], d1_n1, d1_n0)
                vec_op("^", vals[block], vals[block], tmp0s[block])
            emit_hash(ids)
            for block in ids:
                emit_parity(tmp0s[block], vals[block])
                vec_madd(idxs[block], idxs[block], two_v, tmp0s[block])

        def round_depth2(ids):
            for block in ids:
                vec_op("&", bit0, idxs[block], one_v)
                vec_op(">>", bit1, idxs[block], one_v)
                vec_select(tmp0s[block], bit0, d2_n1, d2_n0)
                vec_select(mix, bit0, d2_diff1, d2_diff0)
                vec_madd(tmp0s[block], bit1, mix, tmp0s[block])
                vec_op("^", vals[block], vals[block], tmp0s[block])
            emit_hash(ids)
            for block in ids:
                emit_parity(tmp0s[block], vals[block])
                vec_madd(idxs[block], idxs[block], two_v, tmp0s[block])

        def round_depth3(ids):
            for block in ids:
                vec_op("&", bit0, idxs[block], one_v)
                vec_op(">>", bit1, idxs[block], one_v)
                vec_op("&", bit2, bit1, one_v)
                vec_op(">>", bit1, idxs[block], two_v)
                vec_select(tmp0s[block], bit0, d3_n1, d3_n0)
                vec_select(mix, bit0, d3_diff_lo1, d3_diff_lo0)
                vec_madd(tmp0s[block], bit2, mix, tmp0s[block])
                vec_select(pair, bit0, d3_n5, d3_n4)
                vec_select(mix, bit0, d3_diff_hi1, d3_diff_hi0)
                vec_madd(pair, bit2, mix, pair)
                vec_select(tmp0s[block], bit1, pair, tmp0s[block])
                alu_lanes("^", vals[block], vals[block], tmp0s[block])
            emit_hash(ids)
            for block in ids:
                emit_parity(tmp0s[block], vals[block])
                vec_madd(idxs[block], idxs[block], two_v, tmp0s[block])
                vec_op("+", idxs[block], idxs[block], depth4_base_v)

        def round_gather(ids, update_idx):
            for block in ids:
                for lane in range(VLEN):
                    add_task(
                        "load",
                        ("load_offset", tmp0s[block], idxs[block], lane),
                        reads=(idxs[block] + lane,),
                        writes=(tmp0s[block] + lane,),
                    )
                vec_op("^", vals[block], vals[block], tmp0s[block])
            emit_hash(ids)
            if update_idx:
                for block in ids:
                    emit_parity(tmp0s[block], vals[block])
                    vec_select(tmp1s[block], tmp0s[block], add_odd_v, add_even_v)
                    vec_madd(idxs[block], idxs[block], two_v, tmp1s[block])

        def emit_round(ids, round_i):
            depth = round_i if round_i <= forest_height else round_i - (forest_height + 1)
            if depth == 0:
                round_root(ids, use_scalar_and=(round_i == 0))
            elif depth == 1:
                round_depth1(ids)
            elif depth == 2:
                round_depth2(ids)
            elif depth == 3:
                round_depth3(ids)
            else:
                round_gather(
                    ids,
                    update_idx=(round_i != forest_height and round_i != rounds - 1),
                )

        for schedule_round in range(rounds + stagger * (n_groups - 1)):
            for group, ids in enumerate(group_ids):
                round_i = schedule_round - group * stagger
                if 0 <= round_i < rounds:
                    emit_round(ids, round_i)

        for block in tile_ids:
            add_task(
                "store",
                ("vstore", store_addrs[block], vals[block]),
                reads=(store_addrs[block],) + tuple(range(vals[block], vals[block] + VLEN)),
            )

        self.instrs.extend(self.build_scheduled(tasks))

BASELINE = 147734

def do_kernel_test(
    forest_height: int,
    rounds: int,
    batch_size: int,
    seed: int = 123,
    trace: bool = False,
    prints: bool = False,
):
    print(f"{forest_height=}, {rounds=}, {batch_size=}")
    random.seed(seed)
    forest = Tree.generate(forest_height)
    inp = Input.generate(forest, batch_size, rounds)
    mem = build_mem_image(forest, inp)

    kb = KernelBuilder()
    kb.build_kernel(forest.height, len(forest.values), len(inp.indices), rounds)
    # print(kb.instrs)

    value_trace = {}
    machine = Machine(
        mem,
        kb.instrs,
        kb.debug_info(),
        n_cores=N_CORES,
        value_trace=value_trace,
        trace=trace,
    )
    machine.prints = prints
    for i, ref_mem in enumerate(reference_kernel2(mem, value_trace)):
        machine.run()
        inp_values_p = ref_mem[6]
        if prints:
            print(machine.mem[inp_values_p : inp_values_p + len(inp.values)])
            print(ref_mem[inp_values_p : inp_values_p + len(inp.values)])
        assert (
            machine.mem[inp_values_p : inp_values_p + len(inp.values)]
            == ref_mem[inp_values_p : inp_values_p + len(inp.values)]
        ), f"Incorrect result on round {i}"
        inp_indices_p = ref_mem[5]
        if prints:
            print(machine.mem[inp_indices_p : inp_indices_p + len(inp.indices)])
            print(ref_mem[inp_indices_p : inp_indices_p + len(inp.indices)])
        # Updating these in memory isn't required, but you can enable this check for debugging
        # assert machine.mem[inp_indices_p:inp_indices_p+len(inp.indices)] == ref_mem[inp_indices_p:inp_indices_p+len(inp.indices)]

    print("CYCLES: ", machine.cycle)
    print("Speedup over baseline: ", BASELINE / machine.cycle)
    return machine.cycle


class Tests(unittest.TestCase):
    def test_ref_kernels(self):
        """
        Test the reference kernels against each other
        """
        random.seed(123)
        for i in range(10):
            f = Tree.generate(4)
            inp = Input.generate(f, 10, 6)
            mem = build_mem_image(f, inp)
            reference_kernel(f, inp)
            for _ in reference_kernel2(mem, {}):
                pass
            assert inp.indices == mem[mem[5] : mem[5] + len(inp.indices)]
            assert inp.values == mem[mem[6] : mem[6] + len(inp.values)]

    def test_kernel_trace(self):
        # Full-scale example for performance testing
        do_kernel_test(10, 16, 256, trace=True, prints=False)

    # Passing this test is not required for submission, see submission_tests.py for the actual correctness test
    # You can uncomment this if you think it might help you debug
    # def test_kernel_correctness(self):
    #     for batch in range(1, 3):
    #         for forest_height in range(3):
    #             do_kernel_test(
    #                 forest_height + 2, forest_height + 4, batch * 16 * VLEN * N_CORES
    #             )

    def test_kernel_cycles(self):
        do_kernel_test(10, 16, 256)


# To run all the tests:
#    python perf_takehome.py
# To run a specific test:
#    python perf_takehome.py Tests.test_kernel_cycles
# To view a hot-reloading trace of all the instructions:  **Recommended debug loop**
# NOTE: The trace hot-reloading only works in Chrome. In the worst case if things aren't working, drag trace.json onto https://ui.perfetto.dev/
#    python perf_takehome.py Tests.test_kernel_trace
# Then run `python watch_trace.py` in another tab, it'll open a browser tab, then click "Open Perfetto"
# You can then keep that open and re-run the test to see a new trace.

# To run the proper checks to see which thresholds you pass:
#    python tests/submission_tests.py

if __name__ == "__main__":
    unittest.main()
