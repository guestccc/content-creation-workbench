"""发布任务接口测试：覆盖创建、批量分发、认领、上报、重试与状态流转。

重点是状态机的边界：终态不可再流转、重复认领不会拿到同一条任务、
失败重试的次数控制。
"""

from datetime import datetime, timedelta, timezone


def _iso_utc(delta: timedelta) -> str:
    """生成相对当前时间偏移若干的 UTC ISO8601 字符串。"""
    return (datetime.now(timezone.utc) + delta).isoformat().replace("+00:00", "Z")


def _claim(client, limit: int = 5) -> list:
    """认领任务并返回任务列表。"""
    response = client.post("/api/v1/publish-tasks/claim", json={"limit": limit})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _report(client, task_id: int, success: bool, **extra):
    """上报任务结果。"""
    payload = {"success": success, **extra}
    return client.post(f"/api/v1/publish-tasks/{task_id}/report", json=payload)


class TestCreateTask:
    """创建发布任务接口。"""

    def test_create_success(self, client, created_content, created_account):
        """正常创建应返回 201，并带出冗余的标题与昵称。"""
        response = client.post(
            "/api/v1/publish-tasks",
            json={
                "content_id": created_content["id"],
                "account_id": created_account["id"],
            },
        )

        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["status"] == "pending"
        assert data["content_id"] == created_content["id"]
        # 列表展示需要的冗余字段应由关联对象带出，避免前端二次请求
        assert data["content_title"] == created_content["title"]
        assert data["account_nickname"] == created_account["nickname"]
        assert data["platform"] == created_account["platform"]
        assert data["retry_count"] == 0
        assert data["max_retries"] == 2
        assert data["scheduled_at"] is None

    def test_create_with_future_schedule(self, client, created_content, created_account):
        """计划时间应被换算并存为 UTC。"""
        response = client.post(
            "/api/v1/publish-tasks",
            json={
                "content_id": created_content["id"],
                "account_id": created_account["id"],
                "scheduled_at": _iso_utc(timedelta(hours=2)),
            },
        )

        assert response.status_code == 201
        assert response.json()["data"]["scheduled_at"].endswith("Z")

    def test_create_with_missing_content_returns_404(self, client, created_account):
        """内容不存在应返回 404。"""
        response = client.post(
            "/api/v1/publish-tasks",
            json={"content_id": 999999, "account_id": created_account["id"]},
        )

        assert response.status_code == 404
        assert "内容不存在" in response.json()["error"]["message"]

    def test_create_with_missing_account_returns_404(self, client, created_content):
        """账号不存在应返回 404。"""
        response = client.post(
            "/api/v1/publish-tasks",
            json={"content_id": created_content["id"], "account_id": 999999},
        )

        assert response.status_code == 404
        assert "账号不存在" in response.json()["error"]["message"]

    def test_invalid_content_id_returns_422(self, client, created_account):
        """内容 ID 小于 1 应被参数校验拦截。"""
        response = client.post(
            "/api/v1/publish-tasks",
            json={"content_id": 0, "account_id": created_account["id"]},
        )

        assert response.status_code == 422


class TestCreateBatch:
    """批量创建发布任务接口。"""

    def test_batch_create_success(self, client, created_content, create_account):
        """一条内容应能一次分发到多个账号。"""
        first = create_account(nickname="账号一")
        second = create_account(nickname="账号二")
        third = create_account(nickname="账号三")

        response = client.post(
            "/api/v1/publish-tasks/batch",
            json={
                "content_id": created_content["id"],
                "account_ids": [first["id"], second["id"], third["id"]],
            },
        )

        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert len(data) == 3
        assert {item["account_id"] for item in data} == {
            first["id"],
            second["id"],
            third["id"],
        }
        assert all(item["status"] == "pending" for item in data)

    def test_batch_dedupes_account_ids(self, client, created_content, created_account):
        """重复的账号 ID 应被去重，不会产生重复任务。"""
        response = client.post(
            "/api/v1/publish-tasks/batch",
            json={
                "content_id": created_content["id"],
                "account_ids": [
                    created_account["id"],
                    created_account["id"],
                    created_account["id"],
                ],
            },
        )

        assert response.status_code == 201
        assert len(response.json()["data"]) == 1

    def test_batch_with_missing_account_rolls_back_all(
        self, client, created_content, created_account
    ):
        """有一个账号不存在时，整批回滚，不产生半成品任务。"""
        response = client.post(
            "/api/v1/publish-tasks/batch",
            json={
                "content_id": created_content["id"],
                "account_ids": [created_account["id"], 999999],
            },
        )

        assert response.status_code == 404
        assert "999999" in response.json()["error"]["message"]

        listing = client.get("/api/v1/publish-tasks")
        assert listing.json()["data"]["total"] == 0

    def test_batch_exceeding_limit_returns_422(self, client, created_content):
        """账号数量超过上限应返回 422。"""
        response = client.post(
            "/api/v1/publish-tasks/batch",
            json={"content_id": created_content["id"], "account_ids": list(range(1, 60))},
        )

        assert response.status_code == 422

    def test_batch_with_empty_accounts_returns_422(self, client, created_content):
        """账号列表为空应返回 422。"""
        response = client.post(
            "/api/v1/publish-tasks/batch",
            json={"content_id": created_content["id"], "account_ids": []},
        )

        assert response.status_code == 422


class TestClaimTasks:
    """客户端认领任务接口。"""

    def test_claim_sets_status_running(
        self, client, created_content, created_account, create_task
    ):
        """认领后任务应转为 running 并记录开始时间。"""
        task = create_task(created_content["id"], created_account["id"])

        claimed = _claim(client)

        assert len(claimed) == 1
        assert claimed[0]["id"] == task["id"]
        assert claimed[0]["status"] == "running"
        assert claimed[0]["started_at"] is not None

    def test_claim_returns_empty_when_no_pending(self, client):
        """没有待执行任务时应返回空列表而不是报错。"""
        assert _claim(client) == []

    def test_claim_does_not_return_same_task_twice(
        self, client, created_content, created_account, create_task
    ):
        """同一条任务不应被重复认领（并发安全的核心保证）。"""
        create_task(created_content["id"], created_account["id"])

        first = _claim(client)
        second = _claim(client)

        assert len(first) == 1
        assert second == []

    def test_claim_respects_limit(self, client, created_content, create_account, create_task):
        """认领数量不应超过 limit。"""
        for index in range(3):
            account = create_account(nickname=f"账号{index}")
            create_task(created_content["id"], account["id"])

        claimed = _claim(client, limit=2)

        assert len(claimed) == 2

    def test_claim_skips_future_scheduled_tasks(
        self, client, created_content, created_account, create_task
    ):
        """未到计划时间的任务不应被认领。"""
        create_task(
            created_content["id"],
            created_account["id"],
            scheduled_at=_iso_utc(timedelta(hours=2)),
        )

        assert _claim(client) == []

    def test_claim_includes_due_and_immediate_tasks(
        self, client, created_content, create_account, create_task
    ):
        """计划时间已过的任务与立即执行的任务都应被认领，且立即执行的排在前面。"""
        scheduled_account = create_account(nickname="定时账号")
        immediate_account = create_account(nickname="立即账号")

        scheduled = create_task(
            created_content["id"],
            scheduled_account["id"],
            scheduled_at=_iso_utc(timedelta(hours=-1)),
        )
        immediate = create_task(created_content["id"], immediate_account["id"])

        claimed = _claim(client)

        assert [item["id"] for item in claimed] == [immediate["id"], scheduled["id"]]

    def test_claim_filtered_by_account(
        self, client, created_content, create_account, create_task
    ):
        """指定账号后只认领该账号的任务。"""
        first = create_account(nickname="账号甲")
        second = create_account(nickname="账号乙")
        create_task(created_content["id"], first["id"])
        second_task = create_task(created_content["id"], second["id"])

        response = client.post(
            "/api/v1/publish-tasks/claim",
            json={"limit": 5, "account_id": second["id"]},
        )

        assert response.status_code == 200
        claimed = response.json()["data"]
        assert [item["id"] for item in claimed] == [second_task["id"]]


class TestReportTask:
    """客户端上报结果接口。"""

    def test_report_success(self, client, created_content, created_account, create_task):
        """上报成功应转为 success 并保存链接。"""
        task = create_task(created_content["id"], created_account["id"])
        _claim(client)

        response = _report(
            client, task["id"], True, result_url="https://example.com/p/1"
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "success"
        assert data["result_url"] == "https://example.com/p/1"
        assert data["finished_at"] is not None

    def test_report_failure_requeues_when_retry_left(
        self, client, created_content, created_account, create_task
    ):
        """失败但仍有重试额度时应回到 pending，且重试计数加一。"""
        task = create_task(created_content["id"], created_account["id"], max_retries=2)
        _claim(client)

        response = _report(client, task["id"], False, error_message="登录态失效")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "pending"
        assert data["retry_count"] == 1
        assert data["error_message"] == "登录态失效"
        # 回到队列后应清空本次执行的时间痕迹
        assert data["started_at"] is None
        assert data["finished_at"] is None

    def test_report_failure_exhausts_retries(
        self, client, created_content, created_account, create_task
    ):
        """重试额度用尽后应转为 failed 终态。"""
        task = create_task(created_content["id"], created_account["id"], max_retries=1)
        task_id = task["id"]

        _claim(client)
        first = _report(client, task_id, False, error_message="第一次失败")
        assert first.json()["data"]["status"] == "pending"

        _claim(client)
        second = _report(client, task_id, False, error_message="第二次失败")

        data = second.json()["data"]
        assert data["status"] == "failed"
        assert data["retry_count"] == 1
        assert data["error_message"] == "第二次失败"
        assert data["finished_at"] is not None

    def test_report_failure_without_retries_goes_straight_to_failed(
        self, client, created_content, created_account, create_task
    ):
        """max_retries=0 时首次失败即终态。"""
        task = create_task(created_content["id"], created_account["id"], max_retries=0)
        _claim(client)

        response = _report(client, task["id"], False, error_message="直接失败")

        assert response.json()["data"]["status"] == "failed"

    def test_report_failure_without_message_uses_default(
        self, client, created_content, created_account, create_task
    ):
        """未提供失败原因时应填入默认文案。"""
        task = create_task(created_content["id"], created_account["id"], max_retries=0)
        _claim(client)

        response = _report(client, task["id"], False)

        assert response.json()["data"]["error_message"] == "发布失败，未提供具体原因"

    def test_report_on_pending_task_returns_409(
        self, client, created_content, created_account, create_task
    ):
        """未被认领的任务不允许上报结果。"""
        task = create_task(created_content["id"], created_account["id"])

        response = _report(client, task["id"], True)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFLICT"

    def test_report_twice_returns_409(
        self, client, created_content, created_account, create_task
    ):
        """重复上报应被拒绝，避免终态被覆盖。"""
        task = create_task(created_content["id"], created_account["id"])
        _claim(client)
        _report(client, task["id"], True)

        response = _report(client, task["id"], False, error_message="迟到的失败")

        assert response.status_code == 409

    def test_report_missing_task_returns_404(self, client):
        """上报不存在的任务应返回 404。"""
        assert _report(client, 999999, True).status_code == 404


class TestCancelRetryDelete:
    """取消 / 重试 / 删除接口。"""

    def test_cancel_pending_task(self, client, created_content, created_account, create_task):
        """排队中的任务可以取消。"""
        task = create_task(created_content["id"], created_account["id"])

        response = client.post(f"/api/v1/publish-tasks/{task['id']}/cancel")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "cancelled"
        assert data["finished_at"] is not None

    def test_cancelled_task_is_not_claimed(
        self, client, created_content, created_account, create_task
    ):
        """已取消的任务不应再被认领。"""
        task = create_task(created_content["id"], created_account["id"])
        client.post(f"/api/v1/publish-tasks/{task['id']}/cancel")

        assert _claim(client) == []

    def test_cancel_terminal_task_returns_409(
        self, client, created_content, created_account, create_task
    ):
        """终态任务不可再取消。"""
        task = create_task(created_content["id"], created_account["id"])
        client.post(f"/api/v1/publish-tasks/{task['id']}/cancel")

        response = client.post(f"/api/v1/publish-tasks/{task['id']}/cancel")

        assert response.status_code == 409

    def test_retry_failed_task_resets_counters(
        self, client, created_content, created_account, create_task
    ):
        """重试失败任务应重置重试计数并清空失败信息。"""
        task = create_task(created_content["id"], created_account["id"], max_retries=0)
        _claim(client)
        _report(client, task["id"], False, error_message="失败了")

        response = client.post(f"/api/v1/publish-tasks/{task['id']}/retry")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "pending"
        assert data["retry_count"] == 0
        assert data["error_message"] == ""
        assert data["started_at"] is None

    def test_retried_task_can_be_claimed_again(
        self, client, created_content, created_account, create_task
    ):
        """重试后的任务应重新进入可认领队列。"""
        task = create_task(created_content["id"], created_account["id"], max_retries=0)
        _claim(client)
        _report(client, task["id"], False, error_message="失败了")
        client.post(f"/api/v1/publish-tasks/{task['id']}/retry")

        claimed = _claim(client)

        assert [item["id"] for item in claimed] == [task["id"]]

    def test_retry_pending_task_returns_409(
        self, client, created_content, created_account, create_task
    ):
        """排队中的任务不允许重试。"""
        task = create_task(created_content["id"], created_account["id"])

        response = client.post(f"/api/v1/publish-tasks/{task['id']}/retry")

        assert response.status_code == 409

    def test_delete_finished_task(self, client, created_content, created_account, create_task):
        """已结束的任务可以删除。"""
        task = create_task(created_content["id"], created_account["id"])
        client.post(f"/api/v1/publish-tasks/{task['id']}/cancel")

        response = client.delete(f"/api/v1/publish-tasks/{task['id']}")

        assert response.status_code == 200
        assert client.get(f"/api/v1/publish-tasks/{task['id']}").status_code == 404

    def test_delete_pending_task_returns_409(
        self, client, created_content, created_account, create_task
    ):
        """排队中的任务不允许直接删除。"""
        task = create_task(created_content["id"], created_account["id"])

        response = client.delete(f"/api/v1/publish-tasks/{task['id']}")

        assert response.status_code == 409

    def test_delete_missing_task_returns_404(self, client):
        """删除不存在的任务应返回 404。"""
        assert client.delete("/api/v1/publish-tasks/999999").status_code == 404


class TestTaskQuery:
    """列表、筛选与统计接口。"""

    def test_list_and_filter_by_status(
        self, client, created_content, create_account, create_task
    ):
        """列表应支持按状态过滤。"""
        first = create_account(nickname="账号甲")
        second = create_account(nickname="账号乙")
        create_task(created_content["id"], first["id"])
        cancelled = create_task(created_content["id"], second["id"])
        client.post(f"/api/v1/publish-tasks/{cancelled['id']}/cancel")

        response = client.get("/api/v1/publish-tasks", params={"status": "cancelled"})

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == cancelled["id"]

    def test_list_filter_by_account(self, client, created_content, create_account, create_task):
        """列表应支持按账号过滤。"""
        first = create_account(nickname="账号甲")
        second = create_account(nickname="账号乙")
        create_task(created_content["id"], first["id"])
        create_task(created_content["id"], second["id"])

        response = client.get(
            "/api/v1/publish-tasks", params={"account_id": first["id"]}
        )

        assert response.status_code == 200
        assert response.json()["data"]["total"] == 1

    def test_statistics_counts_by_status(
        self, client, created_content, create_account, create_task
    ):
        """统计接口应返回总数与各状态分布，未出现的状态补 0。"""
        first = create_account(nickname="账号甲")
        second = create_account(nickname="账号乙")
        create_task(created_content["id"], first["id"])
        cancelled = create_task(created_content["id"], second["id"])
        client.post(f"/api/v1/publish-tasks/{cancelled['id']}/cancel")

        response = client.get("/api/v1/publish-tasks/statistics")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        assert data["by_status"]["pending"] == 1
        assert data["by_status"]["cancelled"] == 1
        assert data["by_status"]["running"] == 0
        assert data["by_status"]["success"] == 0
        assert data["by_status"]["failed"] == 0

    def test_statistics_route_not_shadowed_by_path_param(self, client):
        """/statistics 不应被 /{task_id} 路由吞掉（回归测试）。"""
        response = client.get("/api/v1/publish-tasks/statistics")

        assert response.status_code == 200
        assert "by_status" in response.json()["data"]


class TestContentDeleteProtection:
    """内容删除保护：未完成任务存在时不允许删除内容。"""

    def test_delete_content_blocked_by_pending_task(
        self, client, created_content, created_account
    ):
        """内容仍有排队中的任务时应拒绝删除（409）。"""
        client.post(
            "/api/v1/publish-tasks",
            json={"content_id": created_content["id"], "account_id": created_account["id"]},
        )

        response = client.delete(f"/api/v1/contents/{created_content['id']}")

        assert response.status_code == 409
        assert "未完成的发布任务" in response.json()["error"]["message"]

    def test_delete_content_after_task_finished_still_blocked_by_history(
        self, client, created_content, created_account, create_task
    ):
        """任务结束后受外键约束保护，内容同样不可删除（409）。"""
        task = create_task(created_content["id"], created_account["id"])
        client.post(f"/api/v1/publish-tasks/{task['id']}/cancel")

        response = client.delete(f"/api/v1/contents/{created_content['id']}")

        assert response.status_code == 409
        assert "发布任务记录" in response.json()["error"]["message"]
