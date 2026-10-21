FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    LINGSHU_PUBLIC_MODE=1 LINGSHU_HOST=0.0.0.0 LINGSHU_PORT=8002 \
    LINGSHU_DATA_DIR=/app/web_data TZ=Asia/Shanghai
WORKDIR /app
COPY requirements.web.txt ./
RUN pip install --no-cache-dir -r requirements.web.txt \
    && useradd --create-home robo \
    && mkdir -p /app/web_data && chown robo:robo /app/web_data
COPY api_server.py vita_crew.py skills.py robo_actions.py robot_bridge.py start_web.py index.html ./
COPY assets ./assets
COPY live2d ./live2d
USER robo
EXPOSE 8002
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT',os.getenv('LINGSHU_PORT','8002'))+'/health',timeout=3)"
CMD ["python", "start_web.py"]
