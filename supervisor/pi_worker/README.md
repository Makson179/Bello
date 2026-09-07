# Bello Pi worker

This directory is Bello's private Node.js bridge to the pinned Pi SDK. Standard
input and output are reserved for one-JSON-object-per-line protocol frames;
diagnostics go to standard error.

The host chooses every thread and turn identifier. The worker never executes a
command or reads or writes a project file itself. Model tools are registered from
the `tools` array supplied by Bello and forwarded to the host as `bello/tool`
requests with stable Pi tool-call identifiers. Bello owns filesystem scope,
approvals, sandboxing, command output events, and durable tool-call de-duplication.

Pi sessions and the worker's normalized turn ledger are stored beneath the
absolute private `stateDir` passed to `initialize`. Ambient Pi extensions,
skills, prompt templates, context files, themes, settings, and built-in coding
tools are disabled.

Every completed assistant item carries the exact normalized Pi SDK usage fields
reported for that provider response plus provider/model/API provenance. The turn
ledger exposes their arithmetic sum and retains the individual response records;
missing provider fields are not estimated.

Run with Node.js 22.19 or newer:

```sh
npm ci --ignore-scripts
npm test
node worker.mjs
```

Provider credentials are configured through Pi's own login implementations;
Bello never copies a Codex credential store:

```sh
BELLO_PI_AGENT_DIR=/private/host/path node auth.mjs openai-codex
```

Before any paid turn, the host can validate an exact route and its requested
capabilities with `model/validate { provider, model, effort, serviceTier }`.
`model/list` returns only authenticated, available routes in `data`; passing
`includeUnconfigured: true` adds the full known `catalog` with explicit
`configured` flags, input modalities, exact supported efforts, effective
default, and provider-control routes. OpenAI effort aliases such as
`minimal -> low` are rejected instead of silently changing intensity. For
provider adapters that use budgets or another non-literal control, the route is
reported as `adapter_defined` rather than inventing a provider effort. An
omitted effort resolves to Pi's effective default and is marked as defaulted;
it is not recorded as an explicit `off` request. No unavailable route is
substituted. For compatibility with Bello 0.5.2, the OpenAI Codex Astra, Sol,
and Terra routes accept `ultra`; the provider payload is rewritten to literal
`ultra` even though Pi 0.85.1 uses `max` only as its internal session
bookkeeping level.

The host tool contract stays durable across resume, but `view_image` is not
registered in a session whose selected model lacks the `image` input modality.
The worker also rejects such a call before dispatching it to the host.
