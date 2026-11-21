"""受控联邦数据分发领域服务。

从联邦查询缓存（result_cache）的多来源明细中，按来源归属切分可共享明细，
为接收方提供获准来源范围内的只读数据。核心规则：

1. 一行若合用了两个及以上来源才能算出金额，则这些来源必须全部获许可，
   否则该行不可共享；由这些行计算的分组小计同样剔除，防止用小计反推。
2. 许可（授权）以版本快照形式保存，变更历史可按发布批次核对。
3. 交付预检与实际领取都以「领取时刻」接收方的有效授权为准，
   生成期间来源撤回授权会产生覆盖冲突，领取时只交付仍可归属的明细。
4. 历史交付保留授权版本与实际数据列快照；从原全量缓存重新生成时仍按接收方核权。
"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _row_sources(row: dict[str, Any]) -> list[str]:
    """读取一行的来源归属；兼容单 source 字符串与多 sources 列表两种缓存形态。"""
    sources = row.get("sources")
    if isinstance(sources, list) and sources:
        return [str(s) for s in sources]
    single = row.get("source")
    if single:
        return [str(single)]
    return []


def _row_value(row: dict[str, Any]) -> float:
    try:
        return float(row.get("value", 0))
    except (TypeError, ValueError):
        return 0.0


class FedDispatchService(BaseModuleService):
    MODULE = "fed_dispatch"
    ENTITY = "分发批次"
    OPERATION_TERM = "分发"
    LIST_FIELDS = [
        "批次编号", "接收方", "缓存条目", "申请来源", "许可版本",
        "获准来源", "只读列", "发布时间",
    ]
    REQUIRED_FIELDS = ["批次编号", "接收方", "缓存条目"]
    STATUS_ORDER = ["待发布", "预检通过", "覆盖冲突", "已领取", "已撤回"]
    ACTION_RULES = {"撤回批次": "已撤回"}
    NEGATIVE_ACTIONS = ["撤回批次"]

    # 接收方领取明细时不返回的内部/技术列（来源归属与技术标记不进入只读交付列）。
    HIDDEN_COLUMNS = {"sources", "source", "strategy", "timestamp"}

    # ---------- 申请与许可 ----------

    def _get_batch(self, batch_id: int) -> dict[str, Any] | None:
        entry = store.find(self.MODULE, batch_id)
        if entry is not None:
            self._ensure_extensions(entry)
        return entry

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = self._get_batch(entry_id)
        if entry is None:
            return None
        return self.public_view(entry)

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        for row in store.rows(self.MODULE):
            self._ensure_extensions(row)
        items, total = super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size
        )
        return [self.public_view(item) for item in items], total

    def _ensure_extensions(self, entry: dict[str, Any]) -> None:
        """补齐种子行缺失的领域扩展字段，避免历史登记数据无法走分发流程。"""
        if "license" not in entry:
            granted = [s.strip() for s in str(entry.get("获准来源", "")).split(",") if s.strip()]
            entry["license"] = {
                "version": int(str(entry.get("许可版本", "v1")).lstrip("v") or 1),
                "granted_sources": granted,
                "updated_at": _now(),
                "history": [
                    {"version": 1, "action": "授权建立", "sources": granted, "at": _now()}
                ],
            }
        entry.setdefault("cache_id", 0)
        entry.setdefault("query_summary", "")
        entry.setdefault("visible_columns",
                         [c.strip() for c in str(entry.get("只读列", "")).split(",") if c.strip()])
        entry.setdefault("coverage_conflict", False)
        entry.setdefault("deliveries", [])

    def create_application(
        self,
        receiver: str,
        cache_id: int,
        requested_sources: list[str],
        visible_columns: list[str] | None = None,
    ) -> dict[str, Any]:
        """创建分发申请：申请人能看到查询摘要，接收方与获准来源在此绑定。"""
        cache = store.find("result_cache", cache_id)
        if cache is None:
            return {"ok": False, "message": f"缓存条目 {cache_id} 不存在，无法发起分发"}
        detail = cache.get("result_data", cache.get("cached_data", []))
        if not isinstance(detail, list) or not detail:
            return {"ok": False, "message": "联邦查询缓存没有多来源明细，无法切分"}

        cache_sources = sorted({s for row in detail for s in _row_sources(row)})
        unknown = [s for s in requested_sources if s not in cache_sources]
        if unknown:
            return {"ok": False, "message": f"申请来源不在缓存明细归属内：{', '.join(unknown)}"}

        rows = store.rows(self.MODULE)
        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        columns = visible_columns or self._default_columns(detail)

        entry: dict[str, Any] = {
            "id": entry_id,
            "批次编号": f"FEDI-{entry_id:04d}",
            "接收方": receiver,
            "缓存条目": cache.get("条目编号", f"cache:{cache_id}"),
            "申请来源": ", ".join(requested_sources),
            "许可版本": "v1",
            "获准来源": ", ".join(requested_sources),
            "只读列": ", ".join(columns),
            "发布时间": "-",
            "status": self.STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            # —— 领域扩展字段 ——
            "cache_id": cache_id,
            "query_summary": cache.get("查询摘要", ""),
            "visible_columns": columns,
            "coverage_conflict": False,
            "deliveries": [],
        }
        # 许可版本快照与变更历史
        entry["license"] = {
            "version": 1,
            "granted_sources": list(requested_sources),
            "updated_at": _now(),
            "history": [
                {
                    "version": 1,
                    "action": "授权建立",
                    "sources": list(requested_sources),
                    "at": _now(),
                }
            ],
        }
        rows.append(entry)
        return {
            "ok": True,
            "application": self.public_view(entry),
            "query_summary": cache.get("查询摘要", ""),
            "cache_sources": cache_sources,
        }

    def update_license(
        self, batch_id: int, granted_sources: list[str], action: str = "授权变更"
    ) -> dict[str, Any]:
        """许可变更：写入新版本快照并追加历史，历史可按发布批次核对。"""
        entry = self._get_batch(batch_id)
        if entry is None:
            return {"ok": False, "message": "分发批次不存在"}
        cache = store.find("result_cache", entry["cache_id"])
        detail = cache.get("result_data", []) if cache else []
        valid = sorted({s for row in detail for s in _row_sources(row)})
        bad = [s for s in granted_sources if s not in valid]
        if bad:
            return {"ok": False, "message": f"来源不在缓存归属内：{', '.join(bad)}"}

        license_obj = entry["license"]
        new_version = int(license_obj["version"]) + 1
        license_obj["version"] = new_version
        license_obj["granted_sources"] = list(granted_sources)
        license_obj["updated_at"] = _now()
        license_obj["history"].append(
            {
                "version": new_version,
                "action": action,
                "sources": list(granted_sources),
                "at": _now(),
            }
        )
        entry["许可版本"] = f"v{new_version}"
        entry["获准来源"] = ", ".join(granted_sources)
        return {"ok": True, "license": license_obj, "batch_id": batch_id}

    def revoke_source(self, batch_id: int, source: str) -> dict[str, Any]:
        """撤回某来源授权：从当前获准来源中剔除并产生新版本。"""
        entry = self._get_batch(batch_id)
        if entry is None:
            return {"ok": False, "message": "分发批次不存在"}
        current = list(entry["license"]["granted_sources"])
        if source not in current:
            return {"ok": False, "message": f"来源 {source} 本就不在授权范围内"}
        current.remove(source)
        return self.update_license(batch_id, current, action=f"撤回授权:{source}")

    def license_history(self, batch_id: int) -> dict[str, Any]:
        entry = self._get_batch(batch_id)
        if entry is None:
            return {"ok": False, "message": "分发批次不存在"}
        return {
            "ok": True,
            "batch_id": batch_id,
            "批次编号": entry["批次编号"],
            "current_version": f"v{entry['license']['version']}",
            "history": entry["license"]["history"],
        }

    # ---------- 切分、预检、发布 ----------

    def slice_batch(self, batch_id: int) -> dict[str, Any]:
        """按当前许可版本从全量缓存切分可共享明细并重算分组小计。

        始终从原全量缓存重新过滤，保证重新生成时仍按接收方当前授权核权。
        """
        entry = self._get_batch(batch_id)
        if entry is None:
            return {"ok": False, "message": "分发批次不存在"}
        cache = store.find("result_cache", entry["cache_id"])
        if cache is None:
            return {"ok": False, "message": "原全量缓存已不存在，无法切分"}
        detail = cache.get("result_data", cache.get("cached_data", []))
        granted = set(entry["license"]["granted_sources"])

        shareable: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for row in detail:
            provenance = _row_sources(row)
            if provenance and set(provenance).issubset(granted):
                shareable.append(self._project_row(row, entry["visible_columns"]))
            else:
                excluded.append(
                    {
                        "row_id": row.get("row_id"),
                        "sources": provenance,
                        "reason": "存在未获许可的合用来源" if len(provenance) > 1 else "来源未获许可",
                    }
                )

        subtotals = self._subtotals(shareable)
        return {
            "ok": True,
            "license_version": f"v{entry['license']['version']}",
            "granted_sources": sorted(granted),
            "shareable_rows": shareable,
            "excluded_rows": excluded,
            "subtotals": subtotals,
            "total_cache_rows": len(detail),
        }

    def precheck(self, batch_id: int) -> dict[str, Any]:
        """交付预检：以当前授权重算可交付范围，识别生成期间的覆盖冲突。"""
        entry = self._get_batch(batch_id)
        if entry is None:
            return {"ok": False, "message": "分发批次不存在"}
        sliced = self.slice_batch(batch_id)
        requested = {s.strip() for s in entry.get("申请来源", "").split(",") if s.strip()}
        granted = set(entry["license"]["granted_sources"])
        revoked = sorted(requested - granted)

        # 继续交付当前可归属范围 vs 维护原申请完整覆盖之间的冲突
        conflict = bool(revoked) or len(sliced["excluded_rows"]) > 0
        entry["coverage_conflict"] = conflict
        entry["abnormal"] = conflict
        # 已领取/已撤回是终态：预检只更新冲突标记，不回退批次状态
        if entry["status"] not in ("已领取", "已撤回"):
            entry["status"] = "覆盖冲突" if conflict else "预检通过"
        entry["pending"] = entry["status"] not in ("已领取", "已撤回")
        return {
            "ok": True,
            "batch_id": batch_id,
            "license_version": f"v{entry['license']['version']}",
            "requested_sources": sorted(requested),
            "granted_sources": sorted(granted),
            "revoked_sources": revoked,
            "deliverable_rows": len(sliced["shareable_rows"]),
            "excluded_rows": sliced["excluded_rows"],
            "subtotals": sliced["subtotals"],
            "coverage_conflict": conflict,
            "message": "存在授权撤回，交付范围已收窄" if conflict else "预检通过，可领取",
        }

    def publish_batch(self, batch_id: int) -> dict[str, Any]:
        """发布批次：记录发布时间与发布时许可版本。"""
        entry = self._get_batch(batch_id)
        if entry is None:
            return {"ok": False, "message": "分发批次不存在"}
        entry["发布时间"] = _now()
        if entry["status"] == "待发布":
            entry["status"] = "预检通过"
        return {"ok": True, "batch_id": batch_id, "published_at": entry["发布时间"]}

    # ---------- 领取（按领取时授权） ----------

    def claim(self, batch_id: int) -> dict[str, Any]:
        """接收方领取：实际可读的行与小计均符合领取时授权，并留存历史交付快照。"""
        entry = self._get_batch(batch_id)
        if entry is None:
            return {"ok": False, "message": "分发批次不存在"}
        if entry["status"] == "已撤回":
            return {"ok": False, "message": "批次已撤回，不可领取"}

        # 关键：领取时刻重新核权，而不是复用预检时刻的结果
        sliced = self.slice_batch(batch_id)
        granted = set(entry["license"]["granted_sources"])
        requested = {s.strip() for s in entry.get("申请来源", "").split(",") if s.strip()}
        revoked = sorted(requested - granted)

        delivery = {
            "delivery_id": uuid.uuid4().hex[:10],
            "claimed_at": _now(),
            "license_version": f"v{entry['license']['version']}",
            "granted_sources": sorted(granted),
            "revoked_sources": revoked,
            "columns": list(entry["visible_columns"]),
            "rows": sliced["shareable_rows"],
            "subtotals": sliced["subtotals"],
            "row_count": len(sliced["shareable_rows"]),
            "checksum": hashlib.md5(
                str(sliced["shareable_rows"]).encode()
            ).hexdigest()[:12],
        }
        entry["deliveries"].append(delivery)
        entry["status"] = "已领取"
        entry["pending"] = False
        entry["coverage_conflict"] = bool(revoked)
        entry["abnormal"] = bool(revoked)
        return {"ok": True, "delivery": delivery, "coverage_conflict": bool(revoked)}

    def delivery_history(self, batch_id: int) -> dict[str, Any]:
        """历史交付：保留每次领取的授权版本与实际数据列。"""
        entry = self._get_batch(batch_id)
        if entry is None:
            return {"ok": False, "message": "分发批次不存在"}
        return {
            "ok": True,
            "batch_id": batch_id,
            "批次编号": entry["批次编号"],
            "deliveries": entry.get("deliveries", []),
        }

    # ---------- 分析导出对接 ----------

    def approved_delivery_payload(self, batch_id: int) -> dict[str, Any] | None:
        """供分析导出读取：返回批次获准交付的行和列（不改变批次状态）。"""
        entry = self._get_batch(batch_id)
        if entry is None:
            return None
        sliced = self.slice_batch(batch_id)
        return {
            "batch_id": batch_id,
            "批次编号": entry["批次编号"],
            "license_version": f"v{entry['license']['version']}",
            "columns": list(entry["visible_columns"]),
            "rows": sliced["shareable_rows"],
            "subtotals": sliced["subtotals"],
        }

    # ---------- 视图与辅助 ----------

    def public_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        """申请人/列表视图：展示查询摘要与许可信息，不直接暴露明细数据。"""
        view = {k: v for k, v in entry.items() if k not in ("deliveries",)}
        view["license_version"] = f"v{entry['license']['version']}"
        view["granted_sources"] = list(entry["license"]["granted_sources"])
        return view

    def _project_row(self, row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
        """投影到只读列，并附带来源归属用于内部核权（领取视图可再过滤）。"""
        projected = {col: row.get(col) for col in columns if col in row}
        projected["row_id"] = row.get("row_id")
        projected["_sources"] = _row_sources(row)
        return projected

    def _default_columns(self, detail: list[dict[str, Any]]) -> list[str]:
        columns: list[str] = []
        for row in detail:
            for key in row.keys():
                if key not in self.HIDDEN_COLUMNS and key not in columns:
                    columns.append(key)
        return columns

    def _subtotals(self, shareable: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """分组小计只由当前可共享的行重算，避免反推被剔除来源的金额。"""
        groups: dict[str, float] = {}
        counts: dict[str, int] = {}
        for row in shareable:
            category = str(row.get("category", "未分组"))
            groups[category] = groups.get(category, 0.0) + _row_value(row)
            counts[category] = counts.get(category, 0) + 1
        return [
            {
                "category": category,
                "row_count": counts[category],
                "subtotal": round(groups[category], 2),
            }
            for category in sorted(groups)
        ]
