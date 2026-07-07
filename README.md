# 福建气象系统

一个可部署的福建气象 Web 应用，包含天气实况、自动气象站 GIS 地图、雷达云图和综合云图。

## 功能

- 天气实况：当前站点天气、未来逐小时、过去 24 小时趋势图。
- 天气地图：Leaflet GIS 地图，按自动气象站点位显示气温、降雨量、湿度、气压、风速风向。
- 气象云图：雷达拼图、单站雷达、相控阵雷达、综合云图。
- 支持站点搜索、GPS 最近站推荐、底图切换、城市多选加载。

## 本地启动

```powershell
cd U:\Users\Enlink\Downloads
.\.venv\Scripts\python.exe .\radar_weather_app\radar_live_server.py --host 0.0.0.0 --port 8765
```

访问：

```text
http://127.0.0.1:8765/
```

健康检查：

```text
http://127.0.0.1:8765/healthz
```

## Linux 服务器部署

```bash
cd /opt
python3 -m venv fujian-met-venv
source fujian-met-venv/bin/activate

cd /opt/fujian-meteorology-system
pip install -r requirements.txt
HOST=0.0.0.0 PORT=8765 python radar_live_server.py
```

建议用 systemd 托管：

```ini
[Unit]
Description=Fujian Meteorology System
After=network.target

[Service]
WorkingDirectory=/opt/fujian-meteorology-system
Environment=HOST=0.0.0.0
Environment=PORT=8765
ExecStart=/opt/fujian-met-venv/bin/python /opt/fujian-meteorology-system/radar_live_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## Docker 部署

```bash
cd radar_weather_app
docker build -t fujian-meteorology-system .
docker run -d --name fujian-met -p 8765:8765 fujian-meteorology-system
```

## Nginx 反向代理

```nginx
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
```

## 数据源说明

应用会请求福建气象相关接口和图像资源，服务器需要能访问：

- `www.fjqxfw.cn:8096`
- `www.fjqxfw.cn:8099`
- 前端 CDN：Leaflet、ECharts

运行时缓存写入 `weather_runtime_cache.json`。Docker 镜像默认不打包该缓存文件。
