# Inactive correction-index data foundation

This slice contains self-contained private-index data primitives and the unsupported native-reader interface. It adds no consumer, source factory, queue or dispatcher branch, runtime configuration, or installed behavior.

The retained functions cover canonical JSON bytes, digests, exact schemas and selectors, index validation and pure transitions, and construction of write-intent data. `ControllerConfig` is a frozen data container. A write-intent object or selector is not authenticated custody and cannot execute a write. `NativeConsumerReader` retains the default unsupported account, confinement, execution-limit and budget reads; it supplies no installed-positive implementation or probe command.

The portable tests retain the reviewed original canonical oracle and its expected structural hash. Empty AST fields are explicitly included on Python versions that omit them by default, preserving the same structural comparison across Python 3.12, 3.13 and 3.14. Broad byte/rejection equivalence, wire goldens and default-reader refusals remain unchanged. Historical copied evidence and whole-parent AST checks remain local review evidence rather than repository fixtures.

Operational `NativeIndexStore`, custody, `IndexController`, provider guard and inspection paths are outside this foundation. Their full reviewed source is preserved in the separate controller/native projection and must land with its genuine source-installation, native-effect, maintenance and coding-session dependencies. No missing-module suppression or successful implementation stub replaces those dependencies.

There is no enrollment, custody provision, native CAS, provider launch, source qualification, installation, mirror synchronization or activation in this slice. No existing production caller imports it. Publishing these data primitives does not recover or replay historical UNKNOWN outcomes, and is not completion of the integrated controller/native path.
