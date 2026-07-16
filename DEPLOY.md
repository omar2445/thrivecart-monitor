# Self-hosting guide (VPS)

Runs the ThriveCart monitor on your own server with Docker:
FastAPI app + PostgreSQL + Caddy (automatic free HTTPS).

## 1. Get a server and a domain

- VPS: OVH (Beauharnois, Québec — data stays in Canada), DigitalOcean/Vultr (Toronto), etc.
  Smallest plan is enough (1 vCPU / 1 GB RAM). Choose **Ubuntu 24.04**.
- Domain: any registrar (~12 $/yr). Create an **A record** pointing to the server's IP,
  e.g. `moniteur.mondomaine.com → 51.222.x.x`.

## 2. Install Docker on the server

SSH into the server, then:

```bash
curl -fsSL https://get.docker.com | sh
```

## 3. Get the code and configure

```bash
git clone https://github.com/omar2445/thrivecart-monitor.git
cd thrivecart-monitor
nano .env
```

Put this in `.env` (same values currently in Railway → Variables):

```env
# Domain (must already point to this server)
APP_DOMAIN=moniteur.mondomaine.com
APP_URL=https://moniteur.mondomaine.com

# Database password (invent a long one)
DB_PASSWORD=change-me-to-something-long

# ThriveCart
THRIVECART_API_KEY=...

# Email (Brevo)
BREVO_API_KEY=...
SMTP_FROM=...
NOTIFY_EMAIL=email1@gmail.com, email2@gmail.com
NOTIFY_NAME=Admin
```

## 4. Start everything

```bash
docker compose up -d --build
```

That's it. Caddy obtains the HTTPS certificate automatically.
Check: `https://moniteur.mondomaine.com/health` → `{"status":"ok"}`

## 5. Point ThriveCart at the new server

ThriveCart → Settings → API & Webhooks → edit the webhook URL:
`https://moniteur.mondomaine.com/webhook/thrivecart`

## 6. Import the data

Open the dashboard and click **⟳ Synchroniser depuis ThriveCart**
(full import takes ~10 minutes), or visit `/sync-thrivecart`.

## 7. Backups (recommended)

Nightly database dump kept 14 days — add to crontab (`crontab -e`):

```cron
0 3 * * * docker compose -f /root/thrivecart-monitor/docker-compose.yml exec -T db pg_dump -U monitor monitor | gzip > /root/backups/monitor-$(date +\%F).sql.gz && find /root/backups -mtime +14 -delete
```

(`mkdir -p /root/backups` first.)

## Updating the app later

```bash
cd thrivecart-monitor
git pull
docker compose up -d --build
```

## Notes

- The weekly (Monday 9:00 UTC) and monthly (1st, 9:00 UTC) report emails run
  inside the app — nothing else to configure.
- Data lives in the Docker volume `pgdata`; it survives restarts and updates.
- Once the new server is confirmed working, delete the Railway project to stop billing.
