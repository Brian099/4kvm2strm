# 4kvm 采集/播放/导出服务
#
# 站点取流地址由前端 WASM（Rust）做时间签名，必须用 Node 跑它，
# 所以镜像里同时带 Python 和 Node。

FROM node:20-slim AS nodebin

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# 只要 node 二进制，不装整包
COPY --from=nodebin /usr/local/bin/node /usr/local/bin/node

WORKDIR /app

COPY src/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./

# 构建期自检：模块能导入 + node 能加载站点 wasm
RUN python -c "import config, db, settings, strm, maccms, crawl, app; print('python modules ok')" \
 && node --input-type=module -e "import('./wasm/nbmovie_wasm.js').then(()=>console.log('wasm glue ok'))"

VOLUME ["/app/data", "/app/strm"]
EXPOSE 8900

CMD ["python", "app.py"]
