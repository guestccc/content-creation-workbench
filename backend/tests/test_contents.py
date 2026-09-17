"""内容接口测试：覆盖增删改查、校验、过滤与异常分支。"""


class TestCreateContent:
    """创建内容接口。"""

    def test_create_success(self, client, sample_content_payload):
        """正常创建应返回 201 与完整字段。"""
        response = client.post("/api/v1/contents", json=sample_content_payload)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["success"] is True

        data = body["data"]
        assert data["id"] > 0
        assert data["title"] == "秋季新品保温杯种草文案"
        assert data["platform"] == "抖音"
        assert data["status"] == "draft"
        assert data["tags"] == ["保温杯", "办公好物"]
        assert data["author"] == "小明"
        # 时间应输出为带 Z 后缀的 UTC ISO8601
        assert data["created_at"].endswith("Z")

    def test_title_is_trimmed(self, client, sample_content_payload):
        """标题首尾空白应被自动去除。"""
        sample_content_payload["title"] = "   前后有空格   "
        response = client.post("/api/v1/contents", json=sample_content_payload)

        assert response.status_code == 201
        assert response.json()["data"]["title"] == "前后有空格"

    def test_duplicate_tags_are_removed(self, client, sample_content_payload):
        """重复标签应被去重，空白标签应被丢弃。"""
        sample_content_payload["tags"] = ["  带货 ", "带货", "", "   ", "好物"]
        response = client.post("/api/v1/contents", json=sample_content_payload)

        assert response.status_code == 201
        assert response.json()["data"]["tags"] == ["带货", "好物"]

    def test_empty_title_returns_422(self, client, sample_content_payload):
        """标题为空应返回 422 且带字段级错误明细。"""
        sample_content_payload["title"] = "   "
        response = client.post("/api/v1/contents", json=sample_content_payload)

        assert response.status_code == 422
        body = response.json()
        assert body["success"] is False
        assert body["error"]["code"] == "VALIDATION_ERROR"
        # 字段路径输出给前端时应为 title 而非 body.title
        assert {item["field"] for item in body["error"]["details"]} == {"title"}

    def test_invalid_status_returns_422(self, client, sample_content_payload):
        """状态取值不在枚举内应返回 422。"""
        sample_content_payload["status"] = "unknown"
        response = client.post("/api/v1/contents", json=sample_content_payload)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_too_many_tags_returns_422(self, client, sample_content_payload):
        """标签数量超过上限应返回 422。"""
        sample_content_payload["tags"] = [f"标签{i}" for i in range(11)]
        response = client.post("/api/v1/contents", json=sample_content_payload)

        assert response.status_code == 422


class TestGetContent:
    """查询内容接口。"""

    def test_get_detail(self, client, created_content):
        """按 ID 应能查到刚创建的内容。"""
        response = client.get(f"/api/v1/contents/{created_content['id']}")

        assert response.status_code == 200
        assert response.json()["data"]["id"] == created_content["id"]

    def test_get_missing_returns_404(self, client):
        """查询不存在的内容应返回 404 与业务错误码。"""
        response = client.get("/api/v1/contents/999999")

        assert response.status_code == 404
        body = response.json()
        assert body["success"] is False
        assert body["error"]["code"] == "NOT_FOUND"
        assert "999999" in body["error"]["message"]


class TestListContents:
    """列表查询接口。"""

    def test_empty_list(self, client):
        """无数据时应返回空列表与 total=0。"""
        response = client.get("/api/v1/contents")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 0
        assert data["items"] == []
        assert data["page"] == 1

    def test_pagination(self, client):
        """分页应正确返回总数与当前页条数。"""
        for index in range(5):
            client.post("/api/v1/contents", json={"title": f"内容 {index}"})

        response = client.get("/api/v1/contents", params={"page": 2, "page_size": 2})

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["page"] == 2
        assert data["page_size"] == 2

    def test_filter_by_status(self, client):
        """按状态过滤应只返回匹配项。"""
        client.post("/api/v1/contents", json={"title": "草稿内容", "status": "draft"})
        client.post("/api/v1/contents", json={"title": "已发布内容", "status": "published"})

        response = client.get("/api/v1/contents", params={"status": "published"})

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["title"] == "已发布内容"

    def test_search_by_keyword(self, client):
        """关键字应能匹配标题或正文。"""
        client.post("/api/v1/contents", json={"title": "保温杯推荐", "body": "空"})
        client.post("/api/v1/contents", json={"title": "其他", "body": "这里有保温杯的内容"})
        client.post("/api/v1/contents", json={"title": "无关内容", "body": "无关"})

        response = client.get("/api/v1/contents", params={"keyword": "保温杯"})

        assert response.status_code == 200
        assert response.json()["data"]["total"] == 2

    def test_invalid_page_returns_422(self, client):
        """页码小于 1 应被参数校验拦截。"""
        response = client.get("/api/v1/contents", params={"page": 0})

        assert response.status_code == 422


class TestUpdateContent:
    """更新内容接口。"""

    def test_partial_update_keeps_other_fields(self, client, created_content):
        """只传部分字段时，其余字段应保持原值。"""
        response = client.put(
            f"/api/v1/contents/{created_content['id']}",
            json={"status": "published"},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "published"
        # 未传的字段保持原值
        assert data["title"] == created_content["title"]
        assert data["tags"] == created_content["tags"]

    def test_update_tags(self, client, created_content):
        """标签应能整体替换。"""
        response = client.put(
            f"/api/v1/contents/{created_content['id']}",
            json={"tags": ["新标签", "第二个"]},
        )

        assert response.status_code == 200
        assert response.json()["data"]["tags"] == ["新标签", "第二个"]

    def test_update_missing_returns_404(self, client):
        """更新不存在的内容应返回 404。"""
        response = client.put("/api/v1/contents/999999", json={"title": "新标题"})

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_update_with_invalid_status_returns_422(self, client, created_content):
        """非法状态应被校验拦截，且原数据不受影响。"""
        response = client.put(
            f"/api/v1/contents/{created_content['id']}",
            json={"status": "invalid"},
        )

        assert response.status_code == 422
        # 确认原数据未被修改
        detail = client.get(f"/api/v1/contents/{created_content['id']}")
        assert detail.json()["data"]["status"] == created_content["status"]


class TestDeleteContent:
    """删除内容接口。"""

    def test_delete_success(self, client, created_content):
        """删除后再次查询应返回 404。"""
        response = client.delete(f"/api/v1/contents/{created_content['id']}")

        assert response.status_code == 200
        assert response.json()["data"]["id"] == created_content["id"]

        assert client.get(f"/api/v1/contents/{created_content['id']}").status_code == 404

    def test_delete_missing_returns_404(self, client):
        """删除不存在的内容应返回 404。"""
        response = client.delete("/api/v1/contents/999999")

        assert response.status_code == 404


class TestStatistics:
    """内容统计接口。"""

    def test_statistics_counts_by_status(self, client):
        """统计接口应返回总数与各状态分布，未出现的状态补 0。"""
        client.post("/api/v1/contents", json={"title": "草稿一", "status": "draft"})
        client.post("/api/v1/contents", json={"title": "草稿二", "status": "draft"})
        client.post("/api/v1/contents", json={"title": "已发布", "status": "published"})

        response = client.get("/api/v1/contents/statistics")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 3
        assert data["by_status"]["draft"] == 2
        assert data["by_status"]["published"] == 1
        assert data["by_status"]["reviewing"] == 0
        assert data["by_status"]["archived"] == 0

    def test_statistics_route_not_shadowed_by_path_param(self, client):
        """/statistics 不应被 /{content_id} 路由吞掉（回归测试）。"""
        response = client.get("/api/v1/contents/statistics")

        assert response.status_code == 200
        assert "by_status" in response.json()["data"]
