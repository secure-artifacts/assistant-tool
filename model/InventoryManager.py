import json
import math
import os
import time
import uuid
from pathlib import Path

from app_paths import APP_ROOT

DEFAULT_INVENTORY_STATE_FILE = APP_ROOT / "InventoryState.json"
SECONDS_PER_DAY = 24 * 60 * 60

STATUS_NORMAL = "normal"
STATUS_INITIAL = "initial"
STATUS_MODERATE = "moderate"
STATUS_CRITICAL = "critical"

STATUS_RANK = {
    STATUS_NORMAL: 0,
    STATUS_INITIAL: 1,
    STATUS_MODERATE: 2,
    STATUS_CRITICAL: 3,
}

STATUS_LABELS = {
    STATUS_NORMAL: "充足",
    STATUS_INITIAL: "初步报警",
    STATUS_MODERATE: "中度报警",
    STATUS_CRITICAL: "高危报警",
}


def _number(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(result):
        return float(default)
    return max(0.0, result)


def current_quantity(item, now=None):
    now = time.time() if now is None else float(now)
    quantity = _number(item.get("quantity"))
    daily_usage = _number(item.get("daily_usage"))
    updated_at = _number(item.get("updated_at"), now)
    elapsed = max(0.0, now - updated_at)
    return max(0.0, quantity - daily_usage * elapsed / SECONDS_PER_DAY)


def item_status(item, now=None):
    quantity = current_quantity(item, now)
    daily_usage = _number(item.get("daily_usage"))
    if quantity <= 1e-9:
        return STATUS_CRITICAL
    if daily_usage <= 1e-9:
        return STATUS_NORMAL
    days_left = quantity / daily_usage
    if days_left < 1:
        return STATUS_MODERATE
    if days_left < 2:
        return STATUS_INITIAL
    return STATUS_NORMAL


def inventory_summary(items, now=None):
    now = time.time() if now is None else float(now)
    counts = {status: 0 for status in STATUS_RANK}
    evaluated = []
    worst = STATUS_NORMAL
    for item in items:
        row = dict(item)
        row["current_quantity"] = current_quantity(item, now)
        daily_usage = _number(item.get("daily_usage"))
        row["days_left"] = (
            row["current_quantity"] / daily_usage
            if daily_usage > 1e-9
            else math.inf
        )
        row["status"] = item_status(item, now)
        counts[row["status"]] += 1
        if STATUS_RANK[row["status"]] > STATUS_RANK[worst]:
            worst = row["status"]
        evaluated.append(row)
    return {"items": evaluated, "counts": counts, "worst": worst}


class InventoryStore:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_INVENTORY_STATE_FILE)

    def _default_state(self):
        return {"version": 1, "items": []}

    def load(self):
        if not self.path.exists():
            return self._default_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("库存记录无法读取：{}".format(error)) from error
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise ValueError("库存记录格式不正确")
        items = []
        now = time.time()
        for raw in data.get("items", []):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            if not name:
                continue
            items.append(
                {
                    "id": str(raw.get("id") or uuid.uuid4().hex),
                    "name": name,
                    "quantity": _number(raw.get("quantity")),
                    "daily_usage": _number(raw.get("daily_usage")),
                    "updated_at": _number(raw.get("updated_at"), now),
                }
            )
        return {"version": 1, "items": items}

    def _write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        try:
            temp_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(temp_path), str(self.path))
        except OSError:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise

    def list_items(self):
        return self.load()["items"]

    def _settle(self, items, now):
        settled = []
        for item in items:
            row = dict(item)
            row["quantity"] = current_quantity(item, now)
            row["updated_at"] = now
            settled.append(row)
        return settled

    def add_item(self, name, quantity, daily_usage):
        name = str(name or "").strip()
        if not name:
            raise ValueError("库存名称不能为空")
        state = self.load()
        if any(item["name"].casefold() == name.casefold() for item in state["items"]):
            raise ValueError("已经有同名库存：{}".format(name))
        now = time.time()
        state["items"] = self._settle(state["items"], now)
        item = {
            "id": uuid.uuid4().hex,
            "name": name,
            "quantity": _number(quantity),
            "daily_usage": _number(daily_usage),
            "updated_at": now,
        }
        state["items"].append(item)
        self._write(state)
        return item

    def update_item(self, item_id, name, quantity, daily_usage):
        name = str(name or "").strip()
        if not name:
            raise ValueError("库存名称不能为空")
        state = self.load()
        now = time.time()
        state["items"] = self._settle(state["items"], now)
        if any(
            item["id"] != item_id and item["name"].casefold() == name.casefold()
            for item in state["items"]
        ):
            raise ValueError("已经有同名库存：{}".format(name))
        for item in state["items"]:
            if item["id"] == item_id:
                item.update(
                    name=name,
                    quantity=_number(quantity),
                    daily_usage=_number(daily_usage),
                    updated_at=now,
                )
                self._write(state)
                return item
        raise KeyError("找不到要修改的库存")

    def add_stock(self, item_id, amount):
        amount = _number(amount)
        if amount <= 0:
            raise ValueError("补充数量必须大于 0")
        state = self.load()
        now = time.time()
        state["items"] = self._settle(state["items"], now)
        for item in state["items"]:
            if item["id"] == item_id:
                item["quantity"] += amount
                self._write(state)
                return item
        raise KeyError("找不到要补充的库存")

    def delete_item(self, item_id):
        state = self.load()
        before = len(state["items"])
        state["items"] = [item for item in state["items"] if item["id"] != item_id]
        if len(state["items"]) == before:
            raise KeyError("找不到要删除的库存")
        self._write(state)

    def summary(self, now=None):
        return inventory_summary(self.list_items(), now)
