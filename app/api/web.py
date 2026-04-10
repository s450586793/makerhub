from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.settings import ROOT_DIR


router = APIRouter()
templates = Jinja2Templates(directory=str(ROOT_DIR / "app" / "templates"))


DETAIL_PREVIEW = {
    "title": "[少量换色]海上餐厅巴拉蒂/海贼王/山治（200+零件）",
    "author": "肌肉弗兰奇",
    "author_avatar": "https://makerworld.bblmw.cn/makerworld/model/CNa38cb95ec21242/design/cf9a45da2fedb987.png",
    "cover": "https://makerworld.bblmw.cn/makerworld/model/DSM00000002247129/design/23185951b543dc84.png",
    "thumbs": [
        "https://makerworld.bblmw.cn/makerworld/model/DSM00000002247129/design/23185951b543dc84.png",
        "https://makerworld.bblmw.cn/makerworld/model/DSM00000002247129/design/dabe95f6cb2bf9b9.png",
        "https://makerworld.bblmw.cn/makerworld/model/DSM00000002247129/design/b9c717bde34f636d.png",
        "https://makerworld.bblmw.cn/makerworld/model/DSM00000002247129/design/d1afae7c692540aa.png",
    ],
    "stats": {"likes": 29, "favorites": 87, "comments": 26, "downloads": 47, "prints": 23},
    "profiles": [
        {"machine": "P1S", "title": "海上餐厅-单色部分", "time": "29.6 h", "plates": 20, "rating": "5.0 (5)"},
        {"machine": "H2C", "title": "海上餐厅-多色部分", "time": "16 h", "plates": 8, "rating": "5.0 (3)"},
        {"machine": "X1E", "title": "增量部分（摆件版本）", "time": "12.3 h", "plates": 8, "rating": "4.9 (8)"},
    ],
    "summary": """
    <div class="boost-card">
      <div class="boost-card__title">Boost Me (免费)</div>
      <div class="boost-card__body">你的助力，将会成为下艘船的碎片。</div>
    </div>
    <h3>描述</h3>
    <p>这块区域会按目标站详情页的富文本结构复刻，包括大图、分段标题、说明区、评论区。</p>
    <p>新项目里不再兼容旧模板，详情页单独按目标站页面做。</p>
    """,
    "comments": [
        {"author": "用户 A", "time": "2026-03-23 11:20", "content": "这套结构很完整，细节做得很好。"},
        {"author": "用户 B", "time": "2026-03-16 09:43", "content": "如果后续评论图片也能同步展示，详情页会更接近原站。"},
    ],
}


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@router.get("/detail-preview", response_class=HTMLResponse)
async def detail_preview(request: Request):
    return templates.TemplateResponse(
        "detail.html",
        {"request": request, "detail": DETAIL_PREVIEW},
    )

