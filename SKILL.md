---
name: hk-annual-report-extractor
description: 提取香港上市公司年报PDF并输出Markdown。适用于批量处理港股年报、自动识别两栏排版目录、按章节拆分内容，并为每份年报生成一个分章节合并Markdown文件。
---

# 港股年报提取器

用于将港股公司年报 PDF 批量转换为结构化 Markdown。

## 触发场景

- 用户要处理香港上市公司年报 PDF（单份或批量）
- 用户要求处理两栏排版目录与正文
- 用户要求章节化输出，并额外生成整份合并版 Markdown

## 快速使用

1) 安装依赖

```bash
pip install -r /home/vboxuser/Investment_Research/capital-airport/hk-annual-report-extractor/requirements.txt
```

2) 复制并编辑配置

```bash
cp /home/vboxuser/Investment_Research/capital-airport/hk-annual-report-extractor/assets/config_template.yaml ./hk_config.yaml
```

3) 运行

```bash
python /home/vboxuser/Investment_Research/capital-airport/hk-annual-report-extractor/scripts/extract_hk_annual_reports.py --config ./hk_config.yaml
```

## 命令行覆盖

配置文件参数可被 CLI 覆盖：

```bash
python /home/vboxuser/Investment_Research/capital-airport/hk-annual-report-extractor/scripts/extract_hk_annual_reports.py \
  --config ./hk_config.yaml \
  --year 2024 \
  -i ./annual_reports \
  -o ./annual_reports_markdown
```

## 输出说明

- 按章节文件：`{year}_{序号}_{章节标题}.md`
- 按年报合并文件：由 `merged_filename_template` 控制（默认 `{year}_combined.md`）
- 汇总文件：`00_summary.md`

## 核心能力

- 两栏检测：基于词元坐标分布，自动判断双栏并按左/右顺序拼接
- 目录识别：关键词 + 目录行特征 + 连续目录页扩展
- 目录解析：兼容常见港股目录格式（页码前置、页码后置、点线对齐）
- 页码映射：印刷页码到 PDF 索引自动映射，未命中时线性回退

## 失败回退策略

- 若目录解析失败，使用默认章节结构保障流程不中断
- 在 `00_summary.md` 标记目录识别状态与问题项

## 参考文档

- 目录模式：`references/toc_patterns.md`
- 两栏逻辑：`references/two_column_guide.md`
- 排障手册：`references/troubleshooting.md`
