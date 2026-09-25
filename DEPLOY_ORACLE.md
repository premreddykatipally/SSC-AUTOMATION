# Deploy SSC Automation on an Oracle Cloud VM

This guide uses an Ubuntu **Always Free eligible** VM, one Gunicorn worker, Nginx, and HTTPS. The SQLite ledger and portal settings remain on the VM's persistent boot volume. The live database and `config.json` are deliberately not in GitHub.

## 1. Create the VM and DNS name

1. In Oracle Cloud, create an Ubuntu VM in your home region with an **Always Free eligible** shape. Ampere A1 Flex with 2 OCPUs and 12 GB RAM is a suitable choice when capacity is available. Download the SSH private key and keep it private.
2. Put the VM in a public subnet with a public IPv4 address. In the VCN security list or network security group, allow inbound TCP **22** from your own IP and **80/443** from the internet. Do **not** open 5000 or 8000.
3. Point an `A` DNS record for your chosen hostname (for example `billing.example.com`) to the VM's public IP. Wait for DNS to resolve before requesting HTTPS.
4. Connect from your computer:

   ```bash
   ssh -i /path/to/private_key ubuntu@VM_PUBLIC_IP
   ```

## 2. Install the app

Run these commands on the VM:

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip nginx sqlite3 snapd
git clone https://github.com/premreddykatipally/SSC-AUTOMATION.git
cd SSC-AUTOMATION
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
```

The example config starts in simulation mode with blank portal credentials. Enter the real portal credentials only after HTTPS and the dashboard password are working. **Do not copy the local Windows `config.json` or customer database through GitHub.** To migrate existing customer/payment records, transfer the SQLite file privately to the VM and set its owner to `ubuntu` and permissions to `600`.

## 3. Protect the dashboard

Generate two independent random values with `openssl rand -hex 32`. Store them in a root-owned environment file:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/ssc-automation.env
sudo nano /etc/ssc-automation.env
```

Put these two lines in that file, replacing both placeholders with different random values:

```text
SSC_APP_PASSWORD=PASTE_FIRST_RANDOM_VALUE
SSC_SECRET_KEY=PASTE_SECOND_RANDOM_VALUE
```

The browser will prompt for the dashboard password. Do not put it in GitHub, a URL, or a public chat.

## 4. Run the app as a service

Create `/etc/systemd/system/ssc-automation.service` with `sudo nano`:

```ini
[Unit]
Description=SSC Automation dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/SSC-AUTOMATION
EnvironmentFile=/etc/ssc-automation.env
ExecStart=/home/ubuntu/SSC-AUTOMATION/.venv/bin/gunicorn --workers 1 --threads 4 --timeout 120 --bind 127.0.0.1:8000 app:app
Restart=always
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
```

Use **one worker** because the Ezybilling session and recharge preview tokens are kept in process memory. Then start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ssc-automation
sudo systemctl status ssc-automation
```

## 5. Put Nginx and HTTPS in front

Create `/etc/nginx/sites-available/ssc-automation` with `sudo nano`. Replace `billing.example.com` with your DNS name:

```nginx
server {
    listen 80;
    server_name billing.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/ssc-automation /etc/nginx/sites-enabled/ssc-automation
sudo nginx -t
sudo systemctl reload nginx
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo snap install --classic certbot
sudo ln -s /snap/bin/certbot /usr/local/bin/certbot
sudo certbot --nginx -d billing.example.com
sudo certbot renew --dry-run
```

If UFW is already configured, review its rules before enabling it. Oracle's VCN ingress rules and the VM firewall must both allow 80/443. Confirm `https://billing.example.com/` prompts for the dashboard password and shows the app. Do not test the final recharge button with a real customer while checking deployment.

## 6. Back up and update

The SQLite database and `config.json` survive normal VM reboots and `git pull`, but a VM or disk failure can still lose them. Make encrypted backups outside the VM, and test restoring them. For a consistent SQLite snapshot on the VM:

```bash
mkdir -p ~/ssc-backups
sqlite3 ~/SSC-AUTOMATION/ssc_billing.db ".backup '$HOME/ssc-backups/ssc_billing.db'"
cp ~/SSC-AUTOMATION/config.json ~/ssc-backups/config.json
chmod 600 ~/ssc-backups/*
```

Keep those backups private: they contain customer data and portal credentials. To update the app:

```bash
cd ~/SSC-AUTOMATION
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart ssc-automation
sudo systemctl status ssc-automation
```

If the service fails, read `sudo journalctl -u ssc-automation -n 100 --no-pager` and `sudo nginx -t`. Never expose Gunicorn directly to the internet.
