"""系统接口测试：根路径与健康检查。"""


def test_root_returns_service_info(client):
    """根路径应返回服务名称、版本与文档地址。"""
    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["name"] == "content-creation-workbench"
    assert body["data"]["docs"] == "/docs"


def test_health_check_returns_ok(client):
    """数据库正常时健康检查应返回 ok / connected。"""
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "ok"
    assert body["data"]["database"] == "connected"
    assert body["data"]["version"] == "0.1.0"


def test_unknown_route_returns_structured_404(client):
    """访问不存在的路由应返回统一结构的错误响应，而非默认的 detail 格式。"""
    response = client.get("/api/v1/not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_404"
