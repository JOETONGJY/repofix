# RepoFix M2 预构建执行环境（design/05：依赖准备采用预构建环境，Agent 不联网装依赖）
# 用途：受限容器镜像——python 3.12 + ripgrep + git；非 root 用户 repofix (uid/gid 1000)
FROM python:3.12-slim

# 网络环境需要国内 Debian 源（deb.debian.org 不可靠）
RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ripgrep git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -i https://pypi.org/simple pytest \
    && groupadd -g 1000 repofix \
    && useradd -m -u 1000 -g 1000 -s /bin/bash repofix

USER repofix
WORKDIR /workspace

# 冒烟自检：镜像构建即验证核心依赖
RUN python3 --version && rg --version | head -1 && git --version
