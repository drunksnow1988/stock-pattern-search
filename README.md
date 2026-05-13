# A股形态搜索

在 5000+ 只 A 股中，找到与你手绘曲线走势相近的股票。

## 功能

- 🖊️ 画板：鼠标/触摸绘制目标走势形态
- ⚡ 三阶段匹配：Pearson 相关 → 欧氏距离 → DTW，< 100ms 返回结果
- 📊 结果展示：相似度评分 + 迷你走势图，点击跳转东方财富
- 💾 本地缓存：首次下载约 5 分钟，之后秒速启动

## 快速开始

**环境要求：** Python 3.9+

```bash
git clone https://gitee.com/你的用户名/stock-pattern-search.git
cd stock-pattern-search
./start.sh
```

浏览器打开 http://localhost:5001

## 数据来源

- 股票列表：新浪财经
- 历史价格：腾讯财经（前复权日线）

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python · Flask · NumPy |
| 前端 | 原生 HTML/CSS/JS · Canvas |
| 算法 | DTW (Sakoe-Chiba) + Pearson 综合评分 |
