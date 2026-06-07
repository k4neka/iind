# Industrial Informatics — Flexible Production Line: Implementation Brief

You are working on a university project (FEUP, Industrial Informatics, MEEC
2025/26): automating a simulated flexible production line. This document is the
authoritative overview of the **current** system and the **changes to
implement**. Read it fully before touching code. Where a task says "verify", do
not assume — read the actual code and confirm.

---

## 0. Ground rules for you (the implementer)

- **Working directory & file scope.** You work inside `iind_proj/`. You may
  change any code **inside `iind_proj/`**. You may `cd ..` **once** (one level
  up) to *read* the SFS simulator, the `io.csv`, the project PDF, or other
  reference files — **read only, do not modify anything outside `iind_proj/`.**
- **Do not break working behaviour.** This system has been debugged across many
  sessions; the handshakes and completion flow are fragile and correct. When a
  comment says something is a deliberate invariant (e.g. "one product per
  chunk"), respect it unless this brief explicitly overrides it.
- **Read before writing.** Before editing a file, read it and the files it
  imports. Many "obvious" changes are already handled.
- **No CODESYS/PLC changes are possible.** The PLC (Codesys, IEC 61131) is
  fixed. You cannot add PLC variables. In particular you **cannot** add
  machine-occupancy or machine-working-state signals — none exist and none can
  be added. All timing/statistics must be derived MES-side from what the MES
  already knows: the ops it sent to each cell and the **scheduled** durations
  from the process tables. This is a hard constraint — design around it.
- **Keep changes minimal and localized.** Prefer adding functions over
  rewriting modules. Match the existing style (plain-prose comments, small
  functions, asyncio in the MES, threads in the ERP MQTT bridge).
- **Everything that must survive a restart goes in PostgreSQL.** In-memory dicts
  are lost on restart; the project requires persistence (Requirement 8).
- After each change: `python -m py_compile` the changed files, and keep
  `tests/` green if present.

---

## 1. The physical line (what is being controlled)

Simulated by SFS (Shop Floor Simulator) over Modbus/TCP. Nine cells:

- **W1, W2** — two automatic warehouses (assume 32 capacity each). W1 =
  input/raw side; W2 = output/finished side.
- **L** — loading cell, 5 docks. Raw material (Wood, Metal) is spawned here and
  moved into W1. Wood and Metal are the only raw materials; the line cannot
  create them.
- **C1–C4** — four machining cells, each with 3 machines (M1, M2, M3) on a
  6-conveyor ground floor: `Wout → M1 → T1 → M2 → T2 → M3 → Win`.
- **T** — transfer cell, 5 conveyors, moves pieces from W2 back to W1 (used by
  the complex-piece corridor; driven PLC-side, not by the MES).
- **U** — unloading cell, 5 docks. Each dock is a slider holding ≤6 pieces (the
  7th physical slot is the bottom conveyor; we use **6**). Discharging a dock
  drops its pieces to the floor (= delivery to client).

### Cell ground-floor occupancy (drives scheduling — see TASK 3)

Max 6 pieces per cell, but in normal operation there are ~4 on the floor: 2
being worked on M1 and M2, plus ~2 in transit (one waiting before M2, one
heading from M1 toward M2 — the optimized pipeline). Treat a cell as a 3-stage
pipeline **M1 → M2 → M3** when modelling throughput and wait time.

### Tools and transformations (PDF Tables 1–3) — `mes/transformations.py`

- Tool change between any two tools = **30 s**.
- Per-cell tool availability (`CELL_TOOLS`): C1/C2 shape **wood** (M1/M2 carry
  tools 1,2,3), C3/C4 shape **metal** (M1/M2 carry 4,5,6). M3 in every cell does
  assembly (8,9) plus one alt shaping tool (10 or 11).
- Startup tools per (cell, slot) are known — `mes/tool_state.py:_INIT_TOOLS`:
  C1/C2 → M1=1,M2=1,M3=8 ; C3/C4 → M1=4,M2=4,M3=8.
- Shaping recipes (`SINGLE_TRANSFORM`) and assembly recipes (`ASSEMBLY`) carry
  the exact `tool` and `time` per transformation. **These scheduled times are
  the source of truth for all MES timing** — we assume the scheduled time, we do
  NOT measure real machine time (TASK 2 removes the old measured counter).
- Piece IDs (`PIECE_ID`): Wood=1, Metal=2, RtopW=3, StopW=4, LegW=5, RtopM=6,
  StopM=7, LegM=8, RWW=9, SWW=10, RWM=11, SWM=12, RMM=13, SMM=14.
- **Final products** clients can order: RWW, SWW, RWM, SWM, RMM, SMM. Each = 1
  top + 2 legs assembled on M3. RtopX/StopX/LegX are intermediates, never sold.
- **Complex pieces** (mixed) RWM, SWM: wood top + 2 metal legs. The top is
  shaped in a wood cell (C1/C2); legs in the metal path; assembly in C3/C4. The
  W2→W1 move uses the PLC Router + transfer corridor, driven **entirely
  PLC-side** — the MES does not drive the corridor.

---

## 2. High-level architecture (5 requirements)

- **PLC (Req 1):** fixed. Controls cells, loader, router/corridor, 5 unload
  docks. Exposes GVL over OPC-UA (§4).
- **SCADA (Req 2):** Modbus/TCP, one cell, auto/manual/maintenance, 10-s
  stuck-piece alarm. *(Aware of it; out of scope here unless asked.)*
- **Cloud dashboard (Req 3):** an HTML page that subscribes to MQTT-over-
  WebSocket and renders machine tool usage + stats. **This brief feeds it real
  statistics and fixes the WSL connection — see TASK 8.**
- **MES (Req 4):** Python `mes/`. Receives orders, drives the PLC, manages
  docks, produces statistics.
- **ERP (Req 5):** Python `erp/`. Client orders (JSON/TCP), planning, owns the
  sim clock, sends orders to the MES.
- **Persistence (Req 8):** PostgreSQL (`db.fe.up.pt`, schemas `db_mes`,
  `db_erp`). Must survive restart without losing pending orders or statistics.

Data flow: clients → (JSON/TCP :6666) → **ERP** → (MQTT) → **MES** → (OPC-UA) →
**PLC** → (Modbus) → **SFS**. The MES reports status back to the ERP over MQTT
(`factory/mes/status`). A dashboard service reads the DB + status and publishes
an aggregated snapshot on `factory/mes/dashboard`, which the HTML page subscribes
to **over MQTT-WebSocket (broker port 9001)**.

### Sim time
1 sim day = **60 s** (`erp/config.py:SECONDS_PER_DAY`). The ERP owns the clock
(`erp/sim_clock.py`), persists `sim_base_day` in `db_erp.sim_state`, and on each
day rollover: replans, sends production/delivery orders, then sends an
end-of-day notification so the MES discharges loaded docks.

---

## 3. MES internals (current — `mes/`)

`mes/main.py` runs these asyncio loops concurrently:

- `warehouse_loop` — reads PLC `W1Count[1..2]` (Wood/Metal) and snaps the MES's
  local W1 model to it; reports W2 stock from the unloader ledger.
- `dispatcher_loop` (0.5 s) — `Dispatcher.tick()` chunks queued pieces and sends
  simple-piece subparts to free cells.
- `loader_ack_loop` / `loader_flush_loop` — drive the PLC loader (raw spawning).
- `timing_push_loop` (30 s) — currently pushes **measured** durations to the
  ERP (TASK 2 removes this measured path).
- `orchestrator.run_loop` — `ComplexOrchestrator` produces RWM/SWM.
- `completion_loop` (0.5 s) — consumes the PLC `g_Done_*` buffer: maps wire
  ParentID → `pending_pieces` row, marks COMPLETED, reports to the ERP, credits
  the unloader W2 ledger, releases the cell (via `orchestrator.on_complete`),
  and (currently) records a measured timing sample.
- `unloader.run_loop` — fills/discharges docks (§5).

### Key MES modules
- `config.py` — OPC-UA/MQTT/DB config, `PIECE_ID`, `CELL_TOOLS`,
  `COMPLEX_RECIPE`, dock constants (`NUM_DOCKS=5`, `DOCK_CAPACITY=6`).
- `transformations.py` — process knowledge (Tables 1–3), `TOOL_CHANGE_TIME=30`.
- `tool_state.py` — `ToolStateTracker`: per-(cell,slot) mounted tool, seeded
  from `_INIT_TOOLS`, updated by `apply_ops`, reset on PLC reconnect.
  **In-memory only; not persisted.**
- `optimizer.py` — `optimise_batch(...)`: queued pieces → chunks (one final
  product per chunk), target cell via round-robin over capable cells, computes
  the subparts (ops) per chunk; uses the tool snapshot to prefer same-tool
  dispatch.
- `dispatcher.py` — `Dispatcher` (simple) + `ComplexOrchestrator` (RWM/SWM),
  sharing a `busy_cells` lease set + `tool_tracker`. `cell_free(cell)` reads
  `Cell_N_Top_Status.free_cmd`; a cell is released from `busy_cells` on
  completion.
- `opcua_client.py` — `PLCClient`: all PLC reads/writes; cell handshakes, router
  intake, `g_Done` buffer, loader, and unloading-dock fill/discharge.
- `unloader.py` — `UnloaderManager` (§5).
- `database.py` — `db_mes`: `pending_pieces`, `production_timing`,
  `consumed_messages`.
- `mqtt_client.py` — `MESMqtt`: subscribes to ERP topics, maintains the local W1
  model (`w1_estimate`, `w1_consume`, `w1_sync_from_plc`), routes
  delivery/end-of-day to the unloader, publishes `factory/mes/status`.

### Dispatcher W1 gating
`tick()` has one global guard `if self._w1_total() == 0: return` (nothing runs
when W1 is fully empty). Per chunk it uses `_can_afford(subparts)` + `continue`
(not `break`), so a chunk needing wood that can't be afforded does **not** block
a later metal chunk. **W1 now has working PLC vars** (`W1Count[1..2]`), so the
W1 model is real, not a guess. **Verify** the per-chunk skip still holds and add
a small regression test; do not rewrite working dispatch.

---

## 4. MES↔PLC contract (OPC-UA, GVL) — do not rename variables

- W1 raw counts: `W1Count[1]`=Wood, `W1Count[2]`=Metal (PLC decrements as raw
  leaves W1 via a cell WarehouseOut; MES reads them). **These work now.**
- Cells: `Cell_N_Top_Order` (`.Workpiece` + `.recv_cmd`), `Cell_N_Top_Status`
  (`.free_cmd`). `cell_free(N)` already wraps the status read.
- Router intake: `g_Router_Order` (Workpiece + recv_cmd) — real ACK handshake.
- Completion buffer: `g_Done_PieceID[0..15]`, `g_Done_Parent[..]`,
  `g_Done_Type[..]` (PLC writes empty slots; MES reads then clears PieceID=0).
  **This is the only "a piece finished / a cell just got free" signal you
  have** — use it (TASK 3).
- Loader: `g_Loader_Wood_Qty`, `g_Loader_Metal_Qty`, `g_Loader_Exec`,
  `g_Loader_Status`.
- Unloading docks: fill via `g_Unloader_DockID`+`g_Unloader_PieceID`+
  `g_Unloader_Qty`+rising `g_Unloader_Exec`; discharge via
  `g_Unloader_DischargeDockID`+`g_Unloader_DischargeQty`+rising
  `g_Unloader_DischargeExec`; occupancy from `g_Unloader_DockCount[1..5]`. The
  per-dock gate is PLC-side; the MES drives one shared scalar request slot and
  serialises requests.

No `g_W2_Count` node exists. W2 stock is tracked MES-side in the unloader ledger.

---

## 5. Unloader (current — `mes/unloader.py`)

Whole-order delivery model, implemented:
- ERP delivery items carry `order_line_id`, `piece_type`, `quantity` (this
  slice), `line_quantity` (line total), `ddate`, `client_order_id`,
  `client_name`.
- `_lines[order_line_id]` = {client, order, piece_type, target, need, on_docks,
  docks:set}. `credit_w2(piece_type)` is called by `completion_loop` when a
  piece enters W2.
- `run_loop` drains demand: for each needy line (most urgent first by `ddate`,
  then oldest), pull `min(need, w2_stock)` onto **its** reserved dock(s), packing
  to 6 and spilling for >6. Pieces are fungible by type (urgent line eats
  first), but every line keeps its own `need`, so all clients are served.
- A line **discharges as one whole order** the moment `on_docks >= target`.
  End-of-day flush is a backstop for partially-built urgent lines.
- Emits MQTT `DOCK_LOADED` and `ORDER_DISPATCHED` (carry client, order, dock,
  piece_type, quantity).

**Gaps:** `_lines`/`_w2_stock`/`_dock_owner`/`_dock_count` and unloaded tallies
are in-memory only and not by type. See TASK 4.

---

## 6. ERP internals (current — `erp/`)

- `config.py` — `SUPPLIERS` (A: Wood min2 €10 lead0, Metal min4 €15 lead0;
  B: Wood min12 €2 lead2, Metal min8 €4 lead4), `BOM`, `PRODUCT_PRICES`
  (revenue), `BASELINE_STOCK` (Wood12/Metal8 buffer),
  `PLANNING_HORIZON_DAYS=30`, time constants (`TOOL_CHANGE_TIME_S=30`,
  `TRANSFER_TIME_S=5`, `CELL_HOPS=6`, `QUEUE_FACTOR=1.5`).
- `planner.py` — daily replan: schedules production per line by due date,
  fulfils from finished stock first (`from_stock`), reserves stock
  (`stock_reservations`), chooses suppliers by cost vs penalty.
- `time_model.py` — **already a real model-based estimator**:
  `estimate_production_seconds`, `expected_seconds(piece, cell,
  current_tool_state, preceding_type, timing_stats)` with a **tool-change
  credit** (static estimator run cold vs warm — never a hard-coded 30/60), and
  `expected_production_days`. Prefers measured `timing_stats` (≥`_MIN_SAMPLES`)
  and falls back to the static model. **After TASK 2, `timing_stats` will be
  empty, so it falls back to the static model — that is intended.**
- `database.py` — `db_erp`: `client_orders`, `order_lines` (with `produced`,
  `delivered`), `production_plan` (with `from_stock`), `purchase_plan`,
  `inventory`, `mes_status_log`, `production_costs`, `sim_state`,
  `stock_reservations`.
- `mqtt_client.py` — sends production_orders, delivery_orders, end_of_day,
  material_load; logs `factory/mes/status` to `mes_status_log`.
- `tcp_server.py` — accepts client orders (JSON) on :6666.
- `sim_clock.py` — sim day clock, persisted via `sim_state.sim_base_day`.

---

## 7. MQTT topics

- `factory/erp/material_load/wood`, `.../metal` — raw load (ERP→MES).
- `factory/erp/production_orders` — pieces to produce (ERP→MES).
- `factory/erp/delivery_orders` — whole-line delivery demand (ERP→MES).
- `factory/erp/end_of_day` — discharge trigger (ERP→MES).
- `factory/mes/status` — per-piece COMPLETED + unloader DOCK_LOADED /
  ORDER_DISPATCHED (MES→ERP; logged to DB; read by the dashboard service).
- `factory/mes/dashboard` — aggregated snapshot published by
  `dashboards/dashboard_service.py`; the HTML page subscribes to it
  **over WebSocket (port 9001)**.

---

## ====================  WORK TO IMPLEMENT  ====================

### TASK 1 — MES machine statistics from scheduled times, by order (Req 4.3)

**Intent.** Per machine `(cell, slot)`: total operating time, occupation %,
operating time **per tool**, number of tool changes, and **count of each
specific piece type operated on each machine** — derived from the **orders/ops
the MES actually sent** (not inferred from the final product alone). Plus
per-dock unloaded counts **by type** (TASK 4).

**Count per-op output, driven by the orders.** When the MES dispatches a chunk
it knows every op: `(cell, slot, tool, time_s, output_piece)`. Example for one
RWW built in C1: M1 shapes a wood→RtopW op (tool 1, 30 s) → "M1 in C1 operated
1 **RtopW**"; M1/M2 shape the two LegW ops → "operated 2 **LegW**"; M3 assembles
→ "operated 1 **RWW**". So "pieces operated by type per machine" is the histogram
of **each op's output piece**, attributed to the machine that ran it. This is
exactly "knowing the orders": the order defines the ops, the ops define the
per-machine per-type counts. Do **not** attribute the final product to the
shaping machines.

**Store at dispatch, commit on completion.** Record the op data at **dispatch
time** (persisted), but only **commit it into the statistics totals when the
piece is tagged COMPLETED** in `completion_loop`. Rationale: a dispatched piece
might never finish (PLC restart, scrap); we only count work that actually
completed. Persisting the ops at dispatch (not just in memory) means a mid-day
MES restart can still attribute a piece that completes after the restart —
required for Req 8. Concretely:
- add a JSONB column `dispatched_ops` to `pending_pieces`, written at dispatch;
- in `completion_loop`, after `mark_completed`, read that row's `dispatched_ops`
  and feed them to the statistics recorder.

**Tool changes & operating-vs-change time (keep them separate).** Track
`operating_seconds` (sum of op `time_s`) and `tool_change_seconds` separately,
plus a `tool_changes` count. A change is counted when the tool an op needs
differs from the tool `ToolStateTracker` says is mounted on that `(cell, slot)`
just before the op (then update the tracker). Keeping the two time buckets
separate lets the report compute occupation either way; **expose occupation %
twice**: `occupation_operating` (operating / elapsed) and `occupation_busy`
((operating + tool_change) / elapsed). elapsed = current `sim_base_day × 60 s`
read from `db_erp.sim_state`.

**Per-tool operating time.** Sum each op's `time_s` into
`machine_tool_seconds[(cell, slot, tool)]`. This is what the cloud dashboard
shows (cumulative tool use, Req 3).

**Schema (new `db_mes` tables, persistent):**
- `machine_stats(cell, slot, operating_seconds, tool_change_seconds,
  tool_changes, pieces_operated, updated_at)` — PK (cell, slot).
- `machine_tool_seconds(cell, slot, tool, seconds)` — PK (cell, slot, tool).
- `machine_piece_counts(cell, slot, piece_type, count)` — PK (cell, slot,
  piece_type).
- `machine_tool_state(cell, slot, tool, updated_at)` — PK (cell, slot): the
  **currently mounted** tool, so it survives restart (TASK 6).

**Where.** New `mes/statistics.py` with a `StatisticsRecorder` exposing
`record_completion(ops, tool_state_before)` (accumulates into the four tables in
one transaction) and read helpers for the dashboard. Wire `ToolStateTracker` to
persist mounted-tool changes through `machine_tool_state`. Hook
`record_completion` into `completion_loop` after `mark_completed`.

**Acceptance.** After a run, the four tables hold sensible non-zero per-machine
numbers; tool-change counts equal the switches the optimiser actually caused;
per-machine per-type counts equal the ops performed (RtopW/LegW on shaping
machines, RWW on M3); occupation reported both ways; everything survives an MES
restart.

### TASK 2 — Remove the per-piece real-time timing counter

**Intent.** Drop the measured per-final-piece "time to create" counter (it
produced unreliable times). Statistics now come from scheduled times (TASK 1).

**Where & how.**
- In `completion_loop`, remove the `add_timing_sample(...)` /
  `last_completed_on_cell(...)` measurement block.
- Stop `timing_push_loop` pushing measured durations. Cleanest: remove
  `timing_push_loop`, and the now-unused DB functions `add_timing_sample`,
  `get_timing_samples`, `distinct_timing_keys`, `last_completed_on_cell`,
  `push_timing_to_erp` and the `production_timing` table — **and** make
  `erp/time_model.read_timing_stats` return `None` gracefully so
  `expected_seconds` falls back to the static estimate. No dangling imports.

**Acceptance.** No wall-clock per-piece measurement remains. The ERP still
produces day estimates via `time_model` (static + tool-change credit + the new
pipeline model from TASK 3). MES statistics are entirely scheduled-time based.

### TASK 3 — Shared cell-capability + pipeline-wait model, fed by completions

**Intent.** ERP **and** MES must plan with a shared understanding of (a) what
each cell can actually do and how long each piece takes there (scheduled time +
tool change), (b) the **wait** a piece incurs queueing on the cell pipeline, and
(c) the live "this cell just freed up" signal from completions, so they don't
over-commit a cell. You explicitly flagged the M2-behind-M1 coupling: a piece
bound for M2 sitting behind a piece bound for M1 does NOT start at M2 when the
two current M2 jobs finish (e.g. 20 s) but only when the preceding M1 job (e.g.
30 s) completes and frees the pipeline slot. Model that.

**Method (a model, not a PLC simulator — no occupancy signals exist).**
- Single source of truth for "time to make piece X on cell C": reuse
  `erp/time_model.expected_seconds` (static estimate + existing tool-change
  credit). Do not duplicate the 30 s constant.
- Add a `CellPipeline` / `schedule_day(cell, ordered_pieces)` helper in
  `time_model.py` that models each cell as stages M1→M2→M3 with per-stage
  `free_at` timelines. For each piece in sequence: `start_at_stage =
  max(stage.free_at, arrival_from_previous_stage)`. A piece's wait at M2 =
  `max(M2.free_at, its_M1_finish) − its_M1_finish` — this yields the larger
  30 s gate in your example, not 20 s. Advance each stage's `free_at` as pieces
  pass. Return each piece's estimated finish sim-time.
- Per-hop transport slack ≈ 5 s (use `TRANSFER_TIME_S=5`), added per hop. Add a
  config `TIME_TOLERANCE_S = 5` meaning "about 5 s, not exactly" — for the
  **deadline check** use the pessimistic `est + TIME_TOLERANCE_S` so we never
  promise a delivery we'll miss; use `est − TIME_TOLERANCE_S` only for optimistic
  display. (You said "+/- ~5 s"; pessimistic-for-deadline is the safe reading.)
- **Live cell-free feedback.** The only "cell freed" signal is the `g_Done`
  completion (already consumed by `completion_loop`, which releases the cell
  from `busy_cells`). Use that as the trigger to re-evaluate the next dispatch —
  a freed cell becomes a candidate in the very next dispatcher tick (already true
  via `busy_cells` release; confirm it). Also make the ERP's remaining-day
  projection react to the completion events it sees on `factory/mes/status`, so
  its plan stays accurate as cells free up.

**Where.** Extend `erp/time_model.py` (pipeline helper); `erp/planner.py` uses it
to order/limit the day's plan to what can finish by `ddate` and to drive supplier
choice (TASK 7). ERP and MES are separate processes — keep ONE implementation in
`time_model.py`; if the MES needs the same helper, import a shared copy (a tiny
shared module or a duplicated-but-identical file with a comment pointing at the
source of truth). Do not let two divergent wait-models exist.

**Acceptance.** The day plan's per-piece finish reflects pipeline waiting (the
M2-behind-M1 case gives the 30 s gate), plus transport slack, plus the ±~5 s
tolerance on the deadline check. Add a `tests/` unit test for the M2/M1 coupling
example. The plan reacts to completion events (a freed cell is reused promptly).

### TASK 4 — Persist unloader state + per-type unloaded counts (Req 8 + 4.3)

**Intent.** Unloader books survive restart; unloaded counts are **by type**.

**Where & how.** New `db_mes` tables: `unloader_lines`, `unloader_w2_stock`
(piece_type→qty), `dock_state` (dock→owner_line_id, count), `unloaded_pieces`
(dock, piece_type, qty). In `UnloaderManager`, write through on every mutation
of the books (they're tiny — rewriting the whole small book per change is fine),
and **load** them on first run so a mid-day restart resumes lines, W2 credit and
dock ownership. On discharge, record discharged pieces **by type** in
`unloaded_pieces`. Make the DB reflect current dock occupancy and dispatched
orders so the dashboard can read it.

**Acceptance.** Kill the MES mid-day with pieces on docks + pending lines;
restart; the unloader resumes the same lines/credit/ownership and still
discharges whole orders. `unloaded_pieces` shows per-dock per-type totals.

### TASK 5 — Complex production: tops on both wood cells (throughput)

**Intent.** Tops for RWM/SWM currently shape on a single wood cell
(`top["cells"][0]`), bottlenecking two assembly cells. Round-robin tops across
**both** wood cells (C1, C2).

**CAUTION (you flagged this).** The PLC Router moves the top W2→W1 through the
transfer corridor and re-injects it into the assembly cell; it drains its
capture table FIFO, one transfer at a time. So: alternate the top's **source
cell** across C1/C2, but keep the **corridor transfer itself serialised** (the
PLC already does this). Do not raise `inflight` beyond the assembly-pool size;
just stop pinning tops to one wood cell.

**Where.** `ComplexOrchestrator._next_top_machine` / top-cell selection in
`_produce`; round-robin over `top["cells"]` (already `[1, 2]`).

**Acceptance.** Under a burst of RWM/SWM, both C1 and C2 shape tops, both C3/C4
assemble, no piece lost or mis-routed (g_Done completions == dispatched complex
pieces; no router deadlock). **If router safety can't be guaranteed, leave it
serialised and document why** — never trade correctness for throughput.

### TASK 6 — Persist current mounted tool per machine (startup tools known)

**Intent.** The MES knows startup tools (`_INIT_TOOLS`) and must **persist the
current mounted tool per machine** so stats + dashboard survive restart.

**Where.** Covered by TASK 1's `machine_tool_state` table:
`ToolStateTracker` loads from it on startup (falling back to `_INIT_TOOLS` if
empty) and writes through on every `apply_ops`. On PLC reconnect, `reset()`
restores `_INIT_TOOLS` **and** writes that back to the DB (the PLC restarted, so
mounted tools really are the defaults).

**Acceptance.** Restarting the MES (no PLC restart) preserves mounted tools; a
PLC reconnect resets them to defaults in both memory and DB.

### TASK 7 — ERP supplier/profit decision using prices + penalties (Req 5)

**Intent.** With `PRODUCT_PRICES`, supplier prices, and per-line `penalty`, the
planner chooses suppliers and sequences orders to maximise net benefit:
revenue − raw cost − expected penalty.

**Method.** For each line, estimate the finish day with the TASK 3 model. If it
hits `ddate` using cheap bulk supplier B (long lead), prefer B. If B's lead would
miss the deadline, compare the extra cost of fast supplier A vs the penalty
(`penalty €/day × days late`) of using B, and pick the cheaper total — log the
reasoning (the report needs it). Respect `WAREHOUSE_CAPACITY` and the
`BASELINE_STOCK` buffer.

**Where.** `erp/planner.py` supplier-selection path (there's a `_choose_supplier`
-style routine — read it). **Fix the noted profit-formula bug**: `expected_profit`
divides `net_qty` by total BOM raw, conflating pieces with raw units; make it
per-piece (revenue − per-piece raw cost − per-piece expected penalty share).

**Acceptance.** Tight deadline + high penalty → picks fast supplier and logs that
the penalty exceeded the cost delta; slack deadline → picks cheap supplier. The
logged profit figure is per-piece correct.

### TASK 8 — Cloud dashboard: real stats + fix the WSL/WebSocket connection

**Context (this is your actual dashboard bug).** There is **no HTTP server** —
the dashboard is an HTML page (`dashboards/dashboard.html`) you open in a
browser; it talks **MQTT-over-WebSocket** via Paho-JS to `localhost:9001`, topic
`factory/mes/dashboard` (see the page's MQTT BROKER (WS) host/port inputs,
default `localhost`/`9001`). `dashboards/dashboard_service.py` reads the DB and
**publishes** that topic over normal MQTT (1883). It never connected because (a)
the broker's **WebSocket listener on 9001** is usually not enabled by default,
and (b) from a **Windows browser the WSL `localhost` may not reach the WSL
broker**. So this is NOT a "self-discovering HTTP port" problem — fix the
MQTT-WS path and surface the right host/port.

**Do.**
1. **Feed real statistics.** Extend `dashboard_service.get_data()` to also read
   the new `db_mes` stats tables (`machine_stats`, `machine_tool_seconds`,
   `machine_tool_state`, `machine_piece_counts`, `unloaded_pieces`) and include
   them in the published snapshot. The HTML must render, per machine: currently
   mounted tool, available tools (from `CELL_TOOLS`), cumulative per-tool time,
   tool-change count, pieces-operated-by-type, occupation % (both variants), and
   the unloaded-by-type + order/dock status.
2. **Consolidate to ONE dashboard.** Keep a single `dashboards/dashboard.html`;
   delete `cloudash.html` and `cloud_dashboard.html`.
3. **Fix the WS connection for WSL→Windows.**
   - Provide a Mosquitto config (`dashboards/mosquitto_ws.conf`) enabling BOTH
     listeners: `listener 1883` (TCP) and `listener 9001` +
     `protocol websockets`, with `allow_anonymous true`. Document running the
     broker with it (`mosquitto -c dashboards/mosquitto_ws.conf`).
   - **Print the connection info clearly in the console** on startup
     (`dashboard_service.py` and/or `run_all.sh`):
     `Dashboard: open dashboards/dashboard.html, connect MQTT-WS to <HOST>:9001`
     where `<HOST>` is auto-detected: print BOTH `localhost` AND the WSL eth0 IP
     (`hostname -I | awk '{print $1}'`) so the user can pick whichever their
     Windows browser reaches. Also write this to `dashboards/.dashboard_conn`
     so `run_all.sh` can echo it.
   - Make the HTML default host/port configurable via URL query params
     (`?host=...&port=...`) so the user can point it at the printed WSL IP
     without editing the file.

**Acceptance.** One dashboard file. With the broker started using the provided WS
config, opening the HTML and connecting to the printed host:9001 shows live
machine tool usage + stats from the DB. The startup log prints both `localhost`
and the WSL IP plus the WS port.

### TASK 9 — `run_all.sh` launches everything incl. dashboard + requirements

**Intent.** One command boots ERP, MES, the dashboard service, and optionally
the GUI; Python deps auto-install into per-component venvs; the dashboard
connection info is printed.

**Do.**
- Ensure `run_all.sh` provisions ERP + MES venvs (pip guaranteed via
  ensurepip/get-pip fallback — a robust bootstrap exists from prior work) and
  installs each `requirements.txt`.
- Launch ERP → wait for :6666 → launch MES → launch the dashboard service (from
  a venv that has `psycopg2-binary` + `paho-mqtt`; add
  `dashboards/requirements.txt` with those).
- After the dashboard service starts, **read `dashboards/.dashboard_conn` and
  echo it** so the user sees the host(s) + WS port and which HTML to open.
- Optionally check the broker's 9001 WS listener is up; if not, print the
  one-line hint to start Mosquitto with `dashboards/mosquitto_ws.conf`.
- `--gui` also launches `client_gui.py` (stdlib + tkinter only).
- Clean Ctrl-C shutdown of all children.

**Acceptance.** `./run_all.sh --gui` from a fresh checkout boots all components,
auto-installs deps, prints the dashboard host(s)/WS port + which file to open,
and Ctrl-C stops everything.

---

## 9. Things NOT to do

- Do **not** add or assume any PLC/CODESYS variable for machine occupancy or
  working state. All occupancy/operating numbers are derived from **scheduled
  times** the MES controls.
- Do **not** modify anything outside `iind_proj/` (you may `cd ..` once to
  *read* SFS/io.csv/PDF, read-only).
- Do **not** batch M3 assembly beyond 1 top + 2 legs — the SFS M3 consumes every
  parked leg when a top arrives, so "one product per chunk" is a physical
  invariant.
- Do **not** re-introduce a W2→W1 transfer for finished goods; finished products
  stay in W2 until the unloader pulls them. The complex top corridor is
  PLC-side.
- Do **not** measure wall-clock per-piece production time (TASK 2 removes it).
- Do **not** invent an HTTP server for the dashboard — it is MQTT-over-WebSocket;
  fix that path instead (TASK 8).

## 10. Suggested order of work

1. TASK 2 (remove measured timing) — clears the path.
2. TASK 1 + TASK 6 (statistics + persisted tool state) — core deliverable.
3. TASK 4 (persist unloader + per-type unloaded).
4. TASK 8 + TASK 9 (dashboard from real stats + WS fix + launcher).
5. TASK 3 (pipeline model) + TASK 7 (supplier/profit) — planning quality.
6. TASK 5 (complex throughput) — only if router safety is provable.

After each task: `python -m py_compile` the changed files, run
`tests/run_all.py` if present, and do a short end-to-end run with `./run_all.sh`
to confirm no regression in the completion/unload flow.