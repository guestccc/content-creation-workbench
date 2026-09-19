"""Cookie 库接口测试：覆盖增删改查、平台唯一约束、平台过滤、预览截断。"""


class TestCreateCookie:
    """保存 Cookie 接口。"""

    def test_create_success(self, client):
        """正常保存应返回 201 与完整字段。"""
        response = client.post(
            "/api/v1/crawl-cookies",
            json={
                "platform": "xhs",
                "name": "主号",
                "cookie": "web_session=abc123; a1=xyz",
                "remark": "常用小号",
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["success"] is True

        data = body["data"]
        assert data["id"] > 0
        assert data["platform"] == "xhs"
        assert data["platform_label"] == "小红书"
        assert data["name"] == "主号"
        assert data["cookie"] == "web_session=abc123; a1=xyz"
        assert data["remark"] == "常用小号"
        assert data["created_at"].endswith("Z")

    def test_duplicate_name_in_same_platform_returns_409(self, client):
        """同平台下名称重复应返回 409。"""
        payload = {
            "platform": "xhs",
            "name": "主号",
            "cookie": "web_session=abc",
        }
        response = client.post("/api/v1/crawl-cookies", json=payload)
        assert response.status_code == 201, response.text

        response = client.post("/api/v1/crawl-cookies", json=payload)
        assert response.status_code == 409
        body = response.json()
        assert body["success"] is False
        assert body["error"]["code"] == "CONFLICT"

    def test_same_name_in_other_platform_is_allowed(self, client):
        """不同平台下名称可以相同（各平台 Cookie 体系独立）。"""
        client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "xhs", "name": "主号", "cookie": "a=1"},
        )
        response = client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "dy", "name": "主号", "cookie": "b=2"},
        )

        assert response.status_code == 201, response.text

    def test_invalid_platform_returns_422(self, client):
        """平台取值不在支持列表内应返回 422。"""
        response = client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "taobao", "name": "主号", "cookie": "a=1"},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_platform_is_normalized(self, client):
        """平台带空白与大小写差异时应被归一化。"""
        response = client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "  XHS ", "name": "主号", "cookie": "a=1"},
        )

        assert response.status_code == 201, response.text
        assert response.json()["data"]["platform"] == "xhs"

    def test_empty_name_returns_422(self, client):
        """名称为空应返回 422 且带字段级错误明细。"""
        response = client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "xhs", "name": "   ", "cookie": "a=1"},
        )

        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert {item["field"] for item in body["error"]["details"]} == {"name"}

    def test_empty_cookie_returns_422(self, client):
        """Cookie 串为空应返回 422。"""
        response = client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "xhs", "name": "主号", "cookie": "   "},
        )

        assert response.status_code == 422


class TestGetCookie:
    """查询单条 Cookie 接口。"""

    def test_get_detail(self, client):
        """按 ID 应能查到完整 cookie 串。"""
        created = client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "xhs", "name": "主号", "cookie": "secret-token"},
        ).json()["data"]

        response = client.get(f"/api/v1/crawl-cookies/{created['id']}")

        assert response.status_code == 200
        assert response.json()["data"]["cookie"] == "secret-token"

    def test_get_missing_returns_404(self, client):
        """查询不存在的 Cookie 应返回 404。"""
        response = client.get("/api/v1/crawl-cookies/999999")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


class TestListCookies:
    """Cookie 列表接口。"""

    def test_empty_list(self, client):
        """无数据时应返回空列表与 total=0。"""
        response = client.get("/api/v1/crawl-cookies")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 0
        assert data["items"] == []

    def test_list_returns_preview_not_full_cookie(self, client):
        """列表项只给 cookie_preview（截断预览），不给完整 cookie 串。"""
        long_cookie = "a" * 100
        client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "xhs", "name": "主号", "cookie": long_cookie},
        )

        response = client.get("/api/v1/crawl-cookies")

        item = response.json()["data"]["items"][0]
        assert item["cookie_preview"] == "a" * 60 + "…"
        assert "cookie" not in item

    def test_filter_by_platform(self, client):
        """按平台过滤应只返回匹配项。"""
        client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "xhs", "name": "小红书号", "cookie": "a=1"},
        )
        client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "dy", "name": "抖音号", "cookie": "b=2"},
        )

        response = client.get("/api/v1/crawl-cookies", params={"platform": "dy"})

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["name"] == "抖音号"
        assert data["items"][0]["platform_label"] == "抖音"


class TestUpdateCookie:
    """更新 Cookie 接口。"""

    def test_partial_update_keeps_other_fields(self, client):
        """只传部分字段时，其余字段应保持原值。"""
        created = client.post(
            "/api/v1/crawl-cookies",
            json={
                "platform": "xhs",
                "name": "主号",
                "cookie": "old-session",
                "remark": "备注",
            },
        ).json()["data"]

        response = client.put(
            f"/api/v1/crawl-cookies/{created['id']}",
            json={"cookie": "new-session"},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["cookie"] == "new-session"
        assert data["name"] == "主号"
        assert data["remark"] == "备注"

    def test_update_to_conflicting_name_returns_409(self, client):
        """改成同平台下已存在的名称应返回 409，且原数据不受影响。"""
        first = client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "xhs", "name": "主号", "cookie": "a=1"},
        ).json()["data"]
        second = client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "xhs", "name": "小号", "cookie": "b=2"},
        ).json()["data"]

        response = client.put(
            f"/api/v1/crawl-cookies/{second['id']}", json={"name": "主号"}
        )

        assert response.status_code == 409
        detail = client.get(f"/api/v1/crawl-cookies/{second['id']}")
        assert detail.json()["data"]["name"] == "小号"
        assert first["name"] == "主号"

    def test_update_missing_returns_404(self, client):
        """更新不存在的 Cookie 应返回 404。"""
        response = client.put("/api/v1/crawl-cookies/999999", json={"name": "新名字"})

        assert response.status_code == 404


class TestDeleteCookie:
    """删除 Cookie 接口。"""

    def test_delete_success(self, client):
        """删除后再次查询应返回 404。"""
        created = client.post(
            "/api/v1/crawl-cookies",
            json={"platform": "xhs", "name": "主号", "cookie": "a=1"},
        ).json()["data"]

        response = client.delete(f"/api/v1/crawl-cookies/{created['id']}")

        assert response.status_code == 200
        assert response.json()["data"]["id"] == created["id"]
        assert client.get(f"/api/v1/crawl-cookies/{created['id']}").status_code == 404

    def test_delete_missing_returns_404(self, client):
        """删除不存在的 Cookie 应返回 404。"""
        response = client.delete("/api/v1/crawl-cookies/999999")

        assert response.status_code == 404
