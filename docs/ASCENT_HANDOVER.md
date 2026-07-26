# Ascent GX10 — Handover

**For:** whoever's picking up work on the Ascent next.
**Machine:** `spark-7f06`, IP `10.76.4.120` (venue Wi-Fi, DHCP — may change; re-check with `ip a` if SSH stops connecting), aarch64, NVIDIA GB10 Grace Blackwell, Ubuntu 24.04.4 LTS.
**As of writing:** dimOS is installed and verified working; the Go2 control panel is copied over and proven to launch, but currently **stopped** (nothing is running on port 8090 right now); no robot has been connected from this machine yet.

---

## 1. Getting in

```bash
ssh asus@10.76.4.120
```

Password: `Asus123456` (also SSH-key auth is set up from the main dev laptop, so if you're on that machine specifically you shouldn't even be prompted).

`sudo` needs the same password (`Asus123456`) — you'll need it for anything system-level (the LCM prerequisites below, apt installs).

## 2. Where everything is

| Path | What it is |
|---|---|
| `~/dimos-env/` | dimOS's own Python environment (a plain `venv`, **not conda** — see §5 for why). Binary: `~/dimos-env/bin/dimos`. Python: `~/dimos-env/bin/python3`. |
| `~/dimos-pwa/` | The Go2 control panel project (copied from the dev laptop's `dimos-pwa` repo via `rsync`). Its own separate venv at `~/dimos-pwa/venv/` (FastAPI/uvicorn — this one never imports `dimos` directly, only shells out to it, same architecture as the laptop). |
| `~/dimos-pwa-deploy/` | Just install scratch: `constraints.txt` (pins `torch==2.11.0`/`torchvision==0.26.0`) and the pip install logs, kept for reference if you ever need to reinstall/debug. |
| `/home/alex/dev/AdventureX/ASCENT_BRAINSTORM_CONTEXT.md` (on the **dev laptop**, not the Ascent) | Bigger-picture project context — read this if you need to understand *why*, not just *how*. |

## 3. Launching the control panel

```bash
cd ~/dimos-pwa
export DIMOS_BIN=/home/asus/dimos-env/bin/dimos
export DIMOS_PY=/home/asus/dimos-env/bin/python3
./venv/bin/python -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8090
```

Then open `http://10.76.4.120:8090` from any device on the same Wi-Fi (laptop, iPad, phone).

**Why the two `export` lines are needed**: `dimos_cli.py` defaults to the *dev laptop's* conda paths (`/home/alex/miniconda3/envs/dimos/...`). Those two env vars override it to point at this machine's install instead. If you want to stop having to type them every time, edit the defaults directly in `~/dimos-pwa/backend/dimos_cli.py` (search for `DIMOS_BIN =` and `DIMOS_PY =`) — but don't push that change back to the shared `dog-pwa` repo, since it'd break the laptop's own paths for everyone else.

To run it detached (survives you closing the SSH session):
```bash
nohup env DIMOS_BIN=/home/asus/dimos-env/bin/dimos DIMOS_PY=/home/asus/dimos-env/bin/python3 \
  ./venv/bin/python -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8090 \
  > /tmp/ascent-panel.log 2>&1 &
disown
```
Check it's up: `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/`
Stop it: find the PID (`ps aux | grep uvicorn`) and `kill <pid>`.

## 4. Before connecting a real robot — one step not done yet

The LCM transport (what dimOS uses to talk to the Go2) needs some host-level networking prerequisites that **have not been applied on this machine yet**:

```bash
cd ~/dimos-pwa
sudo ./setup.sh
```

This is idempotent and safe to re-run — it checks each thing before changing it (loopback multicast, a multicast route, socket buffer sizes) and only prompts for sudo if something's actually missing. Do this once before the first real robot connection attempt from here.

## 5. Things worth knowing before you touch anything

- **This machine already runs someone else's live work — don't disrupt it.** Three Docker containers belonging to teammate **Helios** (`joyai-gx10-vllm-main-1`, `joyai-gx10-vllm-summary-1`, `joyai-gx10-adapter-1`, ports 7060/8065/8070) are a real local voice/vision AI stack, almost certainly the actual "brain" for this project's demo. They're resilient (`docker restart: unless-stopped`, survived a full reboot fine tonight), but don't kill them casually. Check `sudo docker ps` if you want to confirm they're still healthy.
- **Automatic OS updates are intentionally disabled** (`apt-daily.timer`/`apt-daily-upgrade.timer` are masked) for the rest of the event, so the system won't change under you unexpectedly. Don't unmask them until after the demo.
- **Why a plain `venv` instead of conda**: Miniconda's own installer download was painfully slow from this venue's network (~15KB/s). A plain `python3 -m venv` needs no big download at all since the system already has the exact right Python version (3.12.3), and works identically for our purposes.
- **If a pip install is ever slow**: don't use plain PyPI or anaconda.com from this network — use the pre-configured domestic mirror instead:
  ```bash
  export PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
  export PIP_TRUSTED_HOST=mirrors.aliyun.com
  ```
  (was ~3x faster than direct PyPI, ~50x faster than anaconda.com, when tested tonight). More mirror options in `/home/alex/dev/AdventureX/pip-mirror.conf` on the dev laptop.
- **No robot has been connected from this machine yet.** Everything above proves the software stack itself works (verified: `torch.cuda.is_available() == True`, `dimos list` shows all blueprints, the panel serves and responds correctly over the LAN) — actually driving the Go2 or the A1Z arm from here is the next real test, not something already done.

## 6. Quick sanity checks

```bash
# GPU / driver healthy?
nvidia-smi

# dimOS itself working?
~/dimos-env/bin/dimos list

# Helios's containers still healthy?
sudo docker ps

# is the panel currently running?
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/
```

If anything here looks wrong or you're not sure what state the machine is in, check `/home/alex/dev/AdventureX/ASCENT_BRAINSTORM_CONTEXT.md` on the dev laptop for the fuller story, or ask Alex.
