# Non-Claude data-only candidate implementation

This slice wires Vibe's installed programmatic confinement primitives into an
experimental candidate planner and an inert bootstrap driver. The public adapter
entry point is `prepare_non_claude_data_only_candidate("vibe", ...)`. It prepares
an explicit pinned plan for `vibe-medium-3.5`; production admission remains closed.

## Available controls and remaining work

| Candidate | Native controls present but previously unwired | Untested configuration or missing implementation |
| --- | --- | --- |
| Vibe Medium 3.5 | Public zero-source harness manager; explicit configuration; wildcard tool denial with cleared allowlist; empty hook result and MCP list; injected headless AgentLoop; exact model name and high thinking | Real observing-backend binding, complete installed closure review, exact server-model acceptance, and subscription/no-paid-route assurance |
| Cursor Composer | Client Read/Write/Shell/MCP deny checks; private config/data roots; workspace/project/index exclusion; single turn and update controls | Universal rejection before every tool handler and complete team-hook/MCP startup suppression; native deny selectors need matcher/dispatch coverage |
| Gemini via installed agy | Print/plan/sandbox and slash-expansion controls | Comprehensive tools/MCP/hooks/context contract is unproved; no separate official Gemini CLI found on PATH or checked package locations |
| Muse Spark/Wonder | Direct native payload path bypasses the wrapper updater; empty preset; policy and session-MCP controls | Tool augmentation and global MCP/hook policy precedence need source-backed proof; session-MCP disable does not establish global disable |

These are transport engineering states. They do not establish that a model lacks
reasoning capability or entitlement. Structured-output support is optional; strict
JSON decoding and semantic validation remain separate from confinement.

Cursor's installed `index.js` has Read denial near byte 4746400, Shell hard denial
near 4774055 and MCP denial near 4787526. Search can execute ripgrep before read
filtering near 4654888, and already-approved MCP handling can precede denial near
4777000. Team hooks are a separate surface in `8008.index.js` near 726600. These
facts make a universal dispatch boundary additional work, rather than a flag our
adapter can safely assume already solves the contract.

## Vibe implementation

Installed Vibe 2.15 public APIs provide a smaller first implementation than a
patched Cursor runtime. `init_harness_files_manager()` accepts no sources. The
result has no user/project configuration, tools, agents, skills or prompts to
discover. The candidate uses explicit configuration and the built-in default
agent. It clears the tool allowlist, denies `*`, supplies empty hooks/MCP and no
history, and disables connectors, project context, telemetry and experiments.

The plan uses a pinned Python `-I -S -B` bootstrap, private HOME/config/cache/cwd
paths, explicit import roots, and exact interpreter/source/tree manifests. `-S`
avoids ordinary site `.pth` startup. Manifest drift, extra files and callback/API
source drift fail closed. Preparation performs no SDK import, credential read,
configuration write or provider request.

The inert driver initializes zero sources before configuration, requires the
public API and backend factory to be pinned, checks an empty available-tool set,
and denies network access throughout. The raw-response observer rejects aliases,
model mismatch, missing/multiple responses, tool activity and incomplete results
before response mapping. Requested high thinking is a real native configuration
field; observed effort remains UNKNOWN.

The native backend currently drops the response's model identity. A real passive
observer must retain that upstream field. `load_installed_provider_backend()` is
an explicit unimplemented gate. The installed default provider URL and secret
variable are metadata, not an authorized subscription or paid API route. No live
provider path, additional credentials or role activation is enabled by this slice.

## Next bounded work

Review the installed backend/client observation interface and bind a passive
observer without changing provider/auth routing. Verify the exact runtime closure
and sterile API initialization with inert providers. Establish existing-account
subscription/no-paid-route evidence before proposing a bounded live trial.
Retain all failed trials and keep profile metadata, offline fixture success,
provider suitability and activation as separate states.
