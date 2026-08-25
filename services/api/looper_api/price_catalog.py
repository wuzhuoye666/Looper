from __future__ import annotations

import csv
import re
import threading
from pathlib import Path

from looper_api.cloud_contracts import ApiModel

_REGION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]+$")

_STATIC_REGION_NAME_TO_ID: dict[str, str] = {
    "cn-zhongwei": "cn-zhongwei",
    "中国（香港）": "cn-hongkong",
    "华东 1 (杭州)": "cn-hangzhou",
    "华东 2 (上海)": "cn-shanghai",
    "华中 1 (武汉)": "cn-wuhan",
    "华北 1 (青岛)": "cn-qingdao",
    "华北 2 (北京)": "cn-beijing",
    "华北 3 (张家口)": "cn-zhangjiakou",
    "华北 5 (呼和浩特)": "cn-huhehaote",
    "华北 6 (乌兰察布)": "cn-wulanchabu",
    "华南 1 (深圳)": "cn-shenzhen",
    "华南 2 (河源)": "cn-heyuan",
    "华南 3 (广州)": "cn-guangzhou",
    "西南 1 (成都)": "cn-chengdu",
    "印度尼西亚 (雅加达)": "ap-southeast-5",
    "墨西哥": "mx-central-1",
    "巴西 (圣保罗)": "sa-east-1",
    "德国 (法兰克福)": "eu-central-1",
    "新加坡": "ap-southeast-1",
    "日本 (东京)": "ap-northeast-1",
    "法国 (巴黎)": "eu-west-1",
    "泰国 (曼谷)": "ap-southeast-7",
    "美国 (弗吉尼亚)": "us-east-1",
    "美国 (硅谷)": "us-west-1",
    "英国（伦敦）": "eu-west-2",
    "菲律宾 (马尼拉)": "ap-southeast-6",
    "阿联酋 (迪拜)": "me-central-1",
    "韩国 (首尔)": "ap-northeast-2",
    "马来西亚 (吉隆坡)": "ap-southeast-3",
    "马来西亚 (柔佛州)": "ap-southeast-7",
}


class PriceEntry(ApiModel):
    hourly_list: float
    monthly_list: float
    hourly_discounted: float
    monthly_discounted: float
    currency: str = "CNY"


class AlibabaPriceTable:
    """Read the Alibaba Cloud instance price CSV and index by (region, instance type)."""

    def __init__(self, csv_path: str | Path, region_id_by_name: dict[str, str] | None = None):
        self._csv_path = Path(csv_path)
        self._region_id_by_name = dict(region_id_by_name or {})
        self._index: dict[tuple[str, str], PriceEntry] = {}
        self._lock = threading.Lock()
        self._loaded = False

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            with open(self._csv_path, encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    if (row.get("系统") or "").strip().casefold() != "linux":
                        continue
                    instance_type = (row.get("实例规格") or "").strip()
                    if not instance_type:
                        continue
                    name = (row.get("地域") or "").strip()
                    region_id = self._region_id_by_name.get(name)
                    if region_id is None and _REGION_ID_PATTERN.match(name):
                        region_id = name
                    if not region_id:
                        continue
                    try:
                        hourly_list = float(row.get("按量目录价") or 0)
                        monthly_list = float(row.get("包月目录价") or 0)
                        hourly_discounted = float(row.get("按量折扣价") or 0)
                        monthly_discounted = float(row.get("包月折扣价") or 0)
                    except ValueError:
                        continue
                    if hourly_discounted <= 0:
                        hourly_discounted = hourly_list
                    if monthly_discounted <= 0:
                        monthly_discounted = monthly_list
                    key = (region_id.casefold(), instance_type.casefold())
                    self._index[key] = PriceEntry(
                        hourly_list=hourly_list,
                        monthly_list=monthly_list,
                        hourly_discounted=hourly_discounted,
                        monthly_discounted=monthly_discounted,
                    )
            self._loaded = True

    def get(self, region_id: str, instance_type: str) -> PriceEntry | None:
        self.load()
        return self._index.get((region_id.casefold(), instance_type.casefold()))


def build_alibaba_region_map(provider: object) -> dict[str, str]:
    """Map the CSV region display name to the provider region id."""
    result: dict[str, str] = dict(_STATIC_REGION_NAME_TO_ID)
    regions = getattr(provider, "list_regions", None)
    if callable(regions):
        try:
            for region in regions() or []:
                name = getattr(region, "name", None)
                region_id = getattr(region, "id", None)
                if name and region_id:
                    result[str(name).strip()] = str(region_id)
        except Exception:
            pass
    return result