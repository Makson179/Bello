"""Controller services, each with one state owner and a bounded coordinator port.

The public compatibility surface remains ``supervisor.controller``. These modules
implement services, not base classes: the controller selects and connects them.

Ownership map:
* commands: captured command output, validation/inspection ledgers and outcomes;
* evidence: observed changed paths, private-input filtering and bounded packets;
* children: descendant registry, reviewer identities and quiescence barriers;
* runtime_review: review scheduling, retained triggers, triage and interventions;
* completion: readiness, review returns/knowledge and adversary reservations;
* coder_lifecycle: coder/snapshot identity, progress, revision and recovery locks;
* shutdown: terminal guards, patch delivery and recovery/archive bookkeeping.

Settings is a stateless configuration/preflight adapter. Parsing, fingerprints,
event decoding and evidence rules are functions with explicit input arguments;
fingerprint caches are supplied by runtime_review, never stored globally.

Each service has a slotted state record and a declared coordinator port. Port
reads can borrow mutable infrastructure (for example StateStore or the child
registry); port writes enumerate cross-owner lifecycle transitions. These are
internal collaboration interfaces, not security boundaries. Legacy attribute
descriptors and late-bound callbacks preserve the existing embedding/test API.
"""
