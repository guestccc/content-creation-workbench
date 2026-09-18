"""创作者主页接口测试：覆盖增删改查、平台唯一约束、平台/标签过滤。"""


class TestCreateCreator:
    """创建创作者接口。"""

    def test_create_success(self, client, sample_creator_payload):
        """正常创建应返回 201 与完整字段。"""
        response = client.post("/api/v1/creators", json=sample_creator_payload)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["success"] is True

        data = body["data"]
        assert data["id"] > 0
        assert data["platform"] == "xhs"
        assert data["platform_label"] == "小红书"
        assert data["name"] == "保温杯研究员"
        assert data["tags"] == ["母婴", "好物"]
        assert data["remark"] == "更新稳定，适合跟品"
        assert data["created_at"].endswith("Z")

    def test_tags_are_cleaned(self, client, sample_creator_payload):
        """标签应被 trim、去空项、去重保序。"""
        sample_creator_payload["tags"] = ["  母婴 ", "", "母婴", "美食"]
        response = client.post("/api/v1/creators", json=sample_creator_payload)

        assert response.status_code == 201, response.text
        assert response.json()["data"]["tags"] == ["母婴", "美食"]

    def test_duplicate_homepage_in_same_platform_returns_409(
        self, client, sample_creator_payload, created_creator
    ):
        """同平台下主页重复应返回 409。"""
        response = client.post("/api/v1/creators", json=sample_creator_payload)

        assert response.status_code == 409
        body = response.json()
        assert body["success"] is False
        assert body["error"]["code"] == "CONFLICT"

    def test_same_homepage_in_other_platform_is_allowed(
        self, client, sample_creator_payload, created_creator
    ):
        """不同平台下主页可以相同（各平台 ID 体系独立）。"""
        payload = dict(sample_creator_payload, platform="dy")
        response = client.post("/api/v1/creators", json=payload)

        assert response.status_code == 201, response.text

    def test_invalid_platform_returns_422(self, client, sample_creator_payload):
        """平台取值不在支持列表内应返回 422。"""
        sample_creator_payload["platform"] = "taobao"
        response = client.post("/api/v1/creators", json=sample_creator_payload)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_platform_is_normalized(self, client, sample_creator_payload):
        """平台带空白与大小写差异时应被归一化。"""
        sample_creator_payload["platform"] = "  XHS "
        response = client.post("/api/v1/creators", json=sample_creator_payload)

        assert response.status_code == 201, response.text
        assert response.json()["data"]["platform"] == "xhs"

    def test_empty_name_returns_422(self, client, sample_creator_payload):
        """名称为空应返回 422 且带字段级错误明细。"""
        sample_creator_payload["name"] = "   "
        response = client.post("/api/v1/creators", json=sample_creator_payload)

        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert {item["field"] for item in body["error"]["details"]} == {"name"}

    def test_too_long_tag_returns_422(self, client, sample_creator_payload):
        """单个标签超长应返回 422。"""
        sample_creator_payload["tags"] = ["这" * 21]
        response = client.post("/api/v1/creators", json=sample_creator_payload)

        assert response.status_code == 422


class TestGetCreator:
    """查询单个创作者接口。"""

    def test_get_detail(self, client, created_creator):
        """按 ID 应能查到刚创建的创作者。"""
        response = client.get(f"/api/v1/creators/{created_creator['id']}")

        assert response.status_code == 200
        assert response.json()["data"]["id"] == created_creator["id"]

    def test_get_missing_returns_404(self, client):
        """查询不存在的创作者应返回 404。"""
        response = client.get("/api/v1/creators/999999")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


class TestListCreators:
    """创作者列表接口。"""

    def test_empty_list(self, client):
        """无数据时应返回空列表与 total=0。"""
        response = client.get("/api/v1/creators")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 0
        assert data["items"] == []

    def test_filter_by_platform(self, client, create_creator):
        """按平台过滤应只返回匹配项。"""
        create_creator(platform="xhs", name="小红书博主")
        create_creator(platform="dy", name="抖音博主")

        response = client.get("/api/v1/creators", params={"platform": "dy"})

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["name"] == "抖音博主"
        assert data["items"][0]["platform_label"] == "抖音"

    def test_filter_by_tag(self, client, create_creator):
        """按标签过滤应只返回带该标签的创作者。"""
        create_creator(name="母婴博主", tags=["母婴", "好物"])
        create_creator(name="美食博主", tags=["美食"])

        response = client.get("/api/v1/creators", params={"tag": "母婴"})

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["name"] == "母婴博主"

    def test_list_orders_newest_first(self, client, create_creator):
        """列表应按创建时间倒序（最新添加的在最前）。"""
        create_creator(name="先加的")
        create_creator(name="后加的")

        response = client.get("/api/v1/creators")

        items = response.json()["data"]["items"]
        assert [item["name"] for item in items[:2]] == ["后加的", "先加的"]


class TestUpdateCreator:
    """更新创作者接口。"""

    def test_partial_update_keeps_other_fields(self, client, created_creator):
        """只传部分字段时，其余字段应保持原值。"""
        response = client.put(
            f"/api/v1/creators/{created_creator['id']}",
            json={"remark": "新备注"},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["remark"] == "新备注"
        assert data["name"] == created_creator["name"]
        assert data["homepage"] == created_creator["homepage"]
        assert data["tags"] == created_creator["tags"]

    def test_update_tags_replaces_whole_list(self, client, created_creator):
        """更新 tags 是整体替换（与创建走同一套清洗）。"""
        response = client.put(
            f"/api/v1/creators/{created_creator['id']}",
            json={"tags": [" 美食 ", "", "美食"]},
        )

        assert response.status_code == 200
        assert response.json()["data"]["tags"] == ["美食"]

    def test_update_to_conflicting_homepage_returns_409(self, client, create_creator):
        """改成同平台下已存在的主页应返回 409，且原数据不受影响。"""
        first = create_creator(name="博主甲", homepage="https://example.com/a")
        second = create_creator(name="博主乙", homepage="https://example.com/b")

        response = client.put(
            f"/api/v1/creators/{second['id']}", json={"homepage": "https://example.com/a"}
        )

        assert response.status_code == 409
        detail = client.get(f"/api/v1/creators/{second['id']}")
        assert detail.json()["data"]["homepage"] == "https://example.com/b"
        assert first["homepage"] == "https://example.com/a"

    def test_update_missing_returns_404(self, client):
        """更新不存在的创作者应返回 404。"""
        response = client.put("/api/v1/creators/999999", json={"name": "新名字"})

        assert response.status_code == 404

    def test_update_with_invalid_platform_returns_422(self, client, created_creator):
        """非法平台应被校验拦截。"""
        response = client.put(
            f"/api/v1/creators/{created_creator['id']}", json={"platform": "invalid"}
        )

        assert response.status_code == 422


class TestDeleteCreator:
    """删除创作者接口。"""

    def test_delete_success(self, client, created_creator):
        """删除后再次查询应返回 404。"""
        response = client.delete(f"/api/v1/creators/{created_creator['id']}")

        assert response.status_code == 200
        assert response.json()["data"]["id"] == created_creator["id"]
        assert client.get(f"/api/v1/creators/{created_creator['id']}").status_code == 404

    def test_delete_missing_returns_404(self, client):
        """删除不存在的创作者应返回 404。"""
        response = client.delete("/api/v1/creators/999999")

        assert response.status_code == 404
