# Local app implementation contract

The installed service remains the only durable job engine. Native UI talks to loopback REST with `owner.token`, read by C++ from `CREALITY_AGENT_HOME` or `~/Library/Application Support/CrealityAgent/runtime`. Neither this token nor the agent token enters JavaScript, model prompts or logs. External model tools retain agent authorization only. Operator routes reject non-loopback callers and agent credentials.

## Native UI transport

Load only bundled `resources/web/local_agent/index.html`. JavaScript sends `{command:"local_agent_request",data:{request_id,action,payload,idempotency_key}}` through the existing native message handler. C++ whitelists actions and sends GET `/v1/operator/state` for `state`, otherwise POST `/v1/operator/actions` with `{action,payload,idempotency_key}`. Return `{command:"local_agent_result",data:{request_id,ok,result,error}}` using the existing JS command mechanism. Preserve authorization natively. Never interpret a model/tool result as a native operator request.

State: `{jobs,printers,profiles,policy,model,alerts,conversations}`. Printer enrollment fields are owner-visible; secret fields are omitted. Model: `{base_url,model,api_key_set}`. Conversations include messages as structured text/tool records; no rendered HTML or camera bytes.

Actions: `create_job`, `select_model`, `prepare_job`, `queue_job`, `start_job`, `pause_job`, `cancel_job`, `resume_job`, `approve_resume`, `approve_budget`, `save_model`, `save_policy`, `enroll_printer`, `save_profile`, `enroll_reference`, `save_cfs_inventory`, `probe_printer`, `open_project`, `chat`, `new_conversation`, `get_conversation`, `acknowledge_alert`.

Existing job actions use existing typed payloads plus `job_id` where required. `approve_resume`: `{job_id}`. `approve_budget`: `{job_id}` bound to current G-code hash and estimates. `chat`: `{conversation_id?,message}` returns immediately; poll state. `save_model`: `{base_url,model,api_key?}` local/LAN endpoint only. `save_policy`: `{max_hours,max_grams,monitoring_loss_seconds,pause_on_monitoring_loss}` confirms both policies. `enroll_printer`: `{printer_id,...operator-editable connection/nozzle/camera/material fields}`; no control/vision/auto-start qualification flags accepted. `save_profile`: typed Profile record. `enroll_reference`: `{printer_id,bed_clear_confirmed,roi}`; camera image never returned. `save_cfs_inventory`: `{printer_id,slots:[{slot_id,material,color,remaining_grams,verified}]}`. `probe_printer`: `{printer_id}` read-only status. `open_project`: `{job_id}` returns local path/hash for native isolated inspection; do not load into owner's current Plater.

## GUI helper

Custom binary supports `--local-agent-prepare <request.json>` in a separate process/profile, and `--local-agent-inspect <project.3mf>` in an isolated instance. Input manifest: `{version:1,request_id,job_id,model_path,input_sha256,settings:[profile paths],filaments:[profile paths],overrides:{},copies:1,output_project}`. Helper validates source hash/paths, applies exact effective profiles and supported explicit overrides, prepares using its private Plater, saves editable `.3mf` without triggering send/start, and atomically writes sibling `result.json`: `{version:1,request_id,ok,project_path,input_sha256,settings_sha256?,warnings,error?}`. A result is accepted only after exit success and independent ZIP/mesh/config validation. Unsupported input/override is an error. No external URLs, camera data, heating/movement/control or native modal dialogs. No state from the main scene is used.

The parent then re-slices the saved 3MF with no external profile flags. Missing helper, stale/invalid/partial export and unresolved geometry/profile warnings remain holds. Inspection must also isolate data/project state.

## Worker ownership

Root owns Python config/API/engine/operator routes/chat/store and service tests. UI worker owns MCPChatPanel header/source, new native bridge files and `resources/web/local_agent`. Helper worker owns GUI helper files and necessary CLI/GUI startup plumbing. Packaging worker owns macOS packaging/notification helper scripts and build survey. Coordinate CMake changes with root, no overlapping edits. No worker commits, GitHub writes, printer calls or firmware changes.

Owner state includes `questions: [{id,job_id,question,status,answer,created}]`.
Agents can use `request_owner_input` and `list_questions` (API GET/POST `/v1/questions`).
Only the local owner action `answer_question` accepts `{question_id,answer}`.
Answers persist across restart and do not grant resume, budget, or printer qualification.
