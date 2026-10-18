# Gemini offload timeout and uncertain turns

The dispatcher sets `agy --print-timeout` to the outer offload timeout minus a
15-second receipt margin, using seconds so a short budget is not rounded down to
one minute. The outer subprocess deadline remains the hard cap.

If agy's run log explicitly says `print timeout after … with turn in progress;
returning partial output`, the dispatcher records the result as uncertain, keeps
the scrubbed native log tail and run identity, and does not retry that invocation.
The diagnostic does not prove provider success, failure, cancellation, or served
model identity. Callers must reconcile the existing operation before retrying or
falling back to another provider. The result's `request_metadata` records both
the final prompt content digest/byte count and the exact payload sent to agy;
for stream-json transport, that second digest includes the NDJSON encoding.
Gemini's `cli_selected_model` is explicitly sourced from agy's selected-override
log line and does not establish the backend-served model; `served_model` stays
unknown for Gemini.

The staged closer retains an uncertain interpretation as a durable target barrier.
It suppresses alternate-profile fallback, strong interpretation and actions for
that target, including on a later head or assignment. It never promotes the local
dispatcher run ID to proof of a native asynchronous operation. Stop validation
remains invalid and requires continuation until the native outcome is established.
A supported native turn-status or cancellation operation has not been established;
conversation resume and daemon status do not establish that a turn ended.
