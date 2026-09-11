# 睡前消息每日新闻列表 Skill

《睡前消息》栏目每天晚上发布一期新闻列表。本仓库提供一个 AI agent skill（适用于 Claude Code 等），随时查询当天或历史某天的新闻列表，并附带配套的 eval 测试。

数据本体存放在独立仓库 [liusining/shuiqian-news-list](https://github.com/liusining/shuiqian-news-list)（按天存放的 JSON，每日更新），skill 直接从 GitHub 读取。

## 安装 skill

```bash
npx skills add liusining/shuiqian-news-skill
```

或克隆本仓库后执行 `npx skills add ./skills --all`。

装好后直接对 agent 说「今天的睡前消息」「2023年3月15日的睡前新闻」即可。

## 数据接口与数据范围

见数据仓库 [shuiqian-news-list](https://github.com/liusining/shuiqian-news-list) 的 README。

## Eval

`evals/` 下是确定性的 eval 套件（程序断言，无 LLM 判分），覆盖触发边界、排版正确性、404 分支与批量获取：

```bash
python3 evals/run_evals.py                    # 全量
python3 evals/run_evals.py --only p-bulk-all  # 单条
```

运行依赖本机可用的 `codex` CLI；运行记录写入 `evals/runs/`（不入库）。

## 版权声明

本仓库的 skill 文本与脚本以 MIT 许可发布（见 LICENSE）。新闻列表内容版权归《睡前消息》编辑部所有，存放于数据仓库，不在本仓库许可范围内。
