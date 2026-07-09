# 福建气象系统

一个可部署的福建气象 Web 应用，包含天气实况、自动气象站 GIS 地图、雷达云图和综合云图。

## 功能

- 天气实况：当前站点天气、未来逐小时、过去 24 小时趋势图。
- 天气地图：Leaflet GIS 地图，按自动气象站点位显示气温、降雨量、湿度、气压、风速风向。
- 气象云图：雷达拼图、单站雷达、相控阵雷达、综合云图。
- 支持站点搜索、GPS 最近站推荐、底图切换、城市多选加载。
- Home Assistant 可通过固定图片地址接入厦门相控阵雷达最新图。

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

## Home Assistant 显示厦门相控阵雷达

服务提供固定图片地址：

```text
http://<fjqx服务器IP>:8765/radar-latest.jpg?station_id=20002&count=6
```

其中 `20002` 是厦门相控阵雷达。

在 Home Assistant 的 `configuration.yaml` 添加：

```yaml
camera:
  - platform: generic
    name: 厦门相控阵雷达
    still_image_url: "http://<fjqx服务器IP>:8765/radar-latest.jpg?station_id=20002&count=6"
    content_type: image/jpeg
    verify_ssl: false
```

重启 Home Assistant 后，可以在 Lovelace 添加 Picture Entity 卡片：

```yaml
type: picture-entity
entity: camera.xia_men_xiang_kong_zhen_lei_da
name: 厦门相控阵雷达
show_state: false
show_name: true
```

如果 Home Assistant 与 fjqx 在同一台机器上，地址可写：

```text
http://127.0.0.1:8765/radar-latest.jpg?station_id=20002&count=6
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

## 台湾 CWA 自动气象站

气象地图支持叠加台湾中央氣象署 CWA 自动气象站资料。该接口需要开放资料授权码：

1. 打开 https://opendata.cwa.gov.tw/
2. 注册并登录气象会员。
3. 进入“API 授权码”，取得授权码。
4. 启动服务前设置环境变量 `CWA_API_KEY`。

PowerShell：

```powershell
$env:CWA_API_KEY="你的CWA授权码"
.\.venv\Scripts\python.exe .\radar_weather_app\radar_live_server.py --host 0.0.0.0 --port 8765
```

Linux/systemd 可在服务里加入：

```ini
Environment=CWA_API_KEY=你的CWA授权码
```

未配置 `CWA_API_KEY` 时，福建数据仍会正常显示，台湾自动站不会加载。
