# 通过 SSH 部署 fjqx

以下假设 GitHub 仓库地址为：

```text
https://github.com/<your-github-user>/fjqx.git
```

把 `<your-github-user>` 替换成你的 GitHub 用户名。

## 1. 登录服务器

```bash
ssh root@your-server-ip
```

## 2. 安装基础环境

Debian / Ubuntu：

```bash
apt update
apt install -y git python3 python3-venv python3-pip nginx
```

CentOS / Rocky / AlmaLinux：

```bash
yum install -y git python3 python3-pip nginx
```

## 3. 拉取代码

```bash
mkdir -p /opt
cd /opt
git clone https://github.com/<your-github-user>/fjqx.git
cd fjqx
```

## 4. 创建 Python 环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 5. 试运行

```bash
HOST=0.0.0.0 PORT=8765 python radar_live_server.py
```

浏览器访问：

```text
http://your-server-ip:8765/
```

确认健康检查：

```bash
curl http://127.0.0.1:8765/healthz
```

## 6. systemd 常驻运行

创建服务文件：

```bash
cat >/etc/systemd/system/fjqx.service <<'EOF'
[Unit]
Description=Fujian Meteorology System
After=network.target

[Service]
WorkingDirectory=/opt/fjqx
Environment=HOST=0.0.0.0
Environment=PORT=8765
ExecStart=/opt/fjqx/.venv/bin/python /opt/fjqx/radar_live_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

启动：

```bash
systemctl daemon-reload
systemctl enable --now fjqx
systemctl status fjqx
```

查看日志：

```bash
journalctl -u fjqx -f
```

## 7. Nginx 反向代理

```bash
cat >/etc/nginx/conf.d/fjqx.conf <<'EOF'
server {
    listen 80;
    server_name your-domain.example;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
```

检查并重载：

```bash
nginx -t
systemctl reload nginx
```

如果没有域名，把 `server_name your-domain.example;` 改成 `_`。

## 8. 更新部署

以后更新代码：

```bash
ssh root@your-server-ip
cd /opt/fjqx
git pull
source .venv/bin/activate
pip install -r requirements.txt
systemctl restart fjqx
```

