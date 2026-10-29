"""Map fine-grained industry labels to a consistent coarse sector.

The cninfo/SW industry strings mix levels ("金融" vs "证券", "银行" vs
"国有大型银行III"), which would fragment a cross-sectional neutralization into
hundreds of tiny buckets.  ``coarsen`` folds them into ~14 broad sectors so the
sector-neutral mean is computed on a meaningful number of names.
"""
from __future__ import annotations

import re
from typing import Dict

# Ordered: first match wins.  Keywords are matched as substrings.
_RULES = [
    ("金融", ("银行", "证券", "保险", "金融", "信托", "期货", "租赁", "资本市场", "财富管理", "多元金融", "商行")),
    ("TMT", ("半导体", "芯片", "集成电路", "电子", "元件", "光学", "光电子", "光电", "面板", "显示",
             "通信", "软件", "计算机", "IT", "互联网", "游戏", "传媒", "影视", "广告", "网络", "数据",
             "云计算", "人工智能", "信息技术", "信息", "分立器件", "印制电路板", "电信", "广电",
             "媒体", "电商", "视频", "出版", "文化")),
    ("医药", ("医药", "生物", "医疗", "制药", "疫苗", "中药", "制剂", "原料药", "器械", "诊断", "医美", "医院", "血液", "药店")),
    ("消费", ("食品", "饮料", "酿酒", "白酒", "啤酒", "乳品", "家电", "家用", "家居", "纺织", "服装",
             "服饰", "零售", "商业", "贸易", "旅游", "酒店", "免税", "餐饮", "美容", "个护", "文娱",
             "教育", "可选消费", "主要消费", "消费", "品牌", "电器", "厨房", "冰洗", "熟食",
             "调味", "粮油", "景区", "院线")),
    ("周期材料", ("化工", "化学", "材料", "钢铁", "有色", "金属", "煤炭", "石油", "炼化", "化纤", "建材",
                  "水泥", "玻璃", "造纸", "橡胶", "塑料", "非金属", "采掘", "贵金属", "锂", "稀有",
                  "钾肥", "磷", "焦炭", "特钢", "板材", "冶钢", "黄金", "肥料", "农药", "包装", "印刷", "肥", "硅",
                  "煤", "纸", "稀土", "钴", "钼", "铜", "铝", "钛", "金属", "轮毂")),
    ("公用事业", ("电力", "公用", "水务", "燃气", "环保", "水处理", "电网", "核电", "风电", "环境",
               "发电", "水电", "火电", "光伏", "逆变器")),
    ("制造", ("机械", "电气", "军工", "航空", "航天", "船舶", "汽车", "乘用车", "载货", "发动机", "底盘",
              "零部件", "设备", "仪器", "仪表", "自动化", "工业", "工程", "建筑", "专用", "通用", "系统",
              "电机", "重型", "锻造", "工具", "机器人", "客车", "轮胎", "车身", "饰件")),
    ("地产", ("房地产", "地产", "园区", "开发")),
    ("农业", ("农", "牧", "渔", "种业", "饲料", "养殖", "种植")),
    ("交运", ("交通运输", "运输", "物流", "港口", "航运", "机场", "公路", "铁路", "快递", "仓储")),
    ("能源", ("油气", "能源", "油田", "炼油")),
]


def coarsen(label: str) -> str:
    if not label:
        return "其他"
    s = str(label)
    for bucket, keys in _RULES:
        if any(k in s for k in keys):
            return bucket
    return "其他"


def coarsen_map(mapping: Dict[str, str]) -> Dict[str, str]:
    return {sym: coarsen(label) for sym, label in mapping.items()}
