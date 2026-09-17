"""账号接口测试：覆盖增删改查、唯一约束、过滤与删除保护。

安全相关断言：任何响应体都不得出现凭证字段（cookie/token/password 等）。
"""


class TestCreateAccount:
    """创建账号接口。"""

    def test_create_success(self, client, sample_account_payload):
        """正常创建应返回 201 与完整字段。"""
        response = client.post("/api/v1/accounts", json=sample_account_payload)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["success"] is True

        data = body["data"]
        assert data["id"] > 0
        assert data["platform"] == "抖音"
        assert data["nickname"] == "小明的好物分享"
        assert data["account_uid"] == "dy_10086"
        assert data["status"] == "active"
        assert data["created_at"].endswith("Z")

    def test_response_never_contains_credentials(self, client, sample_account_payload):
        """响应体不得包含任何凭证字段（凭证只存在客户端本地）。"""
        payload = dict(sample_account_payload)
        # 即使客户端误传凭证字段，也不应被持久化或回显
        payload["cookie"] = "sessionid=secret"
        payload["token"] = "secret-token"

        response = client.post("/api/v1/accounts", json=payload)

        assert response.status_code == 201, response.text
        data = response.json()["data"]
        serialized = str(data).lower()
        for forbidden in ("cookie", "token", "password", "secret"):
            assert forbidden not in serialized

    def test_duplicate_nickname_in_same_platform_returns_409(
        self, client, sample_account_payload, created_account
    ):
        """同平台下昵称重复应返回 409。"""
        response = client.post("/api/v1/accounts", json=sample_account_payload)

        assert response.status_code == 409
        body = response.json()
        assert body["success"] is False
        assert body["error"]["code"] == "CONFLICT"

    def test_same_nickname_in_other_platform_is_allowed(
        self, client, sample_account_payload, created_account
    ):
        """不同平台下昵称可以相同。"""
        payload = dict(sample_account_payload, platform="小红书")
        response = client.post("/api/v1/accounts", json=payload)

        assert response.status_code == 201, response.text

    def test_empty_nickname_returns_422(self, client, sample_account_payload):
        """昵称为空应返回 422 且带字段级错误明细。"""
        sample_account_payload["nickname"] = "   "
        response = client.post("/api/v1/accounts", json=sample_account_payload)

        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert {item["field"] for item in body["error"]["details"]} == {"nickname"}

    def test_invalid_status_returns_422(self, client, sample_account_payload):
        """状态取值不在枚举内应返回 422。"""
        sample_account_payload["status"] = "unknown"
        response = client.post("/api/v1/accounts", json=sample_account_payload)

        assert response.status_code == 422

    def test_nickname_is_trimmed(self, client, sample_account_payload):
        """昵称首尾空白应被自动去除。"""
        sample_account_payload["nickname"] = "  带空格的昵称  "
        response = client.post("/api/v1/accounts", json=sample_account_payload)

        assert response.status_code == 201
        assert response.json()["data"]["nickname"] == "带空格的昵称"


class TestGetAccount:
    """查询单个账号接口。"""

    def test_get_detail(self, client, created_account):
        """按 ID 应能查到刚创建的账号。"""
        response = client.get(f"/api/v1/accounts/{created_account['id']}")

        assert response.status_code == 200
        assert response.json()["data"]["id"] == created_account["id"]

    def test_get_missing_returns_404(self, client):
        """查询不存在的账号应返回 404。"""
        response = client.get("/api/v1/accounts/999999")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


class TestListAccounts:
    """账号列表接口。"""

    def test_empty_list(self, client):
        """无数据时应返回空列表与 total=0。"""
        response = client.get("/api/v1/accounts")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 0
        assert data["items"] == []

    def test_filter_by_platform(self, client, create_account):
        """按平台过滤应只返回匹配项。"""
        create_account(platform="抖音", nickname="抖音号")
        create_account(platform="小红书", nickname="小红书号")

        response = client.get("/api/v1/accounts", params={"platform": "小红书"})

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["nickname"] == "小红书号"

    def test_filter_by_status(self, client, create_account):
        """按状态过滤应只返回匹配项。"""
        create_account(nickname="正常账号", status="active")
        create_account(nickname="停用账号", status="disabled")

        response = client.get("/api/v1/accounts", params={"status": "disabled"})

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["nickname"] == "停用账号"

    def test_search_by_keyword_matches_nickname_or_remark(self, client, create_account):
        """关键字应能匹配昵称或备注。"""
        create_account(nickname="保温杯小铺")
        create_account(nickname="其他账号", remark="主营保温杯周边")
        create_account(nickname="无关账号")

        response = client.get("/api/v1/accounts", params={"keyword": "保温杯"})

        assert response.status_code == 200
        assert response.json()["data"]["total"] == 2


class TestUpdateAccount:
    """更新账号接口。"""

    def test_partial_update_keeps_other_fields(self, client, created_account):
        """只传部分字段时，其余字段应保持原值。"""
        response = client.put(
            f"/api/v1/accounts/{created_account['id']}",
            json={"status": "disabled"},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "disabled"
        assert data["nickname"] == created_account["nickname"]
        assert data["platform"] == created_account["platform"]

    def test_update_to_conflicting_nickname_returns_409(self, client, create_account):
        """改成同平台下已存在的昵称应返回 409，且原数据不受影响。"""
        first = create_account(platform="抖音", nickname="账号甲")
        second = create_account(platform="抖音", nickname="账号乙")

        response = client.put(
            f"/api/v1/accounts/{second['id']}", json={"nickname": "账号甲"}
        )

        assert response.status_code == 409
        detail = client.get(f"/api/v1/accounts/{second['id']}")
        assert detail.json()["data"]["nickname"] == "账号乙"
        assert first["nickname"] == "账号甲"

    def test_update_missing_returns_404(self, client):
        """更新不存在的账号应返回 404。"""
        response = client.put("/api/v1/accounts/999999", json={"nickname": "新昵称"})

        assert response.status_code == 404

    def test_update_with_invalid_status_returns_422(self, client, created_account):
        """非法状态应被校验拦截。"""
        response = client.put(
            f"/api/v1/accounts/{created_account['id']}", json={"status": "invalid"}
        )

        assert response.status_code == 422


class TestDeleteAccount:
    """删除账号接口。"""

    def test_delete_success(self, client, created_account):
        """删除后再次查询应返回 404。"""
        response = client.delete(f"/api/v1/accounts/{created_account['id']}")

        assert response.status_code == 200
        assert response.json()["data"]["id"] == created_account["id"]
        assert client.get(f"/api/v1/accounts/{created_account['id']}").status_code == 404

    def test_delete_missing_returns_404(self, client):
        """删除不存在的账号应返回 404。"""
        response = client.delete("/api/v1/accounts/999999")

        assert response.status_code == 404

    def test_delete_blocked_by_pending_task(self, client, created_content, created_account):
        """账号下仍有排队中的任务时应拒绝删除（409）。"""
        client.post(
            "/api/v1/publish-tasks",
            json={"content_id": created_content["id"], "account_id": created_account["id"]},
        )

        response = client.delete(f"/api/v1/accounts/{created_account['id']}")

        assert response.status_code == 409
        body = response.json()
        assert body["error"]["code"] == "CONFLICT"
        assert "未完成的发布任务" in body["error"]["message"]

    def test_delete_blocked_by_running_task(self, client, created_content, created_account):
        """账号下仍有发布中的任务时应拒绝删除（409）。"""
        client.post(
            "/api/v1/publish-tasks",
            json={"content_id": created_content["id"], "account_id": created_account["id"]},
        )
        client.post("/api/v1/publish-tasks/claim", json={"limit": 5})

        response = client.delete(f"/api/v1/accounts/{created_account['id']}")

        assert response.status_code == 409

    def test_delete_blocked_by_finished_task_history(
        self, client, created_content, created_account
    ):
        """存在已结束的任务历史时，受外键约束保护应拒绝删除（409）。"""
        task = client.post(
            "/api/v1/publish-tasks",
            json={"content_id": created_content["id"], "account_id": created_account["id"]},
        ).json()["data"]
        client.post("/api/v1/publish-tasks/claim", json={"limit": 5})
        client.post(
            f"/api/v1/publish-tasks/{task['id']}/report",
            json={"success": True, "result_url": "https://example.com/p/1"},
        )

        response = client.delete(f"/api/v1/accounts/{created_account['id']}")

        assert response.status_code == 409
        assert "发布任务记录" in response.json()["error"]["message"]
