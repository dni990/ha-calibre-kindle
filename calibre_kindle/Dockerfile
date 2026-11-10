# Debian-Calibre (6.x) ist mit DeDRM 10.0.3 kompatibel und läuft auf aarch64.
FROM debian:bookworm-slim

ARG DEDRM_VERSION=10.0.3
ARG DEACSM_VERSION=0.0.16

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    QT_QPA_PLATFORM=offscreen \
    CALIBRE_CONFIG_DIRECTORY=/data/calibre-config \
    LANG=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      calibre python3-flask python3-requests python3-lxml python3-pycryptodome \
      ca-certificates curl unzip zip \
 && rm -rf /var/lib/apt/lists/*

COPY patch_deacsm.py /tmp/patch_deacsm.py

RUN mkdir -p /opt/plugins /tmp/dl && cd /tmp/dl \
 && curl -fsSL -o dedrm.zip "https://github.com/noDRM/DeDRM_tools/releases/download/v${DEDRM_VERSION}/DeDRM_tools_${DEDRM_VERSION}.zip" \
 && unzip -q dedrm.zip DeDRM_plugin.zip && mv DeDRM_plugin.zip /opt/plugins/DeDRM_plugin.zip \
 && curl -fsSL -o /opt/plugins/DeACSM.zip "https://github.com/Leseratte10/acsm-calibre-plugin/releases/download/v${DEACSM_VERSION}/DeACSM_${DEACSM_VERSION}.zip" \
 && python3 /tmp/patch_deacsm.py /opt/plugins/DeACSM.zip \
 && cd / && rm -rf /tmp/dl /tmp/patch_deacsm.py

COPY app/ /app/
COPY run.sh /run.sh

CMD ["sh", "/run.sh"]
