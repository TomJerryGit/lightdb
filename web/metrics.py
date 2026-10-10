"""Prometheus 指标端点"""
from fastapi import APIRouter, Response
from lightdb_studio.core.metrics import get_registry


router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus 抓取端点"""
    registry = get_registry()
    text = registry.export_prometheus()
    return Response(content=text, media_type="text/plain; version=0.0.4")


@router.get("/api/v1/metrics/json")
async def json_metrics():
    """JSON 格式指标（便于调试）"""
    registry = get_registry()
    return {
        'counters': {
            f"{k[0]}{{{k[1]}}}": v
            for k, v in registry.counters.items()
        },
        'gauges': {
            f"{k[0]}{{{k[1]}}}": v
            for k, v in registry.gauges.items()
        },
    }