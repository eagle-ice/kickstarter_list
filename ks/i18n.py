"""中文显示层：把 Kickstarter 的品类和状态译成中文。

只翻译**枚举值**（15 个大类 / 174 个子类 / 7 个状态）—— 这些是固定集合，可以人工校准。
项目名和 blurb 是自由文本且是专有名词，不做机器翻译：译了既不准确，也没法拿去
站内搜索或和原页面对照。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 状态

STATES = {
    "live":       "在筹中",
    "successful": "已成功",
    "failed":     "已失败",
    "canceled":   "已取消",
    "suspended":  "已冻结",
    "submitted":  "待审核",   # 已提交但未上线
    "started":    "草稿",     # 创建了但还没提交
}

# ---------------------------------------------------------------- 大类

PARENTS = {
    "Art":          "艺术",
    "Comics":       "漫画",
    "Crafts":       "手工",
    "Dance":        "舞蹈",
    "Design":       "设计",
    "Fashion":      "时尚",
    "Film & Video": "影视",
    "Food":         "美食",
    "Games":        "游戏",
    "Journalism":   "新闻",
    "Music":        "音乐",
    "Photography":  "摄影",
    "Publishing":   "出版",
    "Technology":   "科技",
    "Theater":      "戏剧",
}

# ---------------------------------------------------------------- 子类
# 同名子类跨大类含义不同（Comedy/Spaces/Events/Video...），用 (大类, 子类) 覆盖。

_BY_PARENT = {
    ("Dance", "Spaces"):        "排练场地",
    ("Food", "Spaces"):         "餐饮空间",
    ("Publishing", "Spaces"):   "文学空间",
    ("Theater", "Spaces"):      "剧场空间",
    ("Music", "Comedy"):        "喜剧音乐",
    ("Film & Video", "Comedy"): "喜剧片",
    ("Publishing", "Comedy"):   "幽默读物",
    ("Theater", "Comedy"):      "喜剧",
    ("Comics", "Events"):       "漫画活动",
    ("Food", "Events"):         "美食活动",
    ("Comics", "Anthologies"):  "漫画选集",
    ("Publishing", "Anthologies"): "文选",
    ("Film & Video", "Experimental"): "实验影像",
    ("Theater", "Experimental"): "实验戏剧",
    ("Film & Video", "Festivals"): "影展",
    ("Theater", "Festivals"):   "戏剧节",
    ("Journalism", "Video"):    "视频报道",
    ("Journalism", "Audio"):    "音频报道",
    ("Journalism", "Photo"):    "图片报道",
    ("Journalism", "Print"):    "纸媒",
    ("Journalism", "Web"):      "网络媒体",
    ("Technology", "Web"):      "Web 技术",
    ("Music", "Kids"):          "儿童音乐",
    ("Photography", "Animals"): "动物摄影",
    ("Photography", "Nature"):  "自然摄影",
    ("Photography", "People"):  "人像摄影",
    ("Photography", "Places"):  "风光摄影",
    ("Photography", "Fine Art"): "艺术摄影",
    ("Dance", "Performances"):  "舞蹈演出",
    ("Dance", "Workshops"):     "舞蹈工作坊",
    ("Dance", "Residencies"):   "驻留计划",
}

_FLAT = {
    # Art 艺术
    "Art": "艺术", "Ceramics": "陶艺", "Conceptual Art": "观念艺术",
    "Digital Art": "数字艺术", "Illustration": "插画", "Installations": "装置艺术",
    "Mixed Media": "综合媒介", "Painting": "绘画", "Performance Art": "行为艺术",
    "Public Art": "公共艺术", "Sculpture": "雕塑", "Social Practice": "社会实践",
    "Textiles": "纺织艺术", "Video Art": "影像艺术",
    # Comics 漫画
    "Comic Books": "漫画书", "Comics": "漫画", "Graphic Novels": "图像小说",
    "Webcomics": "网络漫画",
    # Crafts 手工
    "Candles": "蜡烛", "Crafts": "手工", "Crochet": "钩织", "DIY": "DIY",
    "Embroidery": "刺绣", "Glass": "玻璃工艺", "Knitting": "编织",
    "Pottery": "陶器", "Printing": "版画印刷", "Quilts": "拼布",
    "Stationery": "文具", "Taxidermy": "标本制作", "Weaving": "织造",
    "Woodworking": "木工",
    # Dance 舞蹈
    "Dance": "舞蹈",
    # Design 设计
    "Architecture": "建筑", "Civic Design": "公共设计", "Design": "设计",
    "Graphic Design": "平面设计", "Interactive Design": "交互设计",
    "Product Design": "产品设计", "Toys": "玩具", "Typography": "字体设计",
    # Fashion 时尚
    "Accessories": "配饰", "Apparel": "服装", "Childrenswear": "童装",
    "Couture": "高定", "Fashion": "时尚", "Footwear": "鞋履",
    "Jewelry": "珠宝", "Pet Fashion": "宠物时尚", "Ready-to-wear": "成衣",
    # Film & Video 影视
    "Action": "动作片", "Animation": "动画", "Documentary": "纪录片",
    "Drama": "剧情片", "Family": "家庭片", "Fantasy": "奇幻片",
    "Film & Video": "影视", "Horror": "恐怖片", "Movie Theaters": "影院",
    "Music Videos": "音乐录影", "Narrative Film": "叙事电影", "Romance": "爱情片",
    "Science Fiction": "科幻片", "Shorts": "短片", "Television": "电视剧",
    "Thrillers": "惊悚片", "Webseries": "网剧",
    # Food 美食
    "Bacon": "培根", "Community Gardens": "社区花园", "Cookbooks": "食谱书",
    "Drinks": "饮品", "Farmer's Markets": "农夫市集", "Farms": "农场",
    "Food": "美食", "Food Trucks": "餐车", "Restaurants": "餐厅",
    "Small Batch": "小批量食品", "Vegan": "素食",
    # Games 游戏
    "Games": "游戏", "Gaming Hardware": "游戏硬件", "Live Games": "实景游戏",
    "Mobile Games": "手机游戏", "Playing Cards": "纸牌", "Puzzles": "益智解谜",
    "STL": "3D打印模型", "TTRPG": "桌上角色扮演", "Tabletop Games": "桌游",
    "Video Games": "电子游戏",
    # Journalism 新闻
    "Journalism": "新闻",
    # Music 音乐
    "Blues": "蓝调", "Chiptune": "芯片音乐", "Classical Music": "古典乐",
    "Country & Folk": "乡村民谣", "Electronic Music": "电子乐", "Faith": "宗教音乐",
    "Hip-Hop": "嘻哈", "Indie Rock": "独立摇滚", "Jazz": "爵士",
    "Latin": "拉丁音乐", "Metal": "金属", "Music": "音乐", "Pop": "流行",
    "Punk": "朋克", "R&B": "R&B", "Rock": "摇滚", "World Music": "世界音乐",
    # Photography 摄影
    "Photobooks": "摄影集", "Photography": "摄影",
    # Publishing 出版
    "Academic": "学术", "Art Books": "艺术书", "Audiobooks": "有声书",
    "Calendars": "日历", "Children's Books": "童书", "Fiction": "小说",
    "Letterpress": "活版印刷", "Literary Journals": "文学期刊",
    "Literary Spaces": "文学空间",
    "Nonfiction": "非虚构", "Periodicals": "连续出版物", "Poetry": "诗歌",
    "Publishing": "出版", "Radio & Podcasts": "电台播客", "Translations": "翻译",
    "Young Adult": "青少年读物", "Zines": "独立刊物",
    # Technology 科技
    "3D Printing": "3D打印", "Apps": "应用", "Camera Equipment": "摄影器材",
    "DIY Electronics": "DIY电子", "Fabrication Tools": "制造工具",
    "Flight": "飞行器", "Gadgets": "智能硬件", "Hardware": "硬件",
    "Makerspaces": "创客空间", "Robots": "机器人", "Software": "软件",
    "Sound": "音频设备", "Space Exploration": "太空探索",
    "Technology": "科技", "Wearables": "可穿戴设备",
    # Theater 戏剧
    "Immersive": "沉浸式戏剧", "Musical": "音乐剧", "Plays": "话剧",
    "Theater": "戏剧",
}


# 中文语境里直接沿用原文的词，不算漏译
AS_IS = {"R&B", "DIY"}


def state(value: str | None) -> str:
    return STATES.get(value, value or "")


def parent(value: str | None) -> str:
    return PARENTS.get(value, value or "")


def category(value: str | None, parent_name: str | None = None) -> str:
    """子类翻译。同名跨大类的先查 (大类, 子类) 覆盖表。"""
    if value is None:
        return ""
    hit = _BY_PARENT.get((parent_name, value))
    if hit:
        return hit
    return _FLAT.get(value, value)


def coverage() -> dict:
    """自检用：返回各表条目数。"""
    return {"states": len(STATES), "parents": len(PARENTS),
            "flat": len(_FLAT), "by_parent": len(_BY_PARENT), "as_is": len(AS_IS)}


def missing(pairs, states=(), parents=()) -> dict:
    """给定库里出现过的值，返回漏译清单。tests 和运维自检用。"""
    return {
        "states":   [x for x in states if x and state(x) == x],
        "parents":  [x for x in parents if x and parent(x) == x],
        "categories": [(p, c) for p, c in pairs
                       if c and c not in AS_IS and category(c, p) == c],
    }


# ---------------------------------------------------------------- 国家
# 只覆盖 Kickstarter 支持发起的 20 多个国家，其余原样返回。

COUNTRIES = {
    "US": "美国", "GB": "英国", "CA": "加拿大", "AU": "澳大利亚",
    "DE": "德国", "FR": "法国", "IT": "意大利", "ES": "西班牙",
    "NL": "荷兰", "SE": "瑞典", "DK": "丹麦", "NO": "挪威",
    "IE": "爱尔兰", "CH": "瑞士", "AT": "奥地利", "BE": "比利时",
    "LU": "卢森堡", "NZ": "新西兰", "JP": "日本", "HK": "中国香港",
    "SG": "新加坡", "MX": "墨西哥", "PL": "波兰", "GR": "希腊",
    "SI": "斯洛文尼亚",
}


def country(code: str | None) -> str:
    return COUNTRIES.get(code, code or "")


# ---------------------------------------------------------------- 反查
# 命令行允许直接输中文（--parent 科技），内部再转回英文去查库。

_PARENT_REV = {zh: en for en, zh in PARENTS.items()}
_CATEGORY_REV: dict[str, str] = {}
for _en, _zh in _FLAT.items():
    _CATEGORY_REV.setdefault(_zh, _en)
for (_p, _c), _zh in _BY_PARENT.items():
    _CATEGORY_REV.setdefault(_zh, _c)


def to_parent_en(value: str | None) -> str | None:
    """'科技' -> 'Technology'；已经是英文或不认识就原样返回。"""
    if not value:
        return value
    return _PARENT_REV.get(value, value)


def to_category_en(value: str | None) -> str | None:
    if not value:
        return value
    return _CATEGORY_REV.get(value, value)
