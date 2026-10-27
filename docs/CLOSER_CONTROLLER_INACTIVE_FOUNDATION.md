# Inactive correction-controller foundation

This slice adds the previously reviewed correction-controller module as an inactive library. It does not register a consumer, provision custody, install a source factory, select a model, or change the existing queue, dispatcher, native callbacks or runtime configuration. Existing main files are preserved.

Its portable tests cover canonical JSON byte/rejection equivalence against the original oracle, private wire goldens, and the default unsupported native account/confinement/budget readers. The original oracle and these tests come from the reviewed source. Historical copied evidence and whole-parent AST checks remain local review evidence; they are not portable repository fixtures and are not treated as fresh controller execution.

The integrated enrollment/native-effect/controller path remains a separate source projection. It requires the source installation and shared queue/writer/native callback dependencies, plus review of their current-main overlaps. This module alone does not prove that path works and does not supply missing installed facts or authentication. Its public data objects, hashes and canonical bytes are not authority.

No runtime installation, mirror synchronization or activation occurs in this slice. Loss of custody and unsupported readers continue to hold; no historical UNKNOWN is recovered or replayed. Future source composition must retain newer current-main behavior and rerun the affected compatibility checks rather than replace shared files with the old snapshot.
