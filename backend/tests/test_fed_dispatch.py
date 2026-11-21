"""受控联邦数据分发批次测试。

覆盖核心场景：
- 从联邦查询缓存的多来源明细按归属切分可共享明细；
- 合用两个来源的行在任一来源未授权时不可共享，分组小计同步剔除、无法反推；
- 许可变更历史按发布批次核对；
- 生成期间撤回授权后，领取时刻按当前授权重新核权；
- 历史交付保留授权版本与实际数据列；
- 分析导出只读取获准交付的行和列并回写许可版本；
- 从原全量缓存重新生成仍按接收方核权。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store
from app.services.fed_dispatch import FedDispatchService
from app.services.data_exporter import DataExporterService

client = TestClient(app)


@pytest.fixture
def multi_source_cache():
    """构造一份确定性的多来源联邦查询缓存：含单来源行与 A+B 合用行。"""
    caches = store.rows("result_cache")
    cache_id = max((int(r.get("id", 0)) for r in caches), default=0) + 1
    detail = [
        {"row_id": 1, "sources": ["SRC-A"], "value": 100, "category": "cat_x"},
        {"row_id": 2, "sources": ["SRC-A"], "value": 200, "category": "cat_y"},
        {"row_id": 3, "sources": ["SRC-B"], "value": 300, "category": "cat_x"},
        {"row_id": 4, "sources": ["SRC-B"], "value": 400, "category": "cat_y"},
        # 合用 A、B 两个来源才能算出金额的行
        {"row_id": 5, "sources": ["SRC-A", "SRC-B"], "value": 50, "category": "cat_x"},
        {"row_id": 6, "sources": ["SRC-A", "SRC-B"], "value": 70, "category": "cat_y"},
    ]
    caches.append({
        "id": cache_id,
        "条目编号": f"RESU-MULTI-{cache_id}",
        "查询摘要": f"multi_source_federated_cache_{cache_id}",
        "结果哈希": "deterministic",
        "缓存命中": "0",
        "过期时间": "2027-01-01",
        "存储大小": "0",
        "创建时间": "2026-11-21",
        "最后命中": "2026-11-21",
        "status": "有效",
        "pending": False,
        "abnormal": False,
        "result_data": detail,
    })
    return cache_id, detail


@pytest.fixture
def svc():
    return FedDispatchService()


class TestApplicationAndSlicing:
    def test_apply_and_public_view_keeps_query_summary(self, svc, multi_source_cache):
        cache_id, detail = multi_source_cache
        result = svc.create_application("接收方甲", cache_id, ["SRC-A", "SRC-B"])
        assert result["ok"]
        # 申请人能看查询摘要
        assert result["query_summary"] == f"multi_source_federated_cache_{cache_id}"
        app_view = result["application"]
        assert app_view["接收方"] == "接收方甲"
        assert set(app_view["granted_sources"]) == {"SRC-A", "SRC-B"}
        # 列表视图不直接带出明细
        assert "rows" not in app_view

    def test_unknown_source_rejected(self, svc, multi_source_cache):
        cache_id, _ = multi_source_cache
        result = svc.create_application("接收方甲", cache_id, ["SRC-X"])
        assert not result["ok"]

    def test_slice_with_both_licenses_includes_dual_source_rows(self, svc, multi_source_cache):
        cache_id, _ = multi_source_cache
        app = svc.create_application("接收方甲", cache_id, ["SRC-A", "SRC-B"])
        batch_id = app["application"]["id"]
        sliced = svc.slice_batch(batch_id)
        assert sliced["ok"]
        assert len(sliced["shareable_rows"]) == 6
        assert sliced["excluded_rows"] == []
        # 小计覆盖全部行
        subtotal_sum = sum(s["subtotal"] for s in sliced["subtotals"])
        assert subtotal_sum == 1120

    def test_only_a_license_excludes_b_and_dual_rows(self, svc, multi_source_cache):
        cache_id, _ = multi_source_cache
        app = svc.create_application("接收方甲", cache_id, ["SRC-A", "SRC-B"])
        batch_id = app["application"]["id"]
        svc.update_license(batch_id, ["SRC-A"])
        sliced = svc.slice_batch(batch_id)
        shareable_ids = {r["row_id"] for r in sliced["shareable_rows"]}
        # 只有纯 A 行可共享；纯 B 行与 A+B 合用行都被剔除
        assert shareable_ids == {1, 2}
        excluded_ids = {r["row_id"] for r in sliced["excluded_rows"]}
        assert excluded_ids == {3, 4, 5, 6}
        # 小计只由可共享行重算，无法反推被剔除来源金额
        subtotal_sum = sum(s["subtotal"] for s in sliced["subtotals"])
        assert subtotal_sum == 300
        cat_x = next(s for s in sliced["subtotals"] if s["category"] == "cat_x")
        assert cat_x["subtotal"] == 100  # 不含 B 的 300，也不含合用行的 50

    def test_read_only_columns_projected(self, svc, multi_source_cache):
        cache_id, _ = multi_source_cache
        app = svc.create_application(
            "接收方甲", cache_id, ["SRC-A"], visible_columns=["row_id", "category", "value"]
        )
        batch_id = app["application"]["id"]
        sliced = svc.slice_batch(batch_id)
        row = sliced["shareable_rows"][0]
        assert set(row.keys()) <= {"row_id", "category", "value", "_sources"}
        # 内部来源归属列存在于服务端核权结构，但不是只读交付列
        assert "_sources" in row


class TestLicenseHistory:
    def test_revoke_creates_versioned_history(self, svc, multi_source_cache):
        cache_id, _ = multi_source_cache
        app = svc.create_application("接收方甲", cache_id, ["SRC-A", "SRC-B"])
        batch_id = app["application"]["id"]
        svc.revoke_source(batch_id, "SRC-B")
        history = svc.license_history(batch_id)
        assert history["ok"]
        versions = [h["version"] for h in history["history"]]
        assert versions == [1, 2]
        assert history["history"][1]["action"] == "撤回授权:SRC-B"
        assert history["current_version"] == "v2"
        entry = store.find("fed_dispatch", batch_id)
        assert entry["许可版本"] == "v2"
        assert entry["获准来源"] == "SRC-A"

    def test_revoke_unlicensed_source_rejected(self, svc, multi_source_cache):
        cache_id, _ = multi_source_cache
        app = svc.create_application("接收方甲", cache_id, ["SRC-A"])
        batch_id = app["application"]["id"]
        result = svc.revoke_source(batch_id, "SRC-B")
        assert not result["ok"]


class TestPrecheckClaimAndRevocation:
    def test_precheck_flags_coverage_conflict_after_revoke(self, svc, multi_source_cache):
        cache_id, _ = multi_source_cache
        app = svc.create_application("接收方甲", cache_id, ["SRC-A", "SRC-B"])
        batch_id = app["application"]["id"]
        before = svc.precheck(batch_id)
        assert before["ok"] and not before["coverage_conflict"]
        assert before["deliverable_rows"] == 6

        svc.revoke_source(batch_id, "SRC-B")
        after = svc.precheck(batch_id)
        # 继续交付 A 与维护原申请完整覆盖发生冲突
        assert after["coverage_conflict"]
        assert after["revoked_sources"] == ["SRC-B"]
        assert after["deliverable_rows"] == 2
        entry = store.find("fed_dispatch", batch_id)
        assert entry["status"] == "覆盖冲突"

    def test_claim_uses_authorization_at_claim_time(self, svc, multi_source_cache):
        """B 在生成交付物期间撤回：领取时只含仍可归属的明细。"""
        cache_id, _ = multi_source_cache
        app = svc.create_application("接收方甲", cache_id, ["SRC-A", "SRC-B"])
        batch_id = app["application"]["id"]
        svc.precheck(batch_id)
        svc.publish_batch(batch_id)
        # 生成期间撤回 B
        svc.revoke_source(batch_id, "SRC-B")

        claim = svc.claim(batch_id)
        assert claim["ok"]
        delivery = claim["delivery"]
        assert delivery["license_version"] == "v2"
        assert delivery["granted_sources"] == ["SRC-A"]
        assert delivery["revoked_sources"] == ["SRC-B"]
        ids = {r["row_id"] for r in delivery["rows"]}
        assert ids == {1, 2}
        # 小计无法反推 B 的金额：小计仅等于已交付 A 行之和
        assert sum(s["subtotal"] for s in delivery["subtotals"]) == 300
        entry = store.find("fed_dispatch", batch_id)
        assert entry["status"] == "已领取"

    def test_delivery_history_preserves_version_and_columns(self, svc, multi_source_cache):
        cache_id, _ = multi_source_cache
        app = svc.create_application(
            "接收方甲", cache_id, ["SRC-A", "SRC-B"],
            visible_columns=["row_id", "category", "value"],
        )
        batch_id = app["application"]["id"]
        svc.claim(batch_id)
        svc.revoke_source(batch_id, "SRC-B")
        svc.claim(batch_id)

        history = svc.delivery_history(batch_id)
        assert len(history["deliveries"]) == 2
        first, second = history["deliveries"]
        # 历史交付保留授权版本
        assert first["license_version"] == "v1"
        assert second["license_version"] == "v2"
        # 历史交付保留实际数据列
        assert first["columns"] == ["row_id", "category", "value"]
        assert len(first["rows"]) == 6
        assert len(second["rows"]) == 2
        assert first["checksum"] != second["checksum"]

    def test_regenerate_from_full_cache_re_checks_authorization(self, svc, multi_source_cache):
        """从原全量缓存重新生成时仍按接收方核权。"""
        cache_id, detail = multi_source_cache
        app = svc.create_application("接收方甲", cache_id, ["SRC-A", "SRC-B"])
        batch_id = app["application"]["id"]
        first = svc.slice_batch(batch_id)
        assert len(first["shareable_rows"]) == len(detail)

        svc.revoke_source(batch_id, "SRC-B")
        # 重新切分必须重新读全量缓存并按当前授权过滤，而不是复用旧结果
        regenerated = svc.slice_batch(batch_id)
        assert len(regenerated["shareable_rows"]) == 2
        assert regenerated["total_cache_rows"] == len(detail)
        assert regenerated["license_version"] == "v2"

    def test_revoked_batch_cannot_claim(self, svc, multi_source_cache):
        cache_id, _ = multi_source_cache
        app = svc.create_application("接收方甲", cache_id, ["SRC-A"])
        batch_id = app["application"]["id"]
        entry = store.find("fed_dispatch", batch_id)
        entry["status"] = "已撤回"
        result = svc.claim(batch_id)
        assert not result["ok"]


class TestExporterIntegration:
    def test_export_reads_only_approved_rows_and_writes_back_license(
        self, svc, multi_source_cache
    ):
        cache_id, _ = multi_source_cache
        app = svc.create_application("接收方甲", cache_id, ["SRC-A", "SRC-B"])
        batch_id = app["application"]["id"]
        svc.revoke_source(batch_id, "SRC-B")
        svc.precheck(batch_id)

        exporter = DataExporterService()
        task, missing = exporter.create_entry({
            "任务编号": "EXPORT-DISPATCH-1", "数据来源": f"fed_dispatch:{batch_id}",
            "筛选条件": "licensed_only", "输出格式": "json", "字段列表": "*",
            "排序规则": "id", "导出上限": "10000", "接收地址": "local://exports",
        })
        assert task is not None, missing
        result = exporter.export_dispatch_batch(task["id"], batch_id, svc)
        assert result["ok"]
        assert result["license_version"] == "v2"
        assert result["row_count"] == 2
        assert set(result["columns"]) == {"row_id", "category", "value"}

        # 回写申请清单并注明许可版本
        task_entry = store.find("data_exporter", task["id"])
        assert task_entry["许可版本"] == "v2"
        assert f"fed_dispatch:{batch_id}@v2" == task_entry["数据来源"]
        batch = store.find("fed_dispatch", batch_id)
        assert batch["最近导出许可版本"] == "v2"
        assert batch["export_records"][0]["row_count"] == 2

    def test_export_after_further_revoke_shrinks_again(self, svc, multi_source_cache):
        cache_id, _ = multi_source_cache
        app = svc.create_application("接收方甲", cache_id, ["SRC-A", "SRC-B"])
        batch_id = app["application"]["id"]
        svc.revoke_source(batch_id, "SRC-B")
        exporter = DataExporterService()
        task, _ = exporter.create_entry({
            "任务编号": "EXPORT-DISPATCH-2", "数据来源": f"fed_dispatch:{batch_id}",
            "筛选条件": "licensed_only", "输出格式": "csv", "字段列表": "*",
            "排序规则": "id", "导出上限": "10000", "接收地址": "local://exports",
        })
        first = exporter.export_dispatch_batch(task["id"], batch_id, svc)
        assert first["row_count"] == 2

        svc.revoke_source(batch_id, "SRC-A")
        second = exporter.export_dispatch_batch(task["id"], batch_id, svc)
        # 全部来源撤回后没有可交付明细
        assert not second["ok"]


class TestApiEndpoints:
    def test_lifecycle_pipeline_endpoint(self, multi_source_cache):
        cache_id, _ = multi_source_cache
        resp = client.post(
            "/api/pipelines/fed-dispatch-lifecycle",
            params={
                "cache_id": cache_id,
                "receiver": "API接收方",
                "sources": "SRC-A,SRC-B",
                "revoke_source": "SRC-B",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"]
        assert data["precheck_before_revoke"]["deliverable_rows"] == 6
        assert data["precheck_after_revoke"]["coverage_conflict"] is True
        claim_rows = {r["row_id"] for r in data["claim"]["delivery"]["rows"]}
        assert claim_rows == {1, 2}
        versions = [h["version"] for h in data["license_history"]["history"]]
        assert versions == [1, 2]

    def test_application_and_claim_endpoints(self, multi_source_cache):
        cache_id, _ = multi_source_cache
        resp = client.post("/api/fed_dispatch/applications", json={
            "receiver": "API接收方2", "cache_id": cache_id,
            "requested_sources": ["SRC-A", "SRC-B"],
        })
        assert resp.status_code == 200
        batch_id = resp.json()["application"]["id"]

        assert client.post(f"/api/fed_dispatch/{batch_id}/revoke",
                           json={"source": "SRC-B"}).status_code == 200
        precheck = client.post(f"/api/fed_dispatch/{batch_id}/precheck").json()
        assert precheck["deliverable_rows"] == 2
        claim = client.post(f"/api/fed_dispatch/{batch_id}/claim").json()
        assert {r["row_id"] for r in claim["delivery"]["rows"]} == {1, 2}

        history = client.get(f"/api/fed_dispatch/{batch_id}/license-history").json()
        assert len(history["history"]) == 2
        deliveries = client.get(f"/api/fed_dispatch/{batch_id}/deliveries").json()
        assert len(deliveries["deliveries"]) == 1

    def test_export_dispatch_pipeline_endpoint(self, multi_source_cache):
        cache_id, _ = multi_source_cache
        app_resp = client.post("/api/fed_dispatch/applications", json={
            "receiver": "API接收方3", "cache_id": cache_id,
            "requested_sources": ["SRC-A"],
        })
        batch_id = app_resp.json()["application"]["id"]
        resp = client.post("/api/pipelines/export-dispatch-batch",
                           params={"batch_id": batch_id, "export_no": "EXPORT-API-1",
                                   "fmt": "json"})
        assert resp.status_code == 200
        export = resp.json()["export"]
        assert export["row_count"] == 2
        assert export["license_version"] == "v1"

    def test_seed_module_listed(self):
        resp = client.get("/api/fed_dispatch")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["total"] >= 1
        assert any(item["批次编号"] == "FEDI-0001" for item in payload["items"])
