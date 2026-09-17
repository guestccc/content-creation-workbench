"""API v1 路由聚合。

新增业务模块时，在此处 include_router 即可，main.py 无需改动。
"""

from fastapi import APIRouter

from app.api.v1 import accounts, contents, health, publish_tasks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(contents.router)
api_router.include_router(accounts.router)
api_router.include_router(publish_tasks.router)
