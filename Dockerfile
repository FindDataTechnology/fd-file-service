# fd-file-service 生产镜像（OpenSpec fd-file-service 任务 7.1）。
#
# 构建不在本机/节点做：经私有构建池出片（fd-build-snapshot），本机禁 docker build。
#   fd-build-snapshot ~/finddata/fd-file-service -R <owner>/build-pool \
#     --namespace yizuo --image fd-file-service
#
# 设计要点：
#  - 单阶段即可：纯 Python 包，无前端构建。slim 底 + 非 root 运行。
#  - **不装 PDF 抽取后端**：服务 HTTP 面不抽取（抽取在 worker 侧，见下）；
#    服务侧连 PDF 库都不需要，镜像更小。
#  - worker 与 service **同一镜像、不同入口**：抽取后端（pymupdf/pdfminer）
#    装在同一个镜像里，用 `python scripts/parse_worker.py` 跑。
#    为什么不用两镜像：v1 单实例、同机部署，两镜像只会让版本漂移更易发生；
#    共享同一个锁文件才是"同一套代码"的最小保证。
#  - 元数据 SQLite 与服务状态同在 /data 卷（compose 挂载）。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖先装（层缓存）：先拷元数据再拷源码。
COPY pyproject.toml README.md ./
COPY fd_file_service/ ./fd_file_service/
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir -e . \
 && pip install --no-cache-dir pdfminer.six \
 && useradd --create-home --uid 10001 appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /app /data

USER appuser

# 元数据库落在卷上（compose 挂 /data）
ENV FILE_DATABASE_URL=sqlite:////data/fd_file_service.db

EXPOSE 8731

# 默认入口 = HTTP 服务；worker 用 command 覆盖（见 compose.yml）
CMD ["uvicorn", "fd_file_service.app:app", "--host", "0.0.0.0", "--port", "8731"]
