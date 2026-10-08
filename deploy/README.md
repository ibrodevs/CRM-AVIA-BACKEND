# Production deploy — REG.RU VPS

Target: Ubuntu 26.04 LTS, 2 vCPU, 4 GB RAM, 60 GB NVMe.

The production layout is:

```text
Internet -> Nginx :80/:443 -> 127.0.0.1:8000 -> Django/Gunicorn
                                         |
                                         +-> PostgreSQL (Docker internal only)
                                         +-> jobs worker
                                         +-> scheduler
```

PostgreSQL is never published to the public network. Django is bound only to
`127.0.0.1`, so all public traffic must pass through Nginx.

## 1. Server packages

Install Docker Engine + Compose plugin, Nginx and Certbot. Keep UFW open only for
SSH, HTTP and HTTPS.

Recommended deployment directory:

```bash
/opt/travelhub-backend
```

## 2. Clone the project

```bash
sudo mkdir -p /opt/travelhub-backend
sudo chown "$USER":"$USER" /opt/travelhub-backend
git clone https://github.com/ibrodevs/CRM-AVIA-BACKEND.git /opt/travelhub-backend
cd /opt/travelhub-backend
```

For a private repository use a read-only deploy key instead of a personal token.

## 3. Production environment

```bash
cp deploy/.env.production.example .env
chmod 600 .env
```

Generate secrets locally on the server:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(64))'
openssl rand -hex 24
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

If `cryptography` is not installed on the host, generate `FIELD_ENCRYPTION_KEY`
inside the app image after the first build or generate it on a trusted local
machine. Never commit any generated secret.

Fill at minimum:

- `DJANGO_SECRET_KEY`
- `FIELD_ENCRYPTION_KEY`
- `POSTGRES_PASSWORD`
- the same password inside `DATABASE_URL`
- `DJANGO_ALLOWED_HOSTS`
- `CSRF_TRUSTED_ORIGINS`
- `CORS_ALLOWED_ORIGINS`

Use a hexadecimal PostgreSQL password (`openssl rand -hex 24`) so it is safe in
`DATABASE_URL` without URL encoding.

For production keep:

```text
CORS_ALLOW_ALL_ORIGINS=False
ALLOW_MOCK_ADAPTER=False
SECURE_SSL_REDIRECT=True
```

## 4. First deploy

```bash
chmod +x deploy/deploy.sh deploy/backup.sh
./deploy/deploy.sh
```

The script validates Compose, builds the image, waits for PostgreSQL, runs Django
migrations, collects static files, starts web/jobs/scheduler and checks
`/health/live/`.

Useful checks:

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f --tail=100 web
curl -fsS http://127.0.0.1:8000/health/live/
curl -fsS http://127.0.0.1:8000/health/ready/
```

## 5. Nginx

Copy the template and replace `api.example.com` with the real backend domain:

```bash
sudo cp deploy/nginx/travelhub.conf.example /etc/nginx/sites-available/travelhub
sudo nano /etc/nginx/sites-available/travelhub
sudo ln -s /etc/nginx/sites-available/travelhub /etc/nginx/sites-enabled/travelhub
sudo nginx -t
sudo systemctl reload nginx
```

If the repository is not located at `/opt/travelhub-backend`, update the
`/static/` and `/media/` alias paths in the Nginx config.

## 6. HTTPS

After the DNS A record points to this server and HTTP works:

```bash
sudo certbot --nginx -d api.example.com
sudo certbot renew --dry-run
```

Do not enable HSTS manually in Nginx until the HTTPS hostname is confirmed;
Django production settings already emit HSTS after requests reach the app over
HTTPS.

## 7. Database backup

Manual backup:

```bash
./deploy/backup.sh
```

Backups are stored in `.runtime/backups/` and local files older than 14 days are
removed. This is additional to the REG.RU infrastructure backup and should not
be the only off-server copy.

Example daily cron at 03:20:

```cron
20 3 * * * cd /opt/travelhub-backend && ./deploy/backup.sh >> /var/log/travelhub-backup.log 2>&1
```

## 8. Updating production

```bash
cd /opt/travelhub-backend
git pull --ff-only origin main
./deploy/deploy.sh
```

The PostgreSQL data and uploaded media are kept under `.runtime/` and are not
removed during rebuilds.

## 9. Rollback rule

Do not run `docker compose down -v` in production: `-v` removes persistent
volumes in Compose configurations that use named volumes. This production file
uses bind-mounted `.runtime/` data, but destructive cleanup commands should
still be avoided.

Before a risky schema release, create a database backup and record the current
Git commit:

```bash
./deploy/backup.sh
git rev-parse HEAD
```
