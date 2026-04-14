# 中国新能源大六座车型网站（2025/2026）

基于 Flask 的简洁网站，包含：

- 2025、2026 新能源大六座 SUV / MPV 列表
- 品牌与车型参数（价格、续航、电池、尺寸、驱动等）
- 每个车型详情页附带懂车帝测评链接

## 1. 安装依赖

```bash
pip install -r requirements.txt
```

## 2. 运行项目

```bash
python app.py
```

浏览器打开：`http://127.0.0.1:5000/`

## 3. 部署到 Render（异地可访问）

1. 把项目推送到 GitHub 仓库
2. 登录 [Render](https://render.com/) -> `New +` -> `Web Service`
3. 选择你的 GitHub 仓库并创建服务
4. 关键配置填写：
   - Runtime: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
5. 点击 `Create Web Service`，等待部署完成
6. Render 会给你一个公网地址（`https://xxx.onrender.com`），发给家人即可直接访问

## 4. 数据维护

- 车型数据在 `data.py` 的 `VEHICLES` 列表中
- 新增车型时保证 `id` 唯一
- `video_link` 建议填懂车帝具体测评视频页；当前默认使用搜索链接，方便后续替换为官方或媒体实测视频链接

