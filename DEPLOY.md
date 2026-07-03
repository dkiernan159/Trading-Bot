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

## Updating the bot later

```bash
ssh root@<server-ip>
sudo -u tradingbot git -C /home/tradingbot/trading-bot pull origin claude/topstepx-trading-bot-wqy0at
sudo -u tradingbot /home/tradingbot/trading-bot/.venv/bin/pip install -r /home/tradingbot/trading-bot/requirements.txt
sudo systemctl restart trading-bot
```

## Stopping it

```bash
sudo systemctl stop trading-bot     # stop
sudo systemctl disable trading-bot  # also don't start on next boot
```
