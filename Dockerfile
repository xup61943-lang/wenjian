FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DEBIAN_FRONTEND=noninteractive

# LibreOffice 负责 Word → PDF，fonts-noto-cjk 保证中文不丢字
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

RUN useradd -m app && mkdir -p /app/files && chown -R app /app

# LibreOffice 需要一个可写的 HOME 来生成用户配置
ENV HOME=/home/app
USER app

EXPOSE 8080

# 任务状态保存在进程内存里：必须单 worker、多线程；云托管最大实例数也必须是 1
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8080", "-t", "180", "server:app"]
