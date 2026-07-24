# Hackathon State — AdventureX 2026

**Purpose**: single cross-agent reference for where this project stands. If you're a fresh agent/session picking this up, read this first, then verify anything time-sensitive live (robot IP, which blueprint is running, dimOS install status on the Ascent) rather than trusting it blindly — this is a hackathon, state changes hour to hour. Companion docs at repo root: `ARM_BRAINSTORM_CONTEXT.md` (Galaxea A1Z detail), `ASCENT_BRAINSTORM_CONTEXT.md` (Ascent GX10 infra detail), `ROBOT_HANDOVER.md` (Go2 connect/run cheatsheet), `PWA_TEAMMATE_BRIEF.md` (Go2 control-panel backend architecture).

**Event**: AdventureX 2026, Hangzhou. **Submission deadline: 2026-07-26 01:00** — this doc was written 2026-07-25, so under 24h remain.

**Track**: Dimensional (dimOS), plus whatever base/general tracks apply. AgileX Robotics and D-Robotics tracks were ruled out earlier (require their own branded hardware, which this build doesn't use).

---

## 1. Hardware on hand

| Item | Role |
|---|---|
| ASUS Ascent GX10 (`spark-7f06`, `10.76.4.120`) | Borrowed. aarch64, NVIDIA GB10 Grace Blackwell, CUDA 13.0, 121GiB unified RAM. Target final on-stage "brain" server — the whole stack (dimOS + control panels) is meant to run from here, offline, with clients (iPad/glasses) talking to it over LAN. |
| Unitree Go2 (Air) | Borrowed. Rooted (root access authorized, not yet exploited for firmware unlock). Runs dimOS's navigation/teleop/telemetry stack. Onboard L1 lidar is already a real 360° sensor (see §3). |
| Galaxea A1Z arm | Borrowed. 6-DOF + gripper, **no e-stop, no brakes** — motors falling disabled means free-fall. Manual teleop is solid; autonomous pick/place is unbuilt. |
| Rokid glasses | Borrowed. Scoped down to mic-input-only for now (deeper CXR display integration plan was dropped). |

---

## 2. The two demo concepts on the table

### Demo 1 — Vending-station delivery (locked as of 2026-07-24, per `demo-concept-arm-decision`)

Drinks lined up with ArUco tags (or hardcoded positions). Voice command names a drink → arm locates the tagged drink via `solvePnP`, picks it up, drops it in a basket on the Go2's back. Go2 already knows the coordinates of the table that asked, navigates there using dimOS obstacle avoidance + path planning, delivers, user takes drink.

**Why this was chosen over a general "VLA desk-cleaner" alternative**: ArUco pose estimation is deterministic classical CV (no model, no depth camera, no open-ended perception problem); a single grasp exposed once beats compounding failure probability across a multi-object pick sequence; stays fully offline with no second local-inference dependency beyond the already-committed ASR+LLM.

### Demo 2 — Fireman helper (new, not yet scoped/built)

Robot maps a room; since heat can degrade real lidar, uses a LingBot-map-style camera+model approach as a lidar-equivalent (dimOS team has explicitly said this exact capability — lidar-free heat-robust mapping — would be a genuine differentiator for robots generally, not just this hack). Adds an exploration mode (stay-near-walls-style frontier exploration, not fixed waypoints) instead of ordinary point-to-point nav. Later/optionally fuses an IR camera to find hot spots — for the hackathon, hot-spot locations would be hardcoded (cardboard mockup room), not real IR-detected. Mapping/heavy compute runs on the Ascent (or onboard if it were a Thor-class unit; Ascent stands in as proof-of-concept here). A human "fireman" enters, gets basic data streamed to the Rokid glasses, and voice-controls the robot to explore specific areas, take actions, or use an extinguishing liquid — needs a real NLP→actionable-command pipeline running on the Ascent. When the liquid runs out, the Go2 returns to a station where the arm removes the spent bottle and loads a full one, then the dog goes back out.

**Why it's appealing**: more novel, closer to a real-world problem, and the dimOS team has independently said the heat-robust-mapping piece specifically would matter to them beyond this hackathon.

**Why it's risky**: it's really 5 hard subsystems chained together (heat-robust mapping, frontier exploration, IR fusion, voice-driven NLP→command translation, and a robot-to-robot liquid-refill handoff that's essentially demo 1's arm+dog handoff but reversed/repeated) — none of which are built, with under 24h left including sleep/pitch-prep time.

**Technical note worth carrying forward if demo 2 (or its mapping piece) gets pursued** — spin-while-translate coverage trick: the Go2's camera FOV (~60°) is much narrower than its physical lidar's 360°, so a camera-only "heat-robust" map needs the body to sweep to get wide coverage. This is achievable without trading off travel progress: the Go2 accepts independent body-frame `(vx, vy, wz)` every tick (it can strafe, so spin and net-forward-progress aren't in conflict the way they'd be for a wheeled/car-like robot). Rotate the world-frame goal vector into the *current* body frame each tick using live yaw off odometry (`peek_stream('odom')`, already used by the telemetry panel), and send that alongside a separate `wz` — continuous spin ("lighthouse") or an oscillating sweep (safer, keeps more forward-facing dwell time for immediate-obstacle response). This would be a new small control task, not a modification of the frozen Go2 teleop path. Bounded, well-understood work — not a research risk in itself, but still one more subsystem to wire into demo 2's chain.

---

## 3. What's actually built and verified right now

### Go2 — solid, most complete piece of the whole project
- Full control panel (`dimos-pwa`, repo `666labs/dog-pwa`, branch `alex-branch`, ~4700 lines) live-tested against the physical robot: BLE wifi provisioning wizard, press-and-hold teleop (**frozen, don't touch**), curated sport/gestures panel with real firmware-status feedback (some moves like FrontFlip/BackFlip/Handstand are firmware-rejected on this unit, code 3203; FrontPounce works), low-latency MJPEG camera, dependency-free WebGL 3D lidar point-cloud viewer, real x/y/z/yaw odometry, customizable Foxglove-style dashboard, installable as an iPad PWA.
- Onboard L1 lidar is a real, already-360° sensor — but dimOS has a documented Unitree firmware quirk where it publishes an accumulated `world`-frame map with stale scans re-stamped with fresh timestamps, not a live egocentric per-frame scan. Relevant if demo 2 wants to simulate "lidar degraded by heat" — the real lidar already isn't naive live sensing, so faking degradation may just mean ignoring/discounting it in favor of the camera path.
- dimOS's own autonomous nav/exploration blueprints (`unitree-go2-nav-3d` — A* replanning, frontier exploration) are real and runnable, not just library code — relevant if demo 2's "exploration mode" gets pursued, since it may not need to be built from scratch.
- Backend has two distinct control paths worth knowing apart (per `PWA_TEAMMATE_BRIEF.md`): the agent/text-command path (`/submit_query`, natural-language into whatever agent is wired up — the "autonomy" story) vs. the phone-tilt teleop path (`teleop-phone-go2`, direct `TwistStamped` velocity control with a dead-man's-switch safety pattern). Judging reportedly rewards genuine autonomy over teleop — worth keeping the manual-drive UI visually/narratively secondary regardless of which demo is chosen.
- Known gap: **no auto-reconnect watchdog** — connection has died mid-session twice already (port conflict, WebRTC track failure); currently needs a human to notice and manually reconnect. Highest-risk gap for a live demo regardless of which demo concept is chosen.
- Known constraint: only one WebRTC client connection to the robot at a time — coordinate before launching a new blueprint (`dimos status` / `dimos stop`), and `dimos stop` does not clean up orphaned satellite processes (`humancli`, `rerun-bridge`) that can block a fresh launch.
- Vendor 控制台真机待验证项（2026-07-25 机器人没电延后）：Go2 相机代理出画面、
  真导航中 /api/vendor/estop 实停（零速连发路径）、Ascent 上臂 USB 相机设备号
  （vendor_config.json: arm_camera_device）+ ARM_CAM_PY 解释器确认、隧道端到端
  （cloudflared → https://dimos-drinks.vercel.app/?backend=<隧道URL>）。

### Galaxea A1Z arm — manual teleop solid, autonomy is the real gap
- Real vendor-SDK-backed DimOS adapter (from upstream draft PR `dimensionalOS/dimos@adventure_x`), not a mock.
- Best working teleop path: `merged-keyboard-joint-teleop-galaxea-a1z(-enabled)` — single-clock per-joint jog, smooth, PD-gains hand-tuned. A browser-based variant (`web-keyboard-joint-teleop-galaxea-a1z`) exists but is **not yet bench-tested on real hardware**.
- Gripper is wired into the two current jog paths but also **not yet bench-tested** against the physical gripper.
- **No perception at all on the arm side** — no camera/vision integration, no ArUco detection/pose code, no grasp-pose calibration.
- **No autonomous grasp/pick sequence** — no code that takes a target pose and executes close-gripper → lift → move-to-drop. DimOS's Drake-based `ManipulationModule` (planning/FK/viser viz) is present in every blueprint but its higher-level planning features are untouched/unevaluated — worth investigating as a starting point rather than scripting joint targets from scratch.
- **No voice/LLM trigger wired to anything** — the entire "voice command → orchestration" layer is unbuilt for both robots.
- **No Go2 handoff coordination** — nothing synchronizes "arm picked up the item" with "Go2 is in position to receive it," for either demo concept.
- Recovered-from incident: an encoder desync from a mid-session free-fall was fixed by hand; no lasting hardware issue.

### Ascent GX10 — infra mostly ready, project stack not yet deployed there
- SSH access, GPU driver mismatch fixed, auto-updates masked so nothing drifts unexpectedly mid-event.
- A teammate's (Helios's) vLLM stack already runs here in 3 healthy Docker containers (ports 7060/8065/8070) — this is the most likely candidate to serve as the local ASR+LLM "brain," and is proof this exact chip runs a modern aarch64+CUDA13 stack. **Do not disrupt without a reason and a recovery plan.**
- dimOS install onto the Ascent was **in progress, not confirmed complete** as of the last check (installing into a plain venv, deviated slightly from the original conda-env plan). Verify live before assuming the panel can already run there.
- `tailscaled` is running on the box, unexplained in the original plan — since the demo's core beat is "unplug the internet, it still works," confirm Tailscale is stopped before that moment (fine to leave running for ordinary dev work otherwise).
- Networking for the venue floor is still unresolved: venue wifi causes real iPad-roaming instability; plan is either the Ascent's own wifi in AP mode (untested, driver maturity unknown) or its 10G Ethernet into a cheap travel router (fallback, more likely to just work).

### Voice/NLP orchestration layer — 0% built
Nothing connects "a voice command was understood" to "a robot does something," for either demo. This is the actual differentiator in both concepts and is currently the single biggest unbuilt piece regardless of which demo wins.

### Rokid glasses — minimally scoped
Mic-input-only is the current plan. Demo 2 additionally wants a data stream *to* the glasses (basic telemetry/status display) — that display-integration path was explicitly deprioritized earlier and would need to be revisited if demo 2 is chosen.

---

## 4. Standing rules other agents should know

- **Galaxea A1Z arm**: default to write-only (draft code, don't execute) unless the user is physically present and has explicitly granted live-execution authorization for that session — the arm has no e-stop/brakes and this has been an explicit, repeatedly-stated boundary. Once the user signals they're actively supervising with a kill switch in hand, that authorization has consistently extended to real hardware iteration without needing to re-ask every time.
- **Go2**: much more hands-on/autonomous working relationship already established — running blueprints, live teleop, gesture commands, etc. with the user's real-time authorization has been the normal mode, not an exception.
- **No vision-model narration/commentary features** (live camera-feed narration, spoken trip summaries) — explicitly rejected earlier as low-value AI theater. The one exception carved out: using a raw vLLM endpoint as a plain local LLM to parse a voice transcript into structured intent (e.g. `{"drink": "cola"}`) is still considered useful.
- Infra docs (`ascent-gx10-infra`, `ARM_BRAINSTORM_CONTEXT.md`, `ASCENT_BRAINSTORM_CONTEXT.md`) are explicitly snapshots, not live feeds — verify IPs/process state/install status before acting on anything time-sensitive.
- All pip installs into the `dimos` conda env should go through `-c /home/alex/dev/AdventureX/constraints.txt` to avoid multi-GB dependency-backtracking loops.

---

## 5. Open decision

Demo 1 vs. Demo 2 (or some hybrid/staged version) — unresolved as of this doc. Core tension: demo 1 is nearly-scoped and its remaining gaps (ArUco perception on the arm, voice→orchestration glue) are concrete and bounded; demo 2 is more compelling and more aligned with what the dimOS team says they actually want, but chains five unbuilt subsystems together with under 24h left. See chat for live discussion/recommendation.
