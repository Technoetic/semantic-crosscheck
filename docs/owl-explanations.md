# Bounded OWL inconsistency explanations

Run `reason --explain-inconsistency` to attach an optional `owl:explanation` check after an actual
`owl:consistency` failure. The original failure and the skipped V5 comparisons remain. Consistent ontologies,
excluded OWL checks, source-resource limits and failed baseline reasoner calls do not invoke the diagnostic.
An explicit request requires a configured ontology and an `--only` selection containing `owl`.

```powershell
python skills/semantic-crosscheck/scripts/sxc.py reason --config demo/crosscheck.toml --out <existing-build-output> --explain-inconsistency --explain-max-axioms 128 --explain-max-checks 64 --explain-timeout-ms 10000
```

The defaults are 128 removable groups, 64 consistency calls and 10,000 milliseconds for the complete explanation.
All limits must be positive integers. Hard ceilings are 4,096 groups, 4,096 calls and 300,000 milliseconds.
Large source snapshots can exceed the group limit even when the normal OWL individual limit permits them.
Before sorting source rows or allocating group descriptors, a constant-time lower bound from container lengths
rejects oversized graphs and configurations. Remaining class-assertion counting stops at the cap or deadline.
`axiom_groups_count_complete: true` identifies an exact group count; when false, `axiom_groups` is only a lower
bound and no complete count was attempted. Descriptor collection is also guarded by the cap and deadline.
The elapsed deadline includes worker startup and reasoner execution; process termination and temporary-directory
cleanup follow on interruption. Starting HermiT afresh for every subset has a real cost, so the default deadline
may produce a sufficient partial core on even a small snapshot. Increase it explicitly when appropriate.

The algorithm first checks the complete generated group set in HermiT. It then tries deleting groups in a stable
order, retaining each deletion only when HermiT still reports inconsistency. A final pass checks deletion of every
retained group from the exact final core. Only successful completion of all those checks produces
`status: pass`, `sufficient: true` and `minimality: subset_minimal`. This pass describes the diagnostic work;
the source ontology still fails `owl:consistency`.

A `subset_minimal` result then decides `uniqueness`. A group belongs to every subset-minimal explanation exactly
when removing only that group from the complete generated set makes HermiT report consistency, and the reported
core is the only explanation exactly when this holds for each retained group ([Union and Intersection of all
Justifications](https://arxiv.org/abs/2109.11216), ESWC 2022, applied here to generated axiom groups over fixed
declarations). One further check per retained group therefore gives `uniqueness: unique` when every removal is
consistent and `uniqueness: not_unique` at the first removal that stays inconsistent, because another explanation
without that group exists; the others are not enumerated. Each of these checks covers the complete group set, so it
costs about as much as the initial check. They share the call cap and deadline: a cap, deadline or worker failure
leaves `uniqueness: not_proven` with `uniqueness_reason` `check_limit`, `timeout` or `reasoner_error`, and keeps the
verified `status: pass` and `minimality: subset_minimal`; finished checks give `uniqueness_reason: completed`.
`uniqueness_checks_completed` counts finished uniqueness checks, which `checks_completed` also includes. An
incomplete explanation never attempts uniqueness (`uniqueness_reason: minimality_not_proven`). With the default 64
calls, a three-group core finishes the explanation and these checks only when the snapshot has at most 57 removable
groups (1 + 57 + 3 + 3 calls), and the default deadline often ends the search sooner; raise the limits for real
data. The criterion assumes that every group generates a distinct axiom. Configurations reject a repeated
`ontology.no_children` value. A repeated `ontology.no_children` value or a repeated edge that still reaches the
diagnostic, such as a caller-supplied graph that repeats an edge (database snapshots read distinct edges), leaves
`uniqueness: not_proven` with `duplicate_axiom_groups` and no extra call: the surviving copy would otherwise imitate
a second explanation. Only these two repeats are detected; a caller that bypasses the configuration checks and maps
two entries to one OWL name can get a group-level `not_unique`, never a false `unique`.

A group cap, call cap, timeout or worker failure produces `status: not_run` and `minimality: not_proven`.
`sufficient: true` then means the reported partial core was actually checked and found inconsistent. If the
initial complete-set check did not finish or returned consistent, no sufficient core is reported. The original
source failure is preserved. Limits and failures never imply that a candidate core is minimal.

Groups include each configured subclass relation, the configured class disjointness block, transitivity,
subproperty/domain/range axioms, each `no_children` restriction, each defined-class equivalence, each node type and
each edge assertion. The disjointness block and an equivalence expression are indivisible groups. Class/property/
individual declarations remain fixed background. Identifiers such as `a000003` are local ordinals within the
snapshot, ordered independently of source row order. They are not hashes of private keys and are not intended as
cross-snapshot row identifiers. Each core descriptor has only an opaque id, a group kind and a configuration
reference; class-map references point to `ontology.classes` without embedding class-column values.

Every subset uses the existing owlready2/HermiT implementation in a private local Python subprocess. Worker input
replaces keys and class-column values with ordinals while preserving the generated ontology's logical structure.
Key ordinals follow the same canonical order across both node-table keys and outside edge endpoints. The worker
retains the parent's sorted assertion positions explicitly, including duplicate edges and numeric aliases, so
an active group always refers to the corresponding original assertion. Only node-table rows become node-type
groups; assigning ordinals to outside endpoints does not create extra node-table rows.
Before invoking HermiT, the worker recomputes and compares the parent's full group manifest, including opaque
assertion terms, class-position tokens and configuration references. A correspondence mismatch aborts the check.
No DSN, SQL or source configuration path is serialized. Workers inherit only basic OS/locale environment variables,
with their temporary directories redirected into the private workspace. Java stderr is discarded, and the only accepted worker
responses are fixed consistency tokens. Temporary HermiT files are removed afterwards. On Windows a kill-on-close
Job Object contains the worker and its descendants before the input payload is sent; containment failure aborts
the check. POSIX workers run in their own process group. Timeouts terminate the whole group/job.

This is the same reasoner as the baseline check. Subset minimality and uniqueness concern the removable groups
and fixed background only: `unique` means that no other set of these generated groups is a subset-minimal
inconsistent set for this HermiT. It is not independent certification, a globally minimum explanation, a
statement about axioms outside the generated groups or an enumeration of every cause. No additional Java
dependency or external implementation is bundled.

The source-independent synthetic runner exercises actual HermiT without a database. Set `SXC_JAVA` when Java is
not on `PATH`, and run either case with enough time for repeated Java startup:

```powershell
python examples/owl-explanations/run.py --case no_children --timeout-ms 60000
python examples/owl-explanations/run.py --case disjoint --timeout-ms 60000
```

Both fixtures intentionally fail consistency. The runner exits successfully only when it obtains a verified
subset-minimal explanation and prints the bounded, key-free diagnostic JSON. Tests also directly recheck the
returned core and every retained deletion with actual HermiT. Each fixture has a single explanation, so a run
that finishes reports `uniqueness: unique`; tests confirm with direct HermiT calls that removing each retained
group from the complete set restores consistency, and that two independent violations give `not_unique`.
