# Deploying to a Hetzner VPS

The bot needs to run continuously, so it belongs on a small always-on
server, not your laptop. These steps assume Hetzner Cloud (cheapest
reliable option for this workload -- see chat for why), but `deploy/setup.sh`
is plain Ubuntu and works on any provider.

## 1. Create the server

1. Sign up at https://console.hetzner.cloud/.
2. Create a new server:
   - **Location**: Ashburn, VA or Hillsboro, OR (US regions -- lower latency
     to TopstepX's API, which is US-hosted).
   - **Image**: Ubuntu 24.04.
   - **Type**: CX22 (2 vCPU / 4GB RAM / 40GB NVMe, ~$3.79/mo) -- this bot is
     lightweight, you don't need more.
   - **SSH key**: add your public key during creation (don't use a
     password-only root login).
3. Note the server's public IP once it's created.

## 2. Provision it

SSH in as root and run the setup script:

```bash
ssh root@<server-ip>
curl -fsSL https://raw.githubusercontent.com/dkiernan159/trading-bot/claude/topstepx-trading-bot-wqy0at/deploy/setup.sh -o setup.sh
bash setup.sh
```

This installs Python/git/ufw, creates a dedicated non-root `tradingbot`
user to actually run the bot, clones the repo, builds the virtualenv,
installs the systemd service (`trading-bot`, not started yet) and log
rotation, and locks the firewall down to SSH-only (the bot only makes
outbound HTTPS/WSS connections to TopstepX -- it needs no inbound ports).

## 3. Configure credentials

```bash
nano /home/tradingbot/trading-bot/.env
```

Fill in `PROJECTX_USERNAME`, `PROJECTX_API_KEY`, `PROJECTX_ACCOUNT_ID`.
The file is already `chmod 600` and owned by `tradingbot`.

## 4. Dry-run first

`config.yaml: broker.dry_run` defaults to `true`. Run the bot in the
foreground and watch it log the orders it *would* place, without sending
anything real:

```bash
sudo -u tradingbot bash -c 'cd /home/tradingbot/trading-bot && source .venv/bin/activate && python -m src.runner'
```

Let it run through a morning session, compare the logged entries/stops/
targets against what you'd expect from the strategy, then Ctrl-C.

## 5. Go live

Once you've reviewed STRATEGY.md's "still unverified" list against a
paper/sim account (if available) and you're comfortable, flip
`broker.dry_run: false` in `config.yaml`, then:

```bash
sudo systemctl start trading-bot
sudo systemctl status trading-bot
```

`Restart=on-failure` means systemd auto-restarts it if it crashes; it also
comes back up automatically on server reboot (`systemctl enable` already
ran during setup).

## 6. Watch it

```bash
journalctl -u trading-bot -f          # live systemd log
tail -f /home/tradingbot/trading-bot/logs/bot.log
tail -f /home/tradingbot/trading-bot/trades/trades.csv
```

Logs rotate weekly (8 weeks kept) via `/etc/logrotate.d/trading-bot`.

## 7. View the dashboard

The dashboard shows live trades (auto-refreshing every ~10s from
`trades/trades.csv`, free -- no API calls) plus a backtest section
(candlestick charts, refreshed every `dashboard.refresh_interval_seconds`
in `config.yaml`, default 30 min, since that part costs real API calls).

Start it:

```bash
sudo systemctl start trading-dashboard
```

It binds to `127.0.0.1` only -- it is **not reachable from the internet**,
by design. View it by tunneling it to your own machine over SSH. In a
PowerShell window on your computer (leave this window open while you're
watching the dashboard):

```powershell
ssh -i $HOME\.ssh\hetzner_trading_bot -L 8080:127.0.0.1:8080 root@<server-ip>
```

Then open **http://localhost:8080** in your browser. Closing that SSH
window closes access to the dashboard -- nothing is exposed when you're not
actively tunneled in.

## Convenience commands

`setup.sh` installs a few shortcuts to `/usr/local/bin` so you don't have to
retype the long-form commands (and can't hit the "dubious ownership" git
error that comes from running git as root instead of `tradingbot`):

| Command | What it does |
|---|---|
| `backtest [--days N] [--verbose] [--chart-html PATH]` | Runs a backtest as the `tradingbot` user with `.env` loaded |
| `bot-pull` | `git pull` + reinstall dependencies + restart both services |
| `bot-status` | `systemctl status` for both services |
| `bot-logs` | Tails both services' logs live (`Ctrl-C` to stop) |

## Updating the bot later

```bash
ssh root@<server-ip>
bot-pull
```

(Same as: `sudo -u tradingbot git -C ... pull`, reinstall requirements, and
`systemctl restart trading-bot trading-dashboard` -- see `deploy/bin/bot-pull`.)

## Stopping it

```bash
sudo systemctl stop trading-bot           trading-dashboard     # stop both
sudo systemctl disable trading-bot        trading-dashboard     # also don't start on next boot
```
